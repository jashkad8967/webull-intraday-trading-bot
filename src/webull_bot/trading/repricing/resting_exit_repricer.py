import logging
import time
from decimal import Decimal

from webull_bot.trading.handlers.broker_conflict_check import _broker_conflict
from webull_bot.trading.orders.locks import _rekey_working_order, _working_orders_lock
from webull_bot.trading.orders.manual_touch import _manual_touch_active
from webull_bot.trading.orders.rate_limit_retry import _retry_once_on_rate_limit
from webull_bot.trading.util.concurrent_dispatch import _dispatch_concurrently

log = logging.getLogger("webull-bot")


def reprice_resting_exits(
    self, positions: list[dict], core_session_active: bool = False
) -> None:
    """Continuously re-quote a resting stock PROFIT sell order to track
    the current ask - the top of the spread - for as long as it stays
    unfilled and unescalated ("keep modifying to stay in the spread
    until sold"). Once a symbol is escalated, this stops chasing the ask
    for it and leaves resubmission to the normal escalation path. Also
    skips any symbol currently volatility-scalp eligible - see
    reprice_volatility_scalp_exits, which handles those on a much
    faster, dedicated cadence instead.

    PROFIT only, deliberately - not STOP. A stop-loss needs to fill
    fast to cap a loss; chasing the ask means chasing a price *above*
    the market, and if the stock is actively falling, repeatedly
    cancelling and re-resting a stop above a falling ask can leave it
    unfilled for the whole 15s until escalation, during which the loss
    keeps growing. Only escalate_stalled_stop_losses should ever move a
    stop's price, and only towards a guaranteed-fill crossing price.

    This cancels and replaces the working order *directly* - it does
    not touch pending_stock_exits or stop_exit_submitted, and does not
    call record_trade/record_realized_exit again. Those already ran
    once at the original PROFIT submission; calling them again here on
    every re-quote would record the same realized P&L multiple times
    for one logical exit.
    """
    # By request: "bring repricing to the 0.25 lane as well" -
    # this used to throttle on order_monitor_seconds (5s, shared
    # with monitor_working_orders' fill-detection cadence) even
    # though it already runs inside the fast poll_seconds (0.25s)
    # position-protection loop - now re-quotes every tick of that
    # loop instead of only once every 5s.
    now = time.monotonic()
    if now - self.last_reprice < float(
        getattr(self.config, "poll_seconds", Decimal("0.25"))
    ):
        return
    self.last_reprice = now
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
        if (
            self.strategy.is_volatility_scalp_eligible(symbol)
            or symbol in self.volatility_scalp_positions
        ):
            # Handled by the faster reprice_volatility_scalp_exits
            # cadence instead - condensed onto eligibility alone
            # (not the narrower curated self.volatility_scalp_symbols
            # cohort list), so this skip applies to ANY symbol
            # currently volatile enough to qualify, matching the
            # entry side below. Also keeps deferring for an already-
            # adopted cohort position even once it's no longer
            # live-eligible this cycle, so the two repricers never
            # both try to manage the same resting order at once.
            continue
        candidates.append((order_id, symbol, order))
    if not candidates:
        return
    # One batched snapshot call for every symbol this cycle needs,
    # instead of one self.api.stock_quote(symbol) round-trip per
    # candidate - see _batched_quotes.
    quotes = self._batched_quotes([symbol for _, symbol, _ in candidates])

    def _reprice_one(candidate: tuple[str, str, dict]) -> None:
        order_id, symbol, order = candidate
        key = str(order.get("key") or "")
        action = order.get("action")
        try:
            quote = quotes.get(symbol)
            if quote is None:
                return
            ask = self.api.quote_ask(quote)
            if ask is None or ask == order.get("limit_price"):
                return
            quantity, cost = self.api.stock_position(symbol, positions)
            if quantity <= 0:
                return
            # Same fractional/core-hours constraint as trade_stocks'
            # PROFIT exit: cancel-and-replace can't succeed on a
            # fractional quantity outside core hours either, so leave
            # the existing resting order alone rather than cancelling
            # it for a replacement that will just get rejected.
            if self.is_fractional_quantity(quantity) and not core_session_active:
                return
            if cost > 0 and ask < cost:
                # Never chase the ask down below entry cost - the
                # existing resting order was already validly priced at
                # or above the profit target when submitted; repricing
                # to a falling ask here could reprice a profit-take
                # into a loss. Leave it resting and let escalation (or
                # the ask recovering) handle it instead.
                return
            _retry_once_on_rate_limit(self.api.cancel, order_id)
            new_order_id = _retry_once_on_rate_limit(
                self.api.place_stock,
                symbol,
                "SELL",
                quantity,
                limit_price=ask,
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
                    "limit_price": ask,
                    # Carry the pnl already recorded at the original PROFIT
                    # submission forward - this is the same logical exit,
                    # not a new one, so if the repriced order itself never
                    # fills it's still the correct amount to reverse.
                    "pnl": order.get("pnl"),
                },
            )
            # The dashboard's trade-log entry is still filed under the
            # cancelled order_id - repoint it, or a later reversal
            # (which only ever learns new_order_id) can't find it to
            # discard, leaving a cancelled order's phantom profit
            # visible forever. See StatusWriter.rekey_trade.
            self.status.rekey_trade(order_id, new_order_id)
            log.info(
                "REPRICE| %-8s | %-6s | ask=%s | id=%s",
                symbol,
                action,
                ask,
                new_order_id,
            )
        except Exception as exc:
            if self.is_order_not_cancelable(exc):
                log.warning(
                    "REPRICE| %s | reprice skipped | order already "
                    "resolving | %s",
                    symbol,
                    exc,
                )
            else:
                log.error("REPRICE| %s | %s", symbol, exc)

    # By request: "do not wait for the response to fire another
    # request" - fires every candidate's cancel+place concurrently
    # instead of waiting out each one's full round-trip before the
    # next candidate even starts. See _dispatch_concurrently.
    _dispatch_concurrently(candidates, _reprice_one)
