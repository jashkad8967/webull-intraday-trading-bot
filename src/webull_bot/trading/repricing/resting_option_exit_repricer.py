import logging
import time
from decimal import ROUND_DOWN, Decimal

from webull_bot.trading.guards.price_sanity import OPTION_PRICE_SANITY_TOLERANCE
from webull_bot.trading.handlers.broker_conflict_check import _broker_conflict
from webull_bot.trading.orders.locks import _rekey_working_order, _working_orders_lock
from webull_bot.trading.orders.manual_touch import _manual_touch_active
from webull_bot.trading.orders.rate_limit_retry import _retry_once_on_rate_limit
from webull_bot.webull_api import QuoteUnavailableError

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

    Also actively re-quotes resting STOP orders now - by explicit
    request ("profit is only realized when the order goes through,
    not just getting cancelled... same with exit"): the original
    "PROFIT only" scoping here assumed a stop just needs to fill fast
    once, so continuously repricing it would only delay that. Live
    evidence didn't bear that out - AMC/SNAP/ORCL stop-loss orders sat
    unfilled for full 120s cycles, repeatedly, on thin contracts whose
    bid kept drifting away from the order's now-stale crossing price
    in the meantime. A STOP's target here still ONLY tracks
    option_limit_price's aggressive bid-crossing formula (never chases
    upward toward the ask the way PROFIT does), so it stays a
    loss-capping exit, not a profit-maximizing one - it just stays
    live against a moving market instead of going stale for up to two
    full minutes between attempts.
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
        if action not in ("PROFIT", "STOP") or not key.startswith("OPTION:"):
            continue
        if order.get("cancel_requested_at") is not None:
            continue
        symbol = key.split(":", 1)[1]
        if _manual_touch_active(self, symbol):
            continue
        # Live incident precedent (PETZ, stock side): a broker-conflict
        # symbol's own view of its position doesn't match the account,
        # and every OTHER repricer (stock entries/exits, scalp
        # entries/exits, stop-loss escalation) already skips it while
        # flagged - these two option repricers never did.
        # handle_broker_conflict is called with the full OCC contract
        # symbol for options (see bot.py), matching `symbol` here.
        if _broker_conflict(self, symbol):
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
            contract = contract_by_symbol[symbol]
            quantity, cost = self.api.option_position(contract, positions)
            if quantity <= 0:
                continue
            if action == "PROFIT":
                # By explicit request, after a live miss: a QQQ put
                # bought at 0.45 sat with a 0.48/0.50 quote, and this
                # re-quoted to the FULL ASK (0.50) every cycle - so it
                # never filled, even though the 0.49 midpoint was a
                # genuine, fillable profit ("the bot keeps requesting
                # 50 even though 49 reprice midpoint would bring
                # significant profit as well... now that chance is
                # gone"). An unfilled order at the ask is worth
                # nothing; a filled one at the midpoint is real money.
                # Targets the midpoint now, on the contract's REAL
                # tick (option_tick_from_quote - a 0.48 bid is itself
                # proof this one quotes in pennies, so 0.49 is a valid
                # price the old flat $0.05 grid could never express).
                # Clamped at/above the bid so this only ever gives up
                # the half-spread it has to, never more.
                #
                # By explicit follow-up ("only if the midpoint is a
                # good profit though"): dropping to the midpoint gives
                # up half the spread, so it's only worth doing when
                # what's left is still a real win. The bar is this
                # position's OWN configured profit target - if the
                # midpoint clears cost * (1 + option_take_profit_
                # percent) it's a good profit by the strategy's own
                # definition and worth banking; if it doesn't, hold out
                # at the ask rather than concede the spread for a
                # marginal gain.
                bid = self.api.quote_bid(quote)
                ask = self.api.quote_ask(quote)
                target = ask
                if bid is not None and ask is not None and 0 < bid <= ask:
                    tick = self.api.option_tick_from_quote(bid, ask)
                    midpoint = max(
                        self.api._quantize_to_option_tick(
                            (bid + ask) / 2, ROUND_DOWN, tick
                        ),
                        bid,
                    )
                    good_profit = cost > 0 and midpoint >= cost * (
                        Decimal("1") + self.config.option_take_profit_percent
                    )
                    if good_profit:
                        target = midpoint
                if target is None or target == order.get("limit_price"):
                    continue
                if cost > 0 and target < cost + (
                    self.config.sell_fee_dollars / (quantity * 100)
                ):
                    # Never chase down below entry cost - see the
                    # matching stock-side guard in
                    # reprice_resting_exits.
                    #
                    # Includes the flat sell fee, not just bare cost,
                    # by explicit request ("still make sure to try and
                    # make profit, not sell a loss for a profit"). A
                    # target sitting between cost and cost+fee looks
                    # like a gain and books as a LOSS once the $0.02
                    # lands - exactly how LFUS/FIGR/MAGN exited
                    # "profitably" into real losses. A genuine STOP is
                    # unaffected: it takes the branch below, which is
                    # meant to cross down and cap a loss.
                    continue
            else:
                # STOP: track the same aggressive bid-crossing formula
                # the initial exit used, not the ask - a stop stays a
                # loss-capping exit, never chasing upward toward
                # profit-taking territory the way PROFIT's target does.
                try:
                    target = self.api.option_limit_price(quote, "SELL")
                except QuoteUnavailableError:
                    continue
                if target is None or target == order.get("limit_price"):
                    continue
            if not self.price_sanity_cooldown_ready(symbol):
                # Live incident (NVDA/BMEA-style): without this, a
                # symbol whose ask sits durably past the sanity
                # tolerance gets re-rejected on literally every poll
                # cycle with zero backoff - 60+ ERROR lines in under
                # 10 minutes for one contract. price_sanity_ok itself
                # already stamps price_sanity_rejected_at on a
                # rejection; this just has to actually be checked
                # before trying again, same as every stock-side caller
                # already does.
                continue
            if not self.price_sanity_ok(
                symbol, self.api.quote_price(quote), target,
                tolerance=OPTION_PRICE_SANITY_TOLERANCE,
            ):
                continue
            _retry_once_on_rate_limit(self.api.cancel, order_id)
            new_order_id = _retry_once_on_rate_limit(
                self.api.place_option,
                contract,
                "SELL",
                int(quantity),
                target,
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
                    "limit_price": target,
                    "pnl": order.get("pnl"),
                    "quantity": quantity,
                },
            )
            self.status.rekey_trade(order_id, new_order_id)
            log.info(
                "REPRICE| %-8s | %-6s | %s=%s | id=%s",
                symbol, action,
                "ask" if action == "PROFIT" else "bid-cross",
                target, new_order_id,
            )
        except Exception as exc:
            if self.is_order_not_cancelable(exc):
                log.warning(
                    "REPRICE| %s | reprice skipped | order already "
                    "resolving | %s", symbol, exc,
                )
            else:
                log.error("REPRICE| %s | %s", symbol, exc)
