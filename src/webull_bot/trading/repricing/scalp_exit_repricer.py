import logging
import time

from webull_bot.trading.handlers.broker_conflict_check import _broker_conflict
from webull_bot.trading.orders.locks import _rekey_working_order, _working_orders_lock
from webull_bot.trading.orders.manual_touch import _manual_touch_active
from webull_bot.trading.orders.rate_limit_retry import _retry_once_on_rate_limit
from webull_bot.trading.util.concurrent_dispatch import _dispatch_concurrently

log = logging.getLogger("webull-bot")


def reprice_volatility_scalp_exits(
    self, positions: list[dict], core_session_active: bool = False
) -> None:
    """The volatility-scalp equivalent of reprice_resting_exits, on
    its own much faster VOLATILITY_SCALP_REPRICE_SECONDS cadence
    (default 1s vs the generic ORDER_MONITOR_SECONDS, default 5s) -
    "actively reprice within the spread to capture the profit, cent
    by cent" only makes sense at a cadence this strategy is actually
    meant to run at. Same cancel-and-replace-toward-the-current-ask
    logic as reprice_resting_exits, just scoped to symbols currently
    volatility-scalp eligible instead of everything else.
    """
    now = time.monotonic()
    if now - self.last_volatility_reprice < float(
        self.config.volatility_scalp_reprice_seconds
    ):
        return
    self.last_volatility_reprice = now
    candidates: list[tuple[str, str, dict]] = []
    with _working_orders_lock(self):
        snapshot = list(self.working_orders.items())
    for order_id, order in snapshot:
        action = order.get("action")
        key = str(order.get("key") or "")
        if action != "PROFIT" or not key.startswith("STOCK:"):
            continue
        if order.get("cancel_requested_at") is not None:
            continue
        symbol = key.split(":", 1)[1]
        if symbol in self.stop_loss_escalated:
            continue
        # By request: "when i touch a stock stop doing anything
        # with it while i am there."
        if _manual_touch_active(self, symbol):
            continue
        # Live incident (PETZ): see _broker_conflict's docstring.
        if _broker_conflict(self, symbol):
            continue
        # Also keeps repricing an already-adopted cohort position
        # even if it's no longer live-eligible this exact cycle -
        # same reasoning as the exit-override/pricing gates in
        # trade_stocks (a position with real capital committed
        # across several averaging buys shouldn't lose active
        # management just because a transient stdev recalculation
        # dipped under the eligibility bar for one cycle).
        if (
            not self.strategy.is_volatility_scalp_eligible(symbol)
            and symbol not in self.volatility_scalp_positions
        ):
            continue
        candidates.append((order_id, symbol, order))
    if not candidates:
        return
    quotes = self._batched_quotes([symbol for _, symbol, _ in candidates])

    def _reprice_one(candidate: tuple[str, str, dict]) -> None:
        order_id, symbol, order = candidate
        key = str(order.get("key") or "")
        action = order.get("action")
        try:
            quote = quotes.get(symbol)
            if quote is None:
                return
            quantity, cost = self.api.stock_position(symbol, positions)
            if quantity <= 0:
                return
            exit_is_fractional = self.is_fractional_quantity(quantity)
            if exit_is_fractional and not core_session_active:
                return
            # Live incident (this bug, caught from a live report):
            # "some sell orders are not repricing down in the
            # spread." The old check here was a blunt "ask < cost ->
            # skip entirely," which correctly avoided ever repricing
            # below cost, but as a side effect froze the resting
            # order completely stuck at a stale price the instant
            # the ask dipped below cost - even when the BID still
            # cleared a genuinely profitable fill. Reuses the same
            # _stall_exit_price logic the initial PROFIT placement
            # and the escalation fix both already use (bid first if
            # it clears cost + min_profit + fee, else a spread-
            # sanity-checked ask fallback) - this can still reprice
            # DOWN toward a lower, still-profitable price, or fill
            # immediately at the bid, instead of freezing.
            if exit_is_fractional:
                fee_per_share = self.config.sell_fee_dollars
            else:
                fee_per_share = self.config.sell_fee_dollars / quantity
            min_profit = cost * self.config.volatility_scalp_target_percent
            limit_price = self._stall_exit_price(
                quote,
                cost,
                min_profit,
                fee_per_share,
                max_spread_percent=(
                    self.config.volatility_scalp_max_exit_spread_percent
                ),
            )
            if limit_price is None or limit_price == order.get("limit_price"):
                return
            _retry_once_on_rate_limit(self.api.cancel, order_id)
            new_order_id = _retry_once_on_rate_limit(
                self.api.place_stock,
                symbol,
                "SELL",
                quantity,
                limit_price=limit_price,
            )
            _rekey_working_order(
                self,
                order_id,
                new_order_id,
                {
                    "submitted_at": now,
                    "key": key,
                    "action": action,
                    "cancel_requested_at": None,
                    "limit_price": limit_price,
                    "pnl": order.get("pnl"),
                },
            )
            self.status.rekey_trade(order_id, new_order_id)
            log.info(
                "SCALP  | %-8s | reprice | limit=%s | id=%s",
                symbol,
                limit_price,
                new_order_id,
            )
        except Exception as exc:
            if self.is_order_not_cancelable(exc):
                log.warning(
                    "SCALP  | %s | reprice skipped | order already "
                    "resolving | %s",
                    symbol,
                    exc,
                )
            else:
                log.error("SCALP  | %s | reprice failed | %s", symbol, exc)

    _dispatch_concurrently(candidates, _reprice_one)
