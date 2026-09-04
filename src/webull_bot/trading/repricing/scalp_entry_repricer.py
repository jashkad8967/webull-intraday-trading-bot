import logging
import time

from webull_bot.trading.handlers.broker_conflict_check import _broker_conflict
from webull_bot.trading.orders.locks import _rekey_working_order, _working_orders_lock
from webull_bot.trading.orders.manual_touch import _manual_touch_active
from webull_bot.trading.orders.rate_limit_retry import _retry_once_on_rate_limit
from webull_bot.trading.util.concurrent_dispatch import _dispatch_concurrently
from webull_bot.webull_api import QuoteUnavailableError

log = logging.getLogger("webull-bot")


def reprice_volatility_scalp_entries(self) -> None:
    """Actively re-quotes a resting cohort BUY order toward the
    current market instead of waiting passively for price to come
    back up to the original limit - by request: "do not wait at
    all until the order gets filled, because the price may not
    reach that point, you may have to lower a little." Only ever
    lowers the limit (tracks a further decline), never raises it -
    chasing the price up would mean paying more for the same dip-
    buy, defeating the point. Same fast dedicated cadence as
    reprice_volatility_scalp_exits.
    """
    now = time.monotonic()
    if now - self.last_volatility_entry_reprice < float(
        self.config.volatility_scalp_reprice_seconds
    ):
        return
    self.last_volatility_entry_reprice = now
    candidates: list[tuple[str, str, dict]] = []
    with _working_orders_lock(self):
        snapshot = list(self.working_orders.items())
    for order_id, order in snapshot:
        action = order.get("action")
        key = str(order.get("key") or "")
        if action != "BUY" or not key.startswith("STOCK:"):
            continue
        if order.get("cancel_requested_at") is not None:
            continue
        symbol = key.split(":", 1)[1]
        # By request: "when i touch a stock stop doing anything
        # with it while i am there."
        if _manual_touch_active(self, symbol):
            continue
        # Live incident (PETZ): see _broker_conflict's docstring.
        if _broker_conflict(self, symbol):
            continue
        # Also keeps repricing an already-adopted cohort position's
        # resting BUY (e.g. an averaging-down order) even if it's no
        # longer live-eligible this exact cycle - same reasoning as
        # the other volatility-scalp gates.
        if (
            not self.strategy.is_volatility_scalp_eligible(symbol)
            and symbol not in self.volatility_scalp_positions
        ):
            continue
        quantity = order.get("quantity")
        if not quantity or quantity <= 0:
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
            limit_price = self.api.stock_limit_price(quote, "BUY")
            current_limit = order.get("limit_price")
            if (
                limit_price is None
                or current_limit is None
                or limit_price >= current_limit
            ):
                return
            _retry_once_on_rate_limit(self.api.cancel, order_id)
            new_order_id = _retry_once_on_rate_limit(
                self.api.place_stock,
                symbol,
                "BUY",
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
                    "quantity": quantity,
                },
            )
            self.status.rekey_trade(order_id, new_order_id)
            log.info(
                "SCALP  | %-8s | reprice entry | limit=%s | id=%s",
                symbol,
                limit_price,
                new_order_id,
            )
        except QuoteUnavailableError as exc:
            # A momentarily missing/crossed bid-ask (thin/low-volume
            # penny stock, a quote glitch) is expected and already
            # fully handled - the loop just moves on to the next
            # order next cycle. WARNING, not ERROR - this isn't a
            # fault, it's the same "no data -> don't act" convention
            # every other entry gate in this strategy already uses.
            log.warning("SCALP  | %s | entry reprice skipped | %s", symbol, exc)
        except Exception as exc:
            if self.is_order_not_cancelable(exc):
                log.warning(
                    "SCALP  | %s | entry reprice skipped | order "
                    "already resolving | %s",
                    symbol,
                    exc,
                )
            else:
                log.error(
                    "SCALP  | %s | entry reprice failed | %s", symbol, exc
                )

    _dispatch_concurrently(candidates, _reprice_one)
