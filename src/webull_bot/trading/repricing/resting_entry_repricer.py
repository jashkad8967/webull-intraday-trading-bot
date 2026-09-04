import logging
import time
from decimal import Decimal

from webull_bot.trading.handlers.broker_conflict_check import _broker_conflict
from webull_bot.trading.orders.locks import _rekey_working_order, _working_orders_lock
from webull_bot.trading.orders.manual_touch import _manual_touch_active
from webull_bot.trading.orders.rate_limit_retry import _retry_once_on_rate_limit
from webull_bot.trading.util.concurrent_dispatch import _dispatch_concurrently
from webull_bot.webull_api import QuoteUnavailableError

log = logging.getLogger("webull-bot")


def reprice_resting_entries(self, core_session_active: bool) -> None:
    """Continuously re-quotes a resting general (non-volatility-
    scalp) BUY/SHORT entry order to cross further into the spread
    the longer it sits unfilled, instead of resting passively at
    the original mid-price until the hard order_timeout_seconds
    cancel gives up on it entirely with no attempt to improve the
    price first. Live incident: IBRX (a DISCOVERY-bucket long
    entry) got cancelled for never filling 4 separate times in
    ~15 minutes, always at the same passive mid-price, because
    nothing ever moved the resting order closer to a fillable
    price in between. By request: covers both entry directions - a
    BUY chases up toward the current ask, a SHORT chases down
    toward the current bid.

    Skips any symbol currently volatility-scalp eligible (or
    already adopted into that cohort) - reprice_volatility_scalp_
    entries handles those on its own, much faster, dedicated
    cadence instead. Skips a fractional BUY outside core hours -
    fractional cancel-and-replace can't succeed there either, same
    constraint reprice_resting_exits already respects.
    """
    # By request: "bring repricing to the 0.25 lane as well" - see
    # reprice_resting_exits' identical note above.
    now = time.monotonic()
    if now - self.last_entry_reprice < float(
        getattr(self.config, "poll_seconds", Decimal("0.25"))
    ):
        return
    self.last_entry_reprice = now
    candidates: list[tuple[str, str, dict]] = []
    with _working_orders_lock(self):
        snapshot = list(self.working_orders.items())
    for order_id, order in snapshot:
        action = order.get("action")
        key = str(order.get("key") or "")
        if action not in ("BUY", "SHORT") or not key.startswith("STOCK:"):
            continue
        if order.get("cancel_requested_at") is not None:
            continue
        symbol = key.split(":", 1)[1]
        if (
            self.strategy.is_volatility_scalp_eligible(symbol)
            or symbol in self.volatility_scalp_positions
        ):
            continue
        # By request: "when i touch a stock stop doing anything
        # with it while i am there."
        if _manual_touch_active(self, symbol):
            continue
        # Live incident (PETZ): see _broker_conflict's docstring.
        if _broker_conflict(self, symbol):
            continue
        quantity = order.get("quantity")
        if not quantity or quantity <= 0:
            continue
        if (
            action == "BUY"
            and self.is_fractional_quantity(quantity)
            and not core_session_active
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
        quantity = order.get("quantity")
        try:
            quote = quotes.get(symbol)
            if quote is None:
                return
            current_limit = order.get("limit_price")
            if action == "BUY":
                target_price = self.api.quote_ask(quote)
                improved = (
                    target_price is not None
                    and current_limit is not None
                    and target_price > current_limit
                )
            else:
                target_price = self.api.quote_bid(quote)
                improved = (
                    target_price is not None
                    and current_limit is not None
                    and target_price < current_limit
                )
            if not improved:
                return
            _retry_once_on_rate_limit(self.api.cancel, order_id)
            new_order_id = _retry_once_on_rate_limit(
                self.api.place_stock,
                symbol,
                action,
                quantity,
                limit_price=target_price,
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
                    "limit_price": target_price,
                    "pnl": order.get("pnl"),
                    "quantity": quantity,
                },
            )
            self.status.rekey_trade(order_id, new_order_id)
            log.info(
                "REPRICE| %-8s | %-6s | limit=%s | id=%s",
                symbol,
                action,
                target_price,
                new_order_id,
            )
        except QuoteUnavailableError as exc:
            log.warning(
                "REPRICE| %s | entry reprice skipped | %s", symbol, exc
            )
        except Exception as exc:
            if self.is_order_not_cancelable(exc):
                log.warning(
                    "REPRICE| %s | entry reprice skipped | order "
                    "already resolving | %s",
                    symbol,
                    exc,
                )
            else:
                log.error(
                    "REPRICE| %s | entry reprice failed | %s", symbol, exc
                )

    _dispatch_concurrently(candidates, _reprice_one)
