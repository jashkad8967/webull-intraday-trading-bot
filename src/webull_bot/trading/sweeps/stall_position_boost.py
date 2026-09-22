import logging
import time
from decimal import Decimal

from webull_bot.webull_api import QuoteUnavailableError

log = logging.getLogger("webull-bot")


def boost_stalled_positions(
    self,
    positions: list[dict],
    options_active: bool,
    core_session_active: bool = False,
) -> None:
    """Free capital stuck in a stalled position at breakeven-plus-a-penny.

    This is capital hygiene, not a turnover target: it never sells at a
    loss and only fires on a position whose OWN last order activity is
    stale, so a position isn't held indefinitely waiting on a stalled
    quote. Deliberately per-symbol, not one global "has anything filled
    recently" clock - an account that's generally active (new entries
    landing every minute or two) would otherwise never let this run at
    all, even though a specific older position has been sitting
    untouched the whole time.

    Live incident: VZ hit OPENAPI_ORDER_NOT_SUPPORT_REVERSE_OPTION and
    OPENAPI_POSITION_ORDER_INTENT_MISMATCH here - this swept in and
    placed its own SELL_TO_CLOSE while the normal STOP/PROFIT exit
    path already had a resting close order out for the same symbol,
    double-booking against the broker's reserved quantity. pending_
    stock_exits/pending_option_exits only track orders THIS sweep
    itself submitted; has_pending_sell_order checks the actual
    working_orders state regardless of which code path put it there.
    """
    if not self.config.stall_breaker_enabled:
        return
    now = time.monotonic()
    stall_seconds = float(self.config.stall_breaker_seconds)
    if now - self.last_stall_boost < stall_seconds:
        return
    self.last_stall_boost = now
    min_profit = self.config.stall_breaker_min_profit
    boosted = 0
    quote_by_symbol = self._stall_equity_quotes(positions, core_session_active, stall_seconds, now)
    for position in positions:
        quantity = Decimal(str(position.get("quantity", "0")))
        if quantity <= 0:
            continue
        average_cost = Decimal(str(position.get("cost_price") or "0"))
        if average_cost <= 0:
            continue
        symbol = str(position.get("symbol", "")).upper()
        instrument_type = position.get("instrument_type")
        try:
            if instrument_type == "EQUITY":
                if symbol in self.pending_stock_exits:
                    continue
                key = f"STOCK:{symbol}"
                if self.has_pending_sell_order(key):
                    continue
                if not self.cooldown_ready(key):
                    continue
                # This specific symbol's own last order activity, not
                # whether anything else in the account recently
                # filled - see the docstring above.
                if now - self.last_trade.get(key, 0.0) < stall_seconds:
                    continue
                # Same fractional/core-hours constraint as trade_stocks'
                # exits - Webull rejects any order on a non-integer
                # quantity outside core hours, so don't bother trying.
                if (
                    self.is_fractional_quantity(quantity)
                    and not core_session_active
                ):
                    continue
                quote = quote_by_symbol.get(symbol)
                if quote is None:
                    continue
                fee_per_share = self.config.sell_fee_dollars / quantity
                sell_price = self._stall_exit_price(
                    quote, average_cost, min_profit, fee_per_share
                )
                if sell_price is None:
                    continue
                # Same $0.10-$0.999 lot-restricted-band rejection as
                # trade_stocks' exits - Webull rejects any order under
                # 100 shares while price sits in that band, regardless
                # of side or how many shares are actually held.
                if self.strategy.exit_blocked_by_lot_restriction(quantity, sell_price):
                    continue
                order_id = self.api.place_stock(
                    symbol,
                    "SELL",
                    quantity,
                    limit_price=sell_price,
                    fractional=quantity != quantity.to_integral_value(),
                )
                self.pending_stock_exits.add(symbol)
                pnl = self.record_realized_exit(average_cost, sell_price, quantity)
                self.record_trade(
                    key, order_id, "PROFIT", sell_price, pnl=pnl,
                    entry_price=average_cost, quantity=quantity,
                )
                boosted += 1
            elif instrument_type == "OPTION" and options_active:
                if symbol in self.pending_option_exits:
                    continue
                key = f"OPTION:{symbol}"
                if self.has_pending_sell_order(key):
                    continue
                if not self.cooldown_ready(key):
                    continue
                if now - self.last_trade.get(key, 0.0) < stall_seconds:
                    continue
                contract = self.api.contract_from_position(position)
                if not contract:
                    continue
                fee_per_share = (self.config.option_sell_fee_per_contract * quantity) / (quantity * 100)
                quote = self.api.option_quote(contract["symbol"])
                sell_price = self._stall_exit_price(
                    quote, average_cost, min_profit, fee_per_share
                )
                if sell_price is None:
                    continue
                order_id = self.api.place_option(
                    contract,
                    "SELL",
                    quantity,
                    sell_price,
                    "SELL_TO_CLOSE",
                )
                self.pending_option_exits.add(symbol)
                pnl = self.record_realized_exit(average_cost, sell_price, quantity, multiplier=100)
                self.record_trade(
                    key, order_id, "PROFIT", sell_price, pnl=pnl,
                    entry_price=average_cost, quantity=quantity,
                )
                boosted += 1
        except Exception as exc:
            if isinstance(exc, QuoteUnavailableError):
                continue
            if "INVALID_SYMBOL" in str(exc).upper():
                # Live incident (BA): a 2-contract OPTION position's
                # own top-level "symbol" field came back from Webull's
                # positions() as "2BA260925C00220000" - the quantity
                # itself prefixed onto the real OCC symbol, a genuine
                # broker-data artifact (single-contract positions
                # never showed this). contract_from_position trusted
                # it (len > 10, tries exact_option first) and Webull's
                # quote endpoint correctly rejects the malformed
                # string - but that rejection isn't caught internally,
                # so it escaped here as a raw ERROR every stall cycle
                # (~2-3 min) for as long as the position was held.
                # Recognized broker rejection code, same downgrade-and-
                # move-on convention as every other classified
                # rejection this session - not a bug in this process
                # to alarm on repeatedly.
                log.warning(
                    "STALL  | %s | broker returned an unresolvable "
                    "option symbol for this position - skipping this "
                    "cycle | %s", symbol, exc,
                )
                continue
            log.error("STALL  | %s | %s", symbol, exc)
    if boosted:
        self.last_account_refresh = 0.0
    log.info(
        "STALL  | checked %s position(s) idle %ss+ | boosted %s "
        "profitable exit(s)",
        len(quote_by_symbol),
        self.config.stall_breaker_seconds,
        boosted,
    )
