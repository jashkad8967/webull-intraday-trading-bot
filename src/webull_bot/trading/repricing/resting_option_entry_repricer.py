import logging
import time
from decimal import ROUND_UP, Decimal

from webull_bot.trading.guards.price_sanity import OPTION_PRICE_SANITY_TOLERANCE
from webull_bot.trading.orders.locks import _rekey_working_order, _working_orders_lock
from webull_bot.trading.orders.manual_touch import _manual_touch_active
from webull_bot.trading.orders.rate_limit_retry import _retry_once_on_rate_limit
from webull_bot.webull_api import QuoteUnavailableError

log = logging.getLogger("webull-bot")


def reprice_resting_option_entries(self) -> None:
    """Options analog of reprice_resting_entries - by request: "why is
    the buy price not playing around in the spread, same with sell."
    reprice_resting_option_exits already chases the ask on a resting
    option PROFIT sell; a resting option BUY (fresh entry OR
    averaging-down) had no equivalent and just sat at its original
    limit price until order_timeout_seconds' hard cancel gave up on
    it entirely - live evidence: NKE and a UBER put both got
    cancelled unfilled after 120s the same session, at the same
    passive price the whole time, exactly the stock-side IBRX
    incident reprice_resting_entries was built to fix.

    Chases UP toward the current MIDPOINT (not the full ask) only
    - an option BUY, never a SHORT/write here. By explicit request
    ("you don't have to buy at the edges of the spread, the mid
    price is also fine"): this used to chase the full quoted ask,
    which is the AGGRESSIVE edge of the spread - fine for the very
    first, urgent submission of a genuinely time-sensitive entry, but
    not something a passive re-quote needs to escalate all the way
    to. option_limit_price already computes the same (bid+ask)/2
    midpoint the INITIAL entry order uses, so a reprice just keeps
    tracking that same passive reference price as it moves, instead
    of ratcheting toward the ask over successive reprices. Never
    reprices to a WORSE (lower, more likely to sit unfilled again)
    price than the current resting limit.

    Once an order has been resting unfilled for longer than
    option_entry_escalate_seconds (live evidence: NKE/RIVN/SNAP BUYs
    sat at mid with zero reprice activity until the generic 120s hard
    cancel gave up on them), the chase target escalates from mid
    halfway toward the ask instead of staying pinned to mid all the
    way to the hard cancel. By request, after a first version of this
    jumped straight to the full ask and a VZ put dip-entry filled at
    the max-spread price as a result: escalating all the way to the
    raw ask on the very first step pays the full spread cost on what
    is meant to be a cheap scalp entry - the same "you lose edge every
    trade" cost this session's own research flagged for thin
    contracts. The midpoint-of-mid-and-ask still gives real room to
    fill (unlike staying at mid forever) without paying the full
    spread outright.
    """
    now = time.monotonic()
    if now - self.last_option_entry_reprice < float(
        getattr(self.config, "poll_seconds", Decimal("0.25"))
    ):
        return
    self.last_option_entry_reprice = now
    with _working_orders_lock(self):
        snapshot = list(self.working_orders.items())
    candidates: list[tuple[str, str, dict]] = []
    for order_id, order in snapshot:
        action = order.get("action")
        key = str(order.get("key") or "")
        if action != "BUY" or not key.startswith("OPTION:"):
            continue
        if order.get("cancel_requested_at") is not None:
            continue
        symbol = key.split(":", 1)[1]
        if _manual_touch_active(self, symbol):
            continue
        quantity = order.get("quantity")
        if not quantity or quantity <= 0:
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
        quantity = order.get("quantity")
        try:
            quote = quote_by_symbol.get(symbol)
            if quote is None:
                continue
            submitted_at = order.get("submitted_at")
            escalated = (
                submitted_at is not None
                and now - submitted_at
                >= float(self.config.option_entry_escalate_seconds)
            )
            try:
                mid_price = self.api.option_limit_price(quote, "BUY")
                if escalated:
                    ask_price = self.api.quote_ask(quote)
                    if mid_price is not None and ask_price is not None:
                        # Live incident (this fix): the raw (mid+ask)/2
                        # average lands on off-tick values like 1.975,
                        # which OPENAPI_OPTION_PRICE_STEP_LT rejects
                        # outright (option premiums must be quoted in
                        # $0.05 increments) - route the blend through
                        # the same tick-quantizer option_limit_price
                        # itself uses, rounding UP so it stays a real
                        # escalation and not a silent no-op back to mid.
                        target_price = self.api._quantize_to_option_tick(
                            max(Decimal("0.01"), (mid_price + ask_price) / 2),
                            ROUND_UP,
                        )
                    else:
                        target_price = ask_price
                else:
                    target_price = mid_price
            except QuoteUnavailableError:
                continue
            current_limit = order.get("limit_price")
            if (
                target_price is None
                or current_limit is None
                or target_price <= current_limit
            ):
                continue
            contract = contract_by_symbol[symbol]
            if not self.price_sanity_cooldown_ready(symbol):
                # Live incident: NVDA's escalated (mid->ask) target
                # sat durably past the sanity tolerance and got
                # re-rejected on literally every poll cycle - 60
                # ERROR lines in under 10 minutes for one contract,
                # zero backoff. Same BMEA-style fix already applied
                # to place_stock_scaled/stock-side callers, now
                # applied here too.
                continue
            if not self.price_sanity_ok(
                symbol, self.api.quote_price(quote), target_price,
                tolerance=OPTION_PRICE_SANITY_TOLERANCE,
            ):
                continue
            _retry_once_on_rate_limit(self.api.cancel, order_id)
            new_order_id = _retry_once_on_rate_limit(
                self.api.place_option,
                contract,
                "BUY",
                int(quantity),
                target_price,
                "BUY_TO_OPEN",
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
                "REPRICE| %-8s | %-6s | %s=%s | id=%s",
                symbol, action,
                "mid->ask (escalated)" if escalated else "mid",
                target_price, new_order_id,
            )
        except Exception as exc:
            if self.is_order_not_cancelable(exc):
                log.warning(
                    "REPRICE| %s | entry reprice skipped | order "
                    "already resolving | %s", symbol, exc,
                )
            else:
                log.error("REPRICE| %s | entry reprice failed | %s", symbol, exc)
