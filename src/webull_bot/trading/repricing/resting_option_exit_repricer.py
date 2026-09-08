import logging
import time
from decimal import Decimal

from webull_bot.trading.guards.price_sanity import OPTION_PRICE_SANITY_TOLERANCE
from webull_bot.trading.orders.locks import _rekey_working_order, _working_orders_lock
from webull_bot.trading.orders.manual_touch import _manual_touch_active
from webull_bot.trading.orders.rate_limit_retry import _retry_once_on_rate_limit

log = logging.getLogger("webull-bot")


def reprice_resting_option_exits(
    self, positions: list[dict], core_session_active: bool = False
) -> None:
    """Options analog of reprice_resting_exits - by request: "use
    repricing to capture the trade when buying and selling options
    also." An option PROFIT sell resting away from the current ask
    was previously just left alone until monitor_working_orders'
    escalation path kicked in; this continuously re-quotes it to
    track the ask, same "keep modifying to stay in the spread until
    sold" behavior the stock side already had.

    PROFIT only, deliberately - not STOP, same reasoning as the stock
    repricer: a stop needs to fill fast to cap a loss, and chasing an
    ask upward on a falling option would only delay that fill.
    """
    now = time.monotonic()
    if now - self.last_option_reprice < float(
        getattr(self.config, "poll_seconds", Decimal("0.25"))
    ):
        return
    self.last_option_reprice = now
    with _working_orders_lock(self):
        snapshot = list(self.working_orders.items())
    candidates: list[tuple[str, str, dict]] = []
    for order_id, order in snapshot:
        action = order.get("action")
        key = str(order.get("key") or "")
        if action != "PROFIT" or not key.startswith("OPTION:"):
            continue
        if order.get("cancel_requested_at") is not None:
            continue
        symbol = key.split(":", 1)[1]
        if _manual_touch_active(self, symbol):
            continue
        candidates.append((order_id, symbol, order))
    if not candidates:
        return
    contract_by_symbol = {
        contract["symbol"]: contract for contract in self.option_contracts
    }
    candidates = [
        candidate for candidate in candidates if candidate[1] in contract_by_symbol
    ]
    if not candidates:
        return
    # option_quotes hard-rejects a batch over 20 symbols.
    quote_by_symbol: dict[str, dict] = {}
    symbols = [symbol for _, symbol, _ in candidates]
    for start in range(0, len(symbols), 20):
        chunk = symbols[start : start + 20]
        try:
            for quote in self.api.option_quotes(chunk):
                quote_by_symbol[str(quote.get("symbol", ""))] = quote
        except Exception as exc:
            log.warning("REPRICE| option quote batch failed | %s", exc)

    for order_id, symbol, order in candidates:
        key = str(order.get("key") or "")
        action = order.get("action")
        try:
            quote = quote_by_symbol.get(symbol)
            if quote is None:
                continue
            ask = self.api.quote_ask(quote)
            if ask is None or ask == order.get("limit_price"):
                continue
            contract = contract_by_symbol[symbol]
            quantity, cost = self.api.option_position(contract, positions)
            if quantity <= 0:
                continue
            if cost > 0 and ask < cost:
                # Never chase the ask down below entry cost - see the
                # matching stock-side guard in reprice_resting_exits.
                continue
            if not self.price_sanity_ok(
                symbol, self.api.quote_price(quote), ask,
                tolerance=OPTION_PRICE_SANITY_TOLERANCE,
            ):
                continue
            _retry_once_on_rate_limit(self.api.cancel, order_id)
            new_order_id = _retry_once_on_rate_limit(
                self.api.place_option,
                contract,
                "SELL",
                int(quantity),
                ask,
                "SELL_TO_CLOSE",
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
                    "pnl": order.get("pnl"),
                    "quantity": quantity,
                },
            )
            self.status.rekey_trade(order_id, new_order_id)
            log.info(
                "REPRICE| %-8s | %-6s | ask=%s | id=%s",
                symbol, action, ask, new_order_id,
            )
        except Exception as exc:
            if self.is_order_not_cancelable(exc):
                log.warning(
                    "REPRICE| %s | reprice skipped | order already "
                    "resolving | %s", symbol, exc,
                )
            else:
                log.error("REPRICE| %s | %s", symbol, exc)
