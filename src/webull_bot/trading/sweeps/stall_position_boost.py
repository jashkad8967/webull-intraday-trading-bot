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
    considered = 0
    considered_options = 0
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
        considered += 1
        if instrument_type == "OPTION":
            considered_options += 1
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
                # Resolve the CONTRACT before any duplicate check.
                #
                # A Webull option position reports the bare underlying
                # ("GME") in `symbol`, while pending_option_exits and
                # working_orders are keyed by the OCC contract symbol
                # ("GME261009C00024000"). Testing the bare symbol
                # against those meant BOTH guards silently missed, and
                # this sweep submitted a second sell against a position
                # the repricer was already exiting. Live 2026-09-24,
                # repeatedly:
                #
                #   STALL | GME | HTTP 417
                #   OPENAPI_OPTION_LONG_POSITION_MUST_BE_CLOSE_THAN_
                #   SELL_SHORT - "You can not place order in excess of
                #   current holding quantity"
                #
                # The broker rejected it, so it cost nothing this
                # time. It stops being harmless the moment both orders
                # are accepted: two sells against one position leaves
                # the account short a contract it never owned.
                contract = self.api.contract_from_position(position)
                if not contract:
                    continue
                option_symbol = str(contract.get("symbol", "") or "")
                if not option_symbol:
                    continue
                if option_symbol in self.pending_option_exits:
                    continue
                key = f"OPTION:{option_symbol}"
                if self.has_pending_sell_order(key):
                    continue
                if not self.cooldown_ready(key):
                    continue
                if now - self.last_trade.get(key, 0.0) < stall_seconds:
                    continue
                # quantity cancels out algebraically - written this way
                # it only creates a 0/0 DivisionUndefined when a stale
                # snapshot reports a closed position, the same crash
                # that took down the held-option exit path today.
                fee_per_share = (
                    self.config.option_sell_fee_per_contract / 100
                )
                quote = self.api.option_quote(contract["symbol"])
                sell_price = self._stall_exit_price(
                    quote,
                    average_cost,
                    min_profit,
                    fee_per_share,
                    # Options need their OWN spread bound. The default
                    # is stock_entry_max_spread_percent (0.50%), and
                    # option spreads run 2-10% routinely - so the
                    # ask-fallback was skipped on essentially every
                    # option position and the stall breaker could only
                    # ever exit off the bid alone.
                    #
                    # Live 2026-09-23: a MARA put sat +1.67% (+$3.86)
                    # for ~50 minutes and never sold. The trail had
                    # not armed (one cent short of the 2.5% bar), the
                    # take-profit needs 10%, and the one mechanism
                    # built to bank exactly this kind of small gain was
                    # silently disabled by a bound options can never
                    # satisfy. The docstring warns callers with
                    # deliberately wide-spread positions to pass their
                    # own bound; this one never did.
                    #
                    # option_max_entry_spread_percent is the bound this
                    # codebase already trusts for option liquidity -
                    # a contract we were willing to BUY at that spread
                    # is one we should be willing to rest an exit in.
                    self.config.option_max_entry_spread_percent,
                )
                if sell_price is None:
                    continue
                # Claimed as LATE as possible - right before the
                # irreversible call, after every early-return check has
                # passed. This sweep runs on the scan thread while the
                # 0.5s protection thread evaluates the same contract's
                # exit, and a bare `not in pending_option_exits` test is
                # check-then-act across a broker round-trip, so both can
                # pass and place two SELLs for one position. Claiming
                # here also means no early return needs to hand the
                # claim back. See _claim_option_exit.
                # Claim the CONTRACT, not the bare underlying. The
                # 0.5s protection thread claims option_symbol, so
                # claiming "GME" here collided with nothing and the
                # test-and-set that exists specifically to stop two
                # SELLs for one position never actually fired.
                if not self._claim_option_exit(option_symbol):
                    continue
                try:
                    order_id = self.api.place_option(
                        contract,
                        "SELL",
                        quantity,
                        sell_price,
                        "SELL_TO_CLOSE",
                    )
                except Exception:
                    # Placement failed - release, or this contract is
                    # locked out of every future exit this session.
                    # Must release the SAME key that was claimed.
                    self._release_option_exit(option_symbol)
                    raise
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
    # Counted from the positions actually considered, NOT from
    # quote_by_symbol - that only ever held EQUITY quotes (see
    # _stall_equity_quotes), so an account holding nothing but options
    # logged "checked 0 position(s)" forever while the option branch
    # was in fact running every cycle. Live 2026-09-22 that line read
    # 0 for over two hours with three open option positions, which
    # actively misdirected debugging toward a non-existent bug.
    log.info(
        "STALL  | checked %s position(s) (%s option) idle %ss+ | "
        "boosted %s profitable exit(s)",
        considered,
        considered_options,
        self.config.stall_breaker_seconds,
        boosted,
    )
