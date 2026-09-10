import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from webull_bot.trading.orders.manual_touch import _manual_touch_active
from webull_bot.trading.stocks.stock_symbol_volatility_scalp import (
    _process_stock_symbol_volatility_scalp_averaging,
    _process_stock_symbol_volatility_scalp_entry,
)
from webull_bot.webull_api import QuoteUnavailableError

log = logging.getLogger("webull-bot")


@dataclass
class _StockScanState:
    """Mutable, per-cycle capital/slot accounting that trade_stocks'
    per-symbol loop reads and mutates across iterations - carried in a
    single object (rather than threaded through as ~7 separate
    parameters/return values) so _process_stock_symbol can mutate it in
    place exactly like the inline loop body used to mutate its own bare
    locals, with no return-and-reassign bookkeeping at each call site.
    Every field is required (no defaults) so a caller that forgets to
    seed one fails loudly at construction instead of silently trading
    against a wrong value.
    """

    buying_power: Decimal
    open_count: int
    bucket_remaining: dict
    bucket_position_counts: dict
    fractional_remaining: Decimal
    whole_share_remaining: Decimal
    fractional_position_count: int


def _process_stock_symbol(
    self,
    symbol: str,
    state: _StockScanState,
    positions: list[dict],
    opening_grace_active: bool,
    core_session_active: bool,
    quote_by_symbol: dict,
    batch_moment: float,
    guard_active: bool,
    regime_gate_active: bool,
    idle_relaxation_multiplier: Decimal,
    idle_relaxation_amount: Decimal,
    profit_target_multiplier: Decimal,
    stop_tighten_multiplier: Decimal,
    fresh_entry_blackout_active: bool,
    effective_core_session_active: bool,
    bucket_slot_limits: dict,
    max_fractional_positions: int,
    volatility_scalp_effective_max_concurrent: int,
    volatility_scalp_effective_max_averaging: int,
    volatility_scalp_intensity: Decimal,
) -> None:
    """One symbol's worth of trade_stocks' per-symbol scan/decision/
    order-placement logic, called once per symbol from trade_stocks'
    `for symbol in batch:` loop. Mutates `state` in place (buying_power,
    open_count, and the bucket/fractional capital accounting) instead of
    returning updated values - see _StockScanState's docstring for why.
    Every `continue` from the original inline loop body became a bare
    `return` here (this function IS one loop iteration's body now, so
    returning early has the exact same effect the original `continue`
    did: skip whatever's left for this symbol and let trade_stocks'
    loop move on to the next one).
    """
    try:
        quote = quote_by_symbol.get(symbol)
        if not quote:
            return
        price = self.api.quote_price(quote)
        self.strategy.update_stock_snapshot(quote, price)
        # By request: micro-exhaustion dip confirmation - see
        # TradingStrategy.volatility_scalp_micro_exhaustion_
        # confirmed. Updated here, once per symbol per real
        # snapshot (not inside the gate-check function itself -
        # see that method's docstring for why a stateful gate
        # would double-count against bot.py's existing
        # diagnostic-visibility tuples). batch_moment is a
        # single time.monotonic() reading shared across this
        # whole batch rather than a fresh one per symbol.
        if price is not None and price > 0:
            self.strategy.update_recent_tick_history(
                symbol, price, batch_moment
            )
        snapshot_volume = self.strategy.metrics.get(symbol, {}).get("volume")
        if snapshot_volume is not None:
            self.strategy.update_volume_delta(
                symbol, Decimal(str(snapshot_volume))
            )
        if self.strategy.is_volatility_scalp_eligible(symbol):
            self.volatility_scalp_recently_eligible.add(symbol)
        else:
            self.volatility_scalp_recently_eligible.discard(symbol)
        # By request: "when i touch a stock stop doing anything
        # with it while i am there." Price/volume tracking above
        # still runs (keeps the dashboard/PnL accurate and the
        # strategy's own state warm for when the pause ends),
        # but every decision/order action below - fresh entries,
        # averaging down, PROFIT/STOP submission - is skipped
        # entirely for manual_touch_pause_seconds after a
        # detected manual buy/sell on this symbol.
        if _manual_touch_active(self, symbol):
            return
        if self.analyst_service is not None:
            self.analyst_service.request(symbol, price)
        quantity, cost = self.api.stock_position(symbol, positions)
        key = f"STOCK:{symbol}"
        opened_at = self.position_opened_at.get(key)
        seconds_since_entry = (
            time.monotonic() - opened_at if opened_at is not None else None
        )
        decision = self.strategy.stock_decision(
            key,
            price,
            quantity,
            cost,
            self.agent_assessment(symbol),
            opening_grace_active,
            idle_relaxation_multiplier,
            idle_relaxation_amount,
            seconds_since_entry,
            effective_core_session_active,
            profit_target_multiplier,
            stop_tighten_multiplier,
        )
        # Condensed onto eligibility alone (any symbol currently
        # volatile enough to qualify - see is_volatility_scalp_
        # eligible), not the narrower curated self.volatility_
        # scalp_symbols cohort list - not just symbols opened via
        # the dip-buy path below, a position already held through
        # the normal trend entry gets the same fast cycling once
        # it qualifies.
        #
        # Live incident (this bug, caught from a real trade
        # log): BTCT averaged down 5 times (its blended cost
        # landed around $1.8494), then stopped out at $1.81 - a
        # 2.1% drop, well inside the 5% hard-stop floor that
        # should have protected it. is_volatility_scalp_
        # eligible is a LIVE, continuously-recalculated stdev
        # check - once several fills naturally calmed the
        # rolling window down below the eligibility threshold,
        # the position instantly lost ALL cohort protection
        # (averaging eligibility AND the hard-stop floor) and
        # fell back to the plain, much tighter adaptive stop.
        # Real capital was already committed across 5 averaging
        # buys - that exposure doesn't shrink just because a
        # transient stdev recalculation dipped under the bar for
        # one cycle. Once a symbol has actually been adopted
        # into cohort management (self.volatility_scalp_
        # positions), it now keeps that treatment for as long as
        # it's held, regardless of whether it's still live-
        # eligible this exact cycle - eligibility still fully
        # gates whether a NEW position gets adopted in the first
        # place, just not whether an existing one keeps its
        # protection.
        if quantity > 0 and (
            self.strategy.is_volatility_scalp_eligible(symbol)
            or symbol in self.volatility_scalp_positions
        ):
            # Live incident (this bug, caught from a real trade
            # log): a position opened via the NORMAL trend-entry
            # path that later became scalp-eligible got this
            # fast quick-profit-take on the way UP (the block
            # above applies unconditionally to any eligible
            # held position), but NOT averaging-down protection
            # on the way down, since self.volatility_scalp_
            # positions only ever got populated by the scalp's
            # OWN dip-buy entry path - so its full, larger
            # adaptive stop-loss stayed active and fired for a
            # real loss several times the size of this cohort's
            # own tiny profit-takes (OSRH -1.14, VBIO -0.86 vs.
            # profits of 0.01-0.06) - heads win small, tails
            # lose big. Auto-adopts ANY held, currently-eligible
            # position into full cohort management the moment
            # it's seen here, regardless of how it was opened,
            # so it gets the SAME averaging-down recovery plan
            # and suppressed stop-loss as a symbol dip-bought by
            # this strategy directly - closing the asymmetry
            # that quick-profit-take alone was blind to.
            self.volatility_scalp_positions.add(symbol)
            # Live incident (VVOS): this used to hardcode
            # averaging_available=True for ANY cohort position,
            # so the hard-stop-floor suppression above always
            # gave a position the FULL 8% of room regardless of
            # whether it could actually still average down.
            # VVOS's averaging-down gate hit "cap reached" after
            # just 1 add (thin buying power on a ~$200 account
            # left almost no room under the 12% per-symbol risk
            # budget - see averaging_down_capacity), yet the
            # exit override kept holding it, unprotected, for
            # another ~19 minutes and several more points of
            # adverse move, all the way to the 8% floor - room
            # that was meant for a DCA ladder that had already
            # stopped adding. Re-derives the SAME capacity check
            # the entry-side averaging gate uses (see the
            # identical calculation below in this method) so the
            # stop suppression only stays in effect while there
            # is still real averaging capacity left; once
            # exhausted, the position falls back to its normal,
            # tighter adaptive stop instead of riding the full
            # DCA-sized floor for no further protection.
            averaging_available = True
            if symbol in self.volatility_scalp_positions:
                estimated_average_down_quantity = self.strategy.volatility_scalp_share_count(
                    price,
                    buying_power=self.cached_buying_power,
                    intensity=Decimal("1"),
                )
                per_buy_risk_dollars = (
                    price
                    * Decimal(estimated_average_down_quantity)
                    * self.config.volatility_scalp_hard_stop_percent
                )
                remaining_capacity = self.strategy.averaging_down_capacity(
                    per_buy_risk_dollars,
                    self.cached_buying_power,
                    self.config.volatility_scalp_max_symbol_risk_fraction,
                    self.config.volatility_scalp_max_averaging_buys,
                )
                averaging_available = (
                    self.volatility_scalp_average_down_count[symbol]
                    < remaining_capacity
                )
            decision = self.strategy.volatility_scalp_exit_override(
                decision,
                quantity,
                cost,
                price,
                averaging_available=averaging_available,
                symbol=symbol,
            )
        if decision.action == "LOSS":
            if symbol not in self.stop_condition_since:
                self.stop_condition_since[symbol] = time.monotonic()
        else:
            self.stop_condition_since.pop(symbol, None)
        if decision.action == "HOLD" and quantity == 0:
            self.gate_rejections[decision.reason] += 1
        if (
            decision.action == "HOLD"
            and quantity != 0
            and decision.reason == "cost basis diverges implausibly from live price"
        ):
            # Rare and serious enough to warrant its own periodic
            # line rather than folding into gate_rejections' entry-
            # side summary - see stock_price_sanity_percent.
            last_warned = self.cost_sanity_warned_at.get(symbol, 0.0)
            now_monotonic = time.monotonic()
            if now_monotonic - last_warned >= 300:
                self.cost_sanity_warned_at[symbol] = now_monotonic
                log.warning(
                    "SANITY | %s | average_cost=%s diverges from "
                    "live price=%s by more than %s%% - broker data "
                    "looks wrong, skipping exit math until it "
                    "recovers",
                    symbol,
                    cost,
                    price,
                    self.config.stock_price_sanity_percent * 100,
                )
        if quantity == 0:
            self.pending_stock_exits.discard(symbol)
            self.stop_exit_submitted.pop(symbol, None)
            self.stop_loss_escalated.discard(symbol)
            self.stop_condition_since.pop(symbol, None)
            self.short_symbols.discard(symbol)
            self.volatility_scalp_positions.discard(symbol)
            self.volatility_scalp_average_down_count.pop(symbol, None)
            self.last_volatility_average_down.pop(symbol, None)
            self.volatility_scalp_last_buy_price.pop(symbol, None)
        _process_stock_symbol_volatility_scalp_entry(
            self,
            symbol,
            state,
            positions,
            quote,
            price,
            quantity,
            key,
            core_session_active,
            regime_gate_active,
            fresh_entry_blackout_active,
            batch_moment,
            volatility_scalp_effective_max_concurrent,
            volatility_scalp_intensity,
        )
        # By request: bound worst-case per-symbol exposure from
        # averaging down (research: "doubling down three times
        # can turn a 7% position into an 18% loss"). Per-symbol,
        # not the flat global cap above - a small account's real
        # risk-fraction limit may bind before the configured
        # ceiling ever would. Estimated at this cycle's would-be
        # buy size/price (cheap, pure, no side effects - the
        # real buy is sized again, identically, below once this
        # gate has already passed).
        _process_stock_symbol_volatility_scalp_averaging(
            self,
            symbol,
            state,
            positions,
            quote,
            price,
            quantity,
            cost,
            key,
            core_session_active,
            regime_gate_active,
            volatility_scalp_effective_max_averaging,
            volatility_scalp_intensity,
        )
        if decision.action == "BUY" and quantity == 0:
            if symbol in self.entry_restricted_symbols:
                return
            blocked_until = self.wash_sales.blocked_until(symbol)
            if blocked_until:
                if symbol not in self.wash_skip_logged:
                    self.wash_skip_logged.add(symbol)
                    log.info(
                        "WASH   | %-8s | entry blocked until %s",
                        symbol,
                        blocked_until.strftime("%Y-%m-%d"),
                    )
                return
            self.wash_skip_logged.discard(symbol)
            if guard_active:
                self.gate_rejections[
                    "stop-loss guard active - too many recent stops"
                ] += 1
                return
            if self.symbol_quarantined(key):
                self.gate_rejections[
                    "symbol quarantined - recent net losses on this symbol"
                ] += 1
                return
            if regime_gate_active:
                self.gate_rejections[
                    "regime gate - VIXY elevated vs recent range"
                ] += 1
                return
            # By request: "regime-dependent" strategy switching -
            # this general EMA-crossover path IS the momentum
            # engine (a fresh cross/continuation signal), so it
            # only opens a FRESH position when this specific
            # symbol is actually trending - a momentum entry
            # into a choppy, range-bound symbol is exactly the
            # mismatched-thesis case research warns against.
            # UNKNOWN (insufficient history) stays eligible,
            # unchanged from before this gate existed - fails
            # open, not closed, same convention as every other
            # gate here.
            if self.strategy.symbol_regime(symbol) == "RANGING":
                self.gate_rejections[
                    "symbol regime is ranging - momentum entry "
                    "skipped, mean-reversion handles it instead"
                ] += 1
                return
            bucket = self.strategy.selection_bucket(symbol)
            # By explicit request: "stop trading extended hours
            # unless it is for closing out positions." Used to
            # allow a fresh entry outside core hours for the
            # POPULAR bucket only ("only trading established
            # stocks... in extended hours") - now a hard block
            # on every fresh entry outside core hours,
            # regardless of bucket. Every exit path (profit
            # target, stop-loss, EOD closeout, the extended-
            # hours profit sweep) is completely unaffected -
            # only opening a brand-new position is blocked here.
            if not effective_core_session_active:
                self.gate_rejections[
                    "extended hours - fresh entries disabled, "
                    "exits only"
                ] += 1
                return
            if fresh_entry_blackout_active:
                self.gate_rejections[
                    "core session closing soon - no new fresh "
                    "entries this close to the bell"
                ] += 1
                return
            # By request ("ensure they do not happen again") - once a
            # symbol has confirmed it has no extended-hours session at
            # all (see handle_otc_extended_hours_unsupported), don't
            # waste another doomed order attempt on it outside core
            # hours - core-hours entries on the same symbol are
            # unaffected.
            if (
                not core_session_active
                and symbol in self.otc_extended_hours_unsupported_symbols
            ):
                self.gate_rejections[
                    "no extended-hours session for this security (OTC)"
                ] += 1
                return
            entry_budget = min(
                state.buying_power,
                state.bucket_remaining.get(bucket, Decimal("0")),
                self.diversification_capped_entry_budget(
                    state.buying_power,
                    self.config.stock_max_position_fraction_of_buying_power,
                    self.config.fractional_shares_min_notional,
                ),
            )
            fractional_supported = symbol not in self.fractional_unsupported_symbols
            buy_quantity, buffered_price, fractional = self.size_stock_entry(
                price,
                entry_budget,
                state.fractional_remaining,
                state.whole_share_remaining,
                core_session_active,
                state.fractional_position_count < max_fractional_positions,
                fractional_supported,
                symbol=symbol,
                buying_power=state.buying_power,
            )
            if (
                buy_quantity == 0
                and self.config.fractional_shares_enabled
                and core_session_active
                and self.fractional_trading_enabled
                and fractional_supported
                and state.fractional_position_count < max_fractional_positions
            ):
                fractional_quantity = self.strategy.fractional_stock_quantity(
                    price,
                    entry_budget,
                )
                if fractional_quantity > 0:
                    buy_quantity = fractional_quantity
                    buffered_price = price * Decimal("1.03")
                    fractional = True
            if (
                state.open_count < self.config.max_open_positions
                and state.bucket_position_counts.get(bucket, 0)
                < bucket_slot_limits.get(bucket, 0)
                and buy_quantity > 0
                and self.cooldown_ready(key)
                and not self.rate_capped(key)
                and self.reentry_cooldown_ready(key)
                and self.price_sanity_cooldown_ready(symbol)
                and self.strategy.obi_supports_entry(
                    self.obi_score_for(
                        symbol,
                        self.stock_categories.get(symbol, "US_STOCK"),
                        quote,
                    )
                )
            ):
                order_id = self.place_stock_scaled(
                    symbol,
                    "BUY",
                    buy_quantity,
                    key,
                    quote,
                    fractional=fractional,
                )
                if order_id is None:
                    return
                buy_limit_price = self.api.stock_limit_price(quote, "BUY")
                self.record_trade(
                    key,
                    order_id,
                    "BUY",
                    buy_limit_price,
                    entry_price=buy_limit_price,
                    quantity=buy_quantity,
                )
                state.buying_power = max(
                    Decimal("0"),
                    state.buying_power - buffered_price * buy_quantity,
                )
                state.bucket_remaining[bucket] = max(
                    Decimal("0"),
                    state.bucket_remaining.get(bucket, Decimal("0"))
                    - buffered_price * buy_quantity,
                )
                if fractional:
                    state.fractional_remaining = max(
                        Decimal("0"),
                        state.fractional_remaining - buffered_price * buy_quantity,
                    )
                    state.fractional_position_count += 1
                else:
                    state.whole_share_remaining = max(
                        Decimal("0"),
                        state.whole_share_remaining - buffered_price * buy_quantity,
                    )
                self.position_buckets[symbol] = bucket
                state.bucket_position_counts[bucket] += 1
                positions.append(
                    {
                        "instrument_type": "EQUITY",
                        "symbol": symbol,
                        "quantity": str(buy_quantity),
                    }
                )
                state.open_count += 1
        if decision.action == "SHORT" and quantity == 0:
            if symbol in self.entry_restricted_symbols:
                return
            blocked_until = self.wash_sales.blocked_until(symbol)
            if blocked_until:
                if symbol not in self.wash_skip_logged:
                    self.wash_skip_logged.add(symbol)
                    log.info(
                        "WASH   | %-8s | short entry blocked until %s",
                        symbol,
                        blocked_until.strftime("%Y-%m-%d"),
                    )
                return
            self.wash_skip_logged.discard(symbol)
            if self.symbol_quarantined(key):
                self.gate_rejections[
                    "symbol quarantined - recent net losses on this symbol"
                ] += 1
                return
            if regime_gate_active:
                self.gate_rejections[
                    "regime gate - VIXY elevated vs recent range"
                ] += 1
                return
            if guard_active:
                self.gate_rejections[
                    "stop-loss guard active - too many recent stops"
                ] += 1
                return
            # Same regime gate as the BUY entry path above -
            # SHORT is the momentum engine's other direction.
            if self.strategy.symbol_regime(symbol) == "RANGING":
                self.gate_rejections[
                    "symbol regime is ranging - momentum entry "
                    "skipped, mean-reversion handles it instead"
                ] += 1
                return
            bucket = self.strategy.selection_bucket(symbol)
            # Same "established/popular only" restriction as the
            # BUY entry gate above, for the same reason.
            if not effective_core_session_active and bucket != "POPULAR":
                self.gate_rejections[
                    "extended hours - only established/popular "
                    "symbols trade outside core hours"
                ] += 1
                return
            if fresh_entry_blackout_active:
                self.gate_rejections[
                    "core session closing soon - no new fresh "
                    "entries this close to the bell"
                ] += 1
                return
            # By request ("ensure they do not happen again") - once a
            # symbol has confirmed it has no extended-hours session at
            # all (see handle_otc_extended_hours_unsupported), don't
            # waste another doomed order attempt on it outside core
            # hours - core-hours entries on the same symbol are
            # unaffected.
            if (
                not core_session_active
                and symbol in self.otc_extended_hours_unsupported_symbols
            ):
                self.gate_rejections[
                    "no extended-hours session for this security (OTC)"
                ] += 1
                return
            entry_budget = min(
                state.buying_power,
                state.bucket_remaining.get(bucket, Decimal("0")),
                self.diversification_capped_entry_budget(
                    state.buying_power,
                    self.config.stock_max_position_fraction_of_buying_power,
                    self.config.fractional_shares_min_notional,
                ),
            )
            # Whole-share sizing only - Webull's fractional-share
            # trading is a long-only retail feature, there's no
            # confirmed fractional short order type.
            short_quantity, buffered_price = self.strategy.stock_order_quantity(
                price, entry_budget
            )
            if (
                self.short_selling_supported
                and state.open_count < self.config.max_open_positions
                and state.bucket_position_counts.get(bucket, 0)
                < bucket_slot_limits.get(bucket, 0)
                and short_quantity > 0
                and self.cooldown_ready(key)
                and not self.rate_capped(key)
                and self.reentry_cooldown_ready(key)
                and self.price_sanity_cooldown_ready(symbol)
                and self.strategy.obi_supports_entry(
                    self.obi_score_for(
                        symbol,
                        self.stock_categories.get(symbol, "US_STOCK"),
                        quote,
                    )
                )
            ):
                order_id = self.place_stock_scaled(
                    symbol,
                    "SHORT",
                    short_quantity,
                    key,
                    quote,
                )
                if order_id is None:
                    return
                short_limit_price = self.api.stock_limit_price(quote, "SHORT")
                self.record_trade(
                    key,
                    order_id,
                    "SHORT",
                    short_limit_price,
                    entry_price=short_limit_price,
                    quantity=short_quantity,
                )
                # Not exact margin accounting (Webull's actual short
                # margin requirement isn't modeled here) - same
                # rough capital-pool tracking the rest of this
                # function already uses, just enough to stop
                # multiple candidates in one cycle from each
                # believing they have the full stale buying_power.
                state.buying_power = max(
                    Decimal("0"),
                    state.buying_power - buffered_price * short_quantity,
                )
                state.bucket_remaining[bucket] = max(
                    Decimal("0"),
                    state.bucket_remaining.get(bucket, Decimal("0"))
                    - buffered_price * short_quantity,
                )
                self.position_buckets[symbol] = bucket
                self.short_symbols.add(symbol)
                state.bucket_position_counts[bucket] += 1
                positions.append(
                    {
                        "instrument_type": "EQUITY",
                        "symbol": symbol,
                        "quantity": str(-short_quantity),
                    }
                )
                state.open_count += 1
        is_short_position = quantity < 0
        exit_quantity = -quantity if is_short_position else quantity
        exit_side = "BUY" if is_short_position else "SELL"
        if (
            decision.action == "PROFIT"
            and symbol not in self.pending_stock_exits
            and self.cooldown_ready(key)
        ):
            # A fractional-quantity position (bought via the core-
            # session dollar-sizing path) can only be bought OR
            # sold during core hours - Webull rejects any order on
            # a non-integer quantity outside core hours regardless
            # of order type. Retrying every cycle just spams the
            # same rejection until the next core session, so skip
            # (and count it like any other gate) instead. Shorts
            # are always whole-share (see the SHORT entry branch
            # above), so this is effectively a long-only check.
            exit_is_fractional = self.is_fractional_quantity(exit_quantity)
            if exit_is_fractional and not core_session_active:
                self.gate_rejections[
                    "fractional position - exit waits for core hours"
                ] += 1
                return
            # Webull rejects ANY order (entry or exit, either side)
            # under 100 shares while price sits in $0.10-$0.999,
            # regardless of how many shares are actually held - a
            # position smaller than that, caught in this band
            # (e.g. price drifted down into it after entry), can't
            # be exited by a normal order at all until price moves
            # back out of the band. Retrying every cycle just spams
            # the same rejection, so skip (and count it) instead.
            if self.strategy.exit_blocked_by_lot_restriction(exit_quantity, price):
                self.gate_rejections[
                    "sub-$1 lot-restricted band - exit waits for "
                    "price to clear it"
                ] += 1
                return
            target = decision.target_price
            if target is None:
                return
            # Live incident (this bug, caught from a real trade
            # log): "PROFIT LEDS entry=2.00 exit=None pnl=-0.01"
            # - a PROFIT-type decision that closed at a real
            # loss with NO limit price at all, because
            # should_force_market_exit had tripped after too
            # many consecutive unfilled attempts and forced an
            # actual, completely unprotected MARKET order. This
            # is the same class of bug already fixed for the
            # escalation (stop_loss_escalated) pathway just
            # below - correct for a genuine stop-loss (guarantee
            # execution even at a worse price bounds the loss),
            # backwards for a profit-take (forces a fill "at any
            # price," converting an intended profit into a
            # guaranteed loss). PROFIT exits never force-market -
            # a symbol that genuinely can't get a profitable
            # fill just keeps waiting (the elif/else branches
            # below already skip via `continue` when nothing
            # fillable-and-profitable exists), same as this
            # cohort's own "average down instead of forcing a
            # bad exit" philosophy. should_force_market_exit
            # stays fully in effect for the separate STOP-loss
            # exit path elsewhere, where it's correct.
            force_market = False
            if is_short_position:
                bid = self.api.quote_bid(quote)
                # Mirror of the long case below: never cover above
                # the target that triggered this, but also don't
                # rest the limit above the current bid (that would
                # be paying more than the market for no reason).
                limit_price = (
                    self.api.stock_limit_price(quote, "COVER")
                    if symbol in self.stop_loss_escalated
                    else (min(bid, target) if bid else target)
                )
            elif (
                self.strategy.is_volatility_scalp_eligible(symbol)
                or symbol in self.volatility_scalp_positions
            ):
                # Condensed onto eligibility alone, not the
                # narrower curated self.volatility_scalp_symbols
                # cohort list - see the fresh-entry block above.
                # Also keeps this pricing for an already-adopted
                # cohort position even if it's no longer live-
                # eligible this exact cycle (same reasoning as
                # the exit-override gate above) - a position
                # with real capital committed across several
                # averaging buys shouldn't silently downgrade to
                # the tight-spread-only general pricing right
                # when it most needs the wider-spread-tolerant
                # exit logic to find a fillable price.
                # By request: "the sell price has to be
                # reasonable" - live incident, GAUZ. Resting at
                # the raw ask isn't actually "reasonable" on a
                # wide-spread penny stock (GAUZ: bid=0.40,
                # ask=0.43, a 7.5% spread) - that's the top of
                # the book, not a price anyone's actually buying
                # at, so the order just sits unfilled the same
                # way a fixed target did. Reuses _stall_exit_
                # price's already-correct logic instead: takes
                # the bid immediately if it alone clears cost (a
                # real, guaranteed-fill profit), only falls back
                # to resting at the ask if the bid doesn't clear
                # but the ask does AND the spread itself isn't
                # absurdly wide (same spread-sanity check the
                # stall-breaker already uses). Skips the cycle
                # entirely (continue) rather than resting at an
                # unreliable price if neither holds.
                if exit_is_fractional:
                    fee_per_share = self.config.sell_fee_dollars
                else:
                    fee_per_share = (
                        self.config.sell_fee_dollars / exit_quantity
                    )
                min_profit = cost * self.config.volatility_scalp_target_percent
                # Live incident (this bug, caught from a real
                # trade log): "PROFIT"-labeled orders were
                # closing at a REAL LOSS (LSTA: bought $1.59,
                # sold $1.50; GOAI: bought $2.52, sold $2.42
                # twice) - the escalation path used to switch to
                # self.api.stock_limit_price(quote, "SELL"), a
                # raw aggressive-cross price with NO floor at
                # cost at all, once a symbol sat in self.
                # stop_loss_escalated (15s unfilled). That
                # tradeoff is correct for a genuine stop-loss
                # (guarantee execution even at a worse price
                # bounds the loss), but backwards for a
                # PROFIT-take order - forcing a fill "at any
                # price" converts an intended profit into a
                # guaranteed loss, defeating the entire purpose
                # of the order. ALWAYS use _stall_exit_price now,
                # escalated or not - it already tries harder to
                # find a fillable price (bid first, wider-
                # tolerance ask fallback) without ever dropping
                # below cost + min_profit + fee; if truly nothing
                # fillable-and-profitable exists, the position
                # just keeps waiting (continue below), which is
                # exactly this cohort's own "average down
                # instead of forcing a bad exit" philosophy.
                limit_price = self._stall_exit_price(
                    quote,
                    cost,
                    min_profit,
                    fee_per_share,
                    max_spread_percent=(
                        self.config.volatility_scalp_max_exit_spread_percent
                    ),
                )
                if limit_price is None:
                    self.gate_rejections[
                        "volatility scalp - no reasonably fillable "
                        "profit price available yet"
                    ] += 1
                    return
            else:
                ask = self.api.quote_ask(quote)
                # ask can be below target - or even below cost - if
                # the decision fired off a last-trade print
                # (quote_price) that's already stale relative to
                # the current book (the market moved down between
                # the two reads). Never let a "profit-take"
                # actually price below the target that triggered
                # it, or it can silently execute at a real loss
                # while still being logged as PROFIT.
                #
                # Same live incident/fix as the volatility-scalp
                # branch above (LSTA/GOAI closed at a real loss
                # while logged PROFIT): this comment already
                # described the intended behavior correctly, but
                # the code contradicted it - once escalated
                # (self.stop_loss_escalated, 15s unfilled), it
                # switched to a raw aggressive-cross price with
                # NO floor at target/cost at all. Escalation
                # should mean "try harder to fill," not "give up
                # on price entirely" for an order whose whole
                # purpose is realizing a profit. max(ask, target)
                # unconditionally now - if the market genuinely
                # can't offer a fillable price at or above
                # target, the resting order (or the normal
                # reprice_resting_exits cadence, which has its
                # own "never chase below cost" guard) keeps
                # waiting instead of forcing a loss.
                limit_price = max(ask, target) if ask else target
            # Live incident (WNW): a PROFIT exit escalated 5
            # times over 6+ minutes, every attempt resubmitted
            # at the EXACT same unfillable limit price (the
            # bid/ask never actually crossed it) - "never
            # force-market a profit-take" (see above) correctly
            # avoids converting a stuck profit-take into an
            # unprotected any-price market order, but had no
            # give-up threshold at all, so it can wait forever
            # even once it's clearly not a temporary stall. Also
            # blocked the user's own manual sell override the
            # whole time (see _manual_sell's "exit already
            # pending" skip). By request: "it should be sold,
            # maybe lower than the margin" - once
            # should_force_market_exit's SAME threshold used for
            # genuine stop-losses trips (consecutive_exit_
            # failures, incremented once per escalation - see
            # escalate_stalled_stop_losses), fall back to the
            # current bid: still a real, currently-executable
            # price (not a blind market order that could print
            # far worse on a thin/illiquid name), just no longer
            # gated on clearing the profit target.
            if (
                not is_short_position
                and self.should_force_market_exit(
                    symbol, exit_is_fractional, core_session_active
                )
            ):
                bid = self.api.quote_bid(quote)
                if bid:
                    limit_price = bid
            if force_market:
                log.warning(
                    "ORDER  | %s | never filled %s times in a row - "
                    "forcing a market order to end the loop",
                    symbol,
                    self.consecutive_exit_failures.get(symbol, 0),
                )
            # Live incident (this bug): exits submitted here via
            # the raw API call had NO fat-finger protection at
            # all - price_sanity_ok (and its cooldown backoff)
            # only ever got checked by callers going through
            # place_stock_scaled, which entries use but exits
            # never did. Cooldown checked FIRST (not after) so a
            # symbol already known to be failing this check
            # doesn't keep re-attempting and re-logging every
            # single cycle - the exact "570 rejections in one
            # day" pattern this fix targets. force_market is
            # always False for PROFIT now (see the earlier fix
            # removing it from this path), so limit_price is
            # guaranteed a real price here, not None - safe to
            # sanity-check.
            if not self.price_sanity_cooldown_ready(symbol):
                return
            if not self.price_sanity_ok(symbol, price, limit_price):
                self.gate_rejections[
                    "volatility scalp - profit exit price failed "
                    "the sanity check"
                ] += 1
                return
            order_id = self.api.place_stock(
                symbol,
                exit_side,
                exit_quantity,
                limit_price=limit_price,
                fractional=exit_is_fractional,
                market=force_market,
            )
            self.pending_stock_exits.add(symbol)
            self.stop_exit_submitted[symbol] = time.monotonic()
            realized_price = limit_price if limit_price is not None else price
            pnl = self.record_realized_exit(cost, realized_price, quantity)
            self.record_trade(
                key, order_id, "PROFIT", limit_price, pnl=pnl,
                entry_price=cost, quantity=exit_quantity,
            )
        if decision.action == "LOSS" and self.stop_ready_to_submit(key, symbol):
            exit_is_fractional = self.is_fractional_quantity(exit_quantity)
            if not self.stop_loss_confirmed(symbol):
                self.gate_rejections[
                    "stop breach not yet confirmed - waiting out a "
                    "possible single-tick wick"
                ] += 1
                return
            if exit_is_fractional and not core_session_active:
                self.gate_rejections[
                    "fractional position - exit waits for core hours"
                ] += 1
                return
            if self.strategy.exit_blocked_by_lot_restriction(exit_quantity, price):
                self.gate_rejections[
                    "sub-$1 lot-restricted band - exit waits for "
                    "price to clear it"
                ] += 1
                return
            # Never price an initial stop-loss at the passive side -
            # unlike a profit-take, a stop needs to fill fast to cap
            # the loss, not rest passively hoping for a better price
            # while the position keeps moving further away from it.
            # stock_stop_exit_price (bid/ask midpoint) balances
            # "don't overshoot the market" against "don't sit
            # unfilled" for either direction - only escalation
            # (after 15s unfilled) should cross the market harder
            # than that.
            force_market = self.should_force_market_exit(
                symbol, exit_is_fractional, core_session_active
            )
            if force_market:
                limit_price = None
                log.warning(
                    "ORDER  | %s | never filled %s times in a row - "
                    "forcing a market order to end the loop",
                    symbol,
                    self.consecutive_exit_failures.get(symbol, 0),
                )
            else:
                limit_price = (
                    self.api.stock_limit_price(
                        quote, "COVER" if is_short_position else "SELL"
                    )
                    if symbol in self.stop_loss_escalated
                    else self.api.stock_stop_exit_price(quote)
                )
            # Cooldown/sanity-check the stop's limit price just like
            # the profit-exit path now does - but only when there IS
            # a limit price. force_market's limit_price=None is a
            # deliberate guaranteed-execution market order (after
            # repeated unfilled attempts) and must never be blocked
            # here - this guard exists to catch a bad LIMIT price,
            # not to second-guess an intentional market order.
            if limit_price is not None:
                if not self.price_sanity_cooldown_ready(symbol):
                    return
                if not self.price_sanity_ok(symbol, price, limit_price):
                    self.gate_rejections[
                        "volatility scalp - stop exit price failed "
                        "the sanity check"
                    ] += 1
                    return
            order_id = self.api.place_stock(
                symbol,
                exit_side,
                exit_quantity,
                limit_price=limit_price,
                fractional=exit_is_fractional,
                market=force_market,
            )
            self.wash_sales.block(symbol, "stop-loss exit submitted")
            self.pending_stock_exits.add(symbol)
            self.stop_exit_submitted[symbol] = time.monotonic()
            realized_price = limit_price if limit_price is not None else price
            pnl = self.record_realized_exit(cost, realized_price, quantity)
            self.record_trade(
                key, order_id, "STOP", limit_price, pnl=pnl,
                entry_price=cost, quantity=exit_quantity,
            )
    except Exception as exc:
        self.stop_loss_escalated.discard(symbol)
        if isinstance(exc, QuoteUnavailableError):
            return
        if self.is_broker_position_conflict(exc):
            self.handle_broker_conflict(symbol, exc)
            return
        if "BUYING_POWER_INSUFFICIENT" in str(exc):
            state.buying_power = Decimal("0")
            log.warning(
                "FUNDS  | %s | buy skipped | insufficient buying power",
                symbol,
            )
            return
        if self.is_fractional_trading_not_enabled(exc):
            self.handle_fractional_trading_not_enabled(exc)
            return
        if self.is_fractional_ticker_unsupported(exc):
            self.handle_fractional_ticker_unsupported(symbol, exc)
            return
        if self.is_short_selling_unsupported(exc):
            self.handle_short_selling_unsupported(exc)
            return
        if self.is_symbol_restricted_to_closing_only(exc):
            self.handle_symbol_restricted_to_closing_only(symbol, exc)
            return
        if self.is_otc_extended_hours_unsupported(exc):
            self.handle_otc_extended_hours_unsupported(symbol, exc)
            return
        log.error("STOCK  | %s | %s", symbol, exc)
