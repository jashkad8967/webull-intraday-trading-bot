import logging
import time
from decimal import Decimal

log = logging.getLogger("webull-bot")


def _process_stock_symbol_volatility_scalp_entry(
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
) -> None:
    """Volatility-scalp cohort's fresh dip-buy entry sub-phase of
    _process_stock_symbol - the diagnostic-only per-condition pass, the
    real entry gate, and order placement on a pass. Contains no early
    `return` (the original inline block never `continue`d out of the
    per-symbol loop from here), so it's safe to call as a plain
    statement from _process_stock_symbol without any stop sentinel.
    """
    if (
        quantity == 0
        and core_session_active
        and symbol not in self.volatility_scalp_positions
        and not self.has_pending_buy_order(key)
        and symbol not in self.broker_conflict_symbols
        and symbol not in self.entry_restricted_symbols
        and len(self.volatility_scalp_positions)
        < volatility_scalp_effective_max_concurrent
        and state.open_count < self.config.max_open_positions
        and not regime_gate_active
    ):
        # Diagnostic-only pass, by request after live evidence
        # of zero volatility-scalp entries over a multi-hour
        # window despite individually-eligible candidates
        # existing - unlike the general strategy's gate_
        # rejections, this cohort's own entry gate never
        # recorded WHY a candidate was rejected, out of ~10
        # independently-narrow conditions stacked together
        # (each new one added in a separate request, never
        # tested for their compounding effect together).
        # Purely additive: evaluates the same conditions in
        # the same order as the real gate immediately below
        # and records only the FIRST one that fails - never
        # affects the real gate or submits anything itself.
        for reason, ok in (
            (
                "scalp - core session closing soon",
                not fresh_entry_blackout_active,
            ),
            (
                "scalp - daily volatility window seed still in progress",
                self.volatility_windows_seeded_date == self.resolved_date,
            ),
            (
                "scalp - still in post-stop-loss cooldown",
                self.post_stop_reentry_ready(symbol),
            ),
            (
                "scalp - wash-sale blocked",
                not self.wash_sales.blocked_until(symbol),
            ),
            (
                "scalp - order-submission cooldown",
                self.cooldown_ready(key),
            ),
            (
                "scalp - reentry cooldown",
                self.volatility_scalp_reentry_ready(key),
            ),
            (
                "scalp - price-sanity cooldown",
                self.price_sanity_cooldown_ready(symbol),
            ),
            (
                "scalp - not eligible (stdev/dollar volume)",
                self.strategy.is_volatility_scalp_eligible(symbol),
            ),
            (
                "scalp - spread too wide",
                self.strategy.volatility_scalp_entry_spread_ok(symbol),
            ),
            (
                "scalp - against the daily SMA trend",
                self.strategy.sma_trend_supports_entry(
                    symbol, price, "BUY"
                ),
            ),
            (
                "scalp - below session VWAP",
                self.strategy.volatility_scalp_vwap_supports_entry(
                    symbol, price
                ),
            ),
            (
                "scalp - recent momentum breaking down",
                self.strategy.recent_momentum_supports_entry(
                    symbol, "BUY"
                ),
            ),
            (
                "scalp - no dip/reversal trigger",
                (
                    (
                        self.strategy.volatility_scalp_dip_signal(
                            symbol, price
                        )
                        and self.strategy.volatility_scalp_micro_exhaustion_confirmed(
                            symbol, price, batch_moment
                        )
                    )
                    or self.strategy.heikin_ashi_bullish_reversal_signal(
                        symbol
                    )
                ),
            ),
            (
                "scalp - momentum still falling",
                self.strategy.volatility_scalp_momentum_stalled_or_rising(
                    symbol, price
                ),
            ),
        ):
            if not ok:
                self.gate_rejections[reason] += 1
                break
    if (
        quantity == 0
        # By request, after pre-market losses: "no volatility
        # scalp in extended hours." A hard, unconditional
        # gate - unlike the earlier intensity-dampening
        # approach this replaces, no fresh volatility-scalp
        # entry fires at all outside core hours. Exits and
        # position management for anything already held
        # (opened during core hours, or held over from an
        # earlier session) are completely unaffected - only
        # fresh entries are blocked here.
        and core_session_active
        # By request, after live evidence (WNW/WKHS stopping
        # out shortly after core hours ended): a fresh entry
        # this close to the bell has almost no runway to
        # reach its target before conditions change - see
        # fresh_entry_blackout_active above. Averaging down
        # on an already-open position is unaffected (a
        # separate, later gate) - this only blocks a BRAND
        # NEW commitment.
        and not fresh_entry_blackout_active
        # Condensed onto eligibility alone (any symbol
        # currently volatile enough to qualify), not the
        # narrower curated self.volatility_scalp_symbols
        # cohort list - by request: "trade volatile stocks
        # with high frequency," not just the top handful.
        # is_volatility_scalp_eligible is the real "is this
        # volatile enough" test; self.volatility_scalp_symbols
        # remains a separate, smaller priority list used only
        # for prioritized batch scanning/dashboard display.
        # Live incident: GAUZ compounded into a 200-share
        # position (double the intended fixed 100) after the
        # cohort started being force-scanned every cycle -
        # `quantity == 0` alone isn't enough, since positions
        # only comes from account_state()'s cache (refreshed
        # every ACCOUNT_REFRESH_SECONDS, ~2s), and a second
        # entry could fire against that same stale "flat"
        # snapshot before the first order's fill ever shows
        # up in it. volatility_scalp_positions is updated
        # synchronously, in-process, the instant an order is
        # placed below - a race-free second guard.
        #
        # Live incident (this bug, caught from production
        # logs): with volatility_scalp_reentry_cooldown_
        # seconds zeroed and trade_cooldown_seconds already
        # 0, this in-process set was the ONLY thing standing
        # between one cycle and the next - and the quantity
        # == 0 cleanup block right above discards a symbol
        # from it EVERY cycle the account's cached position
        # snapshot still shows flat, which is true the
        # entire time a resting BUY order hasn't filled yet
        # (quantity genuinely IS 0 - no shares owned, just a
        # pending order). That reopened the exact race this
        # set exists to close: MTNB got 5 separate 100-share
        # BUY orders stacked within ~70s, all at the same
        # price, because the guard was wiped and re-armed
        # every single cycle while the first order just sat
        # resting. self.has_pending_buy_order(key) checks
        # self.working_orders directly instead - true for as
        # long as an uncancelled BUY order for this symbol
        # actually exists, regardless of what the (up to
        # ACCOUNT_REFRESH_SECONDS-stale) position snapshot
        # says - a real fix, not another cooldown.
        and symbol not in self.volatility_scalp_positions
        and not self.has_pending_buy_order(key)
        and symbol not in self.broker_conflict_symbols
        and symbol not in self.entry_restricted_symbols
        and len(self.volatility_scalp_positions)
        < volatility_scalp_effective_max_concurrent
        and state.open_count < self.config.max_open_positions
        # By explicit request: keep buying this cohort's
        # dips continuously, multiple times a minute, EVEN
        # THROUGH a losing stretch - unlike every other
        # entry path, this deliberately does NOT check
        # guard_active (account-wide stop-loss guard),
        # symbol_quarantined (recent-loss pause), or
        # rate_capped (hourly trade cap), since those exist
        # specifically to slow down or pause trading after
        # losses - exactly what this strategy is meant to
        # keep doing anyway. Only cooldown_ready (a cross-
        # order-submission race guard, not a loss-driven
        # pause - trade_cooldown_seconds defaults to 0) and
        # volatility_scalp_reentry_ready (zeroed by request -
        # "orders can be made as frequently as possible
        # without a cooldown") still gate timing.
        #
        # REVERSED, by request ("does the wash sale block
        # actually fulfill its purpose"): a wash-sale block
        # WAS on this same bypass list until today - live
        # evidence showed CLGN stop out and get a wash-sale
        # block written 5 separate times in one session
        # (11:11, 11:56, 12:05, 12:12, 12:58), and every
        # single one was a no-op, since this path never
        # checked it. Re-added as a real gate here - a
        # symbol that just stopped out via THIS path can't
        # be re-bought via THIS path either for wash_sale_
        # block_days. Averaging down on an already-open
        # position is a separate, later gate and is
        # unaffected either way - this only blocks a BRAND
        # NEW fresh entry.
        and not self.wash_sales.blocked_until(symbol)
        and not regime_gate_active
        # Deliberately does NOT check symbol_regime here (a
        # Kaufman-Efficiency-Ratio "trending vs ranging" gate
        # exists and IS applied to the general momentum path
        # below) - live evidence, and a bug, not just a
        # design choice: it produced zero volatility-scalp
        # entries for a full session. It directly contradicts
        # an earlier, more specific, already-documented
        # design decision - volatility_scalp_dip_signal's own
        # docstring explicitly wants to dip-buy a stock
        # "trending hard in one direction all day" (live
        # example: HOWL, up ~100% intraday) - exactly the
        # case a high efficiency ratio (TRENDING) describes.
        # This mean-reversion path's whole thesis is buying
        # pullbacks WITHIN a move, trending or not; only the
        # general EMA-crossover path actually needs to know
        # whether a symbol is trending.
        # By request, after the DAIC incident (3 stop-losses
        # in ~9 minutes on one symbol during a fast decline,
        # erasing the day's gains): unlike every other loss-
        # driven gate above, deliberately kept even for this
        # cohort's "trade through losses" design - it's
        # narrow (pauses only the ONE symbol that just
        # stopped out, not the whole strategy) and doesn't
        # conflict with "keep buying dips through a losing
        # stretch" elsewhere. Without it, nothing stopped an
        # immediate re-entry into the exact same falling
        # knife seconds after being stopped out of it.
        and self.post_stop_reentry_ready(symbol)
        and self.cooldown_ready(key)
        and self.volatility_scalp_reentry_ready(key)
        and self.price_sanity_cooldown_ready(symbol)
        and self.strategy.is_volatility_scalp_eligible(symbol)
        # By request: "make sure the algo plays around in
        # the spread while ensuring a profit, or a
        # profitable entry" - buying into an absurdly wide
        # spread sets up a losing trade before it even
        # starts (the exit still has to clear the same wide
        # spread to reach a real profit).
        and self.strategy.volatility_scalp_entry_spread_ok(symbol)
        # By request, after "why is it selecting stocks at
        # such wrong times, having to sell majority for
        # losses": the scalp entry path never checked the
        # higher-timeframe daily trend at all, unlike the
        # general strategy (which already has this exact
        # filter). It would happily dip-buy a stock in a
        # real, sustained daily downtrend, where each "dip"
        # is just continuation, not a bounce setup - live
        # incident: AIRE averaged down once and stopped out
        # a minute later. Reuses the same sma_trend_
        # supports_entry infrastructure the general strategy
        # already relies on (refreshed once daily from real
        # daily-bar closes, see AutoTrader.refresh_sma_trend)
        # - only lets a dip-buy fire in the direction of (or
        # with no data on) the larger trend, not against it.
        and self.strategy.sma_trend_supports_entry(symbol, price, "BUY")
        # By request, after an end-of-day retrospective ("we
        # just kept buying at the wrong time"): the SMA
        # filter above only catches a MULTI-DAY downtrend -
        # nothing for a stock simply having a bad DAY today
        # specifically, which is what repeated same-day
        # losses on one symbol (BTCT, three times in one
        # session) actually looks like. A stock trading
        # meaningfully below its own session VWAP is real
        # intraday weakness, not just a normal dip.
        and self.strategy.volatility_scalp_vwap_supports_entry(
            symbol, price
        )
        # By request: "look at tickers in the last 10 mins
        # for momentum... to analyze the upcoming trend."
        # The two checks above cover the historical (SMA)
        # and whole-day (VWAP) trend - this completes the
        # chain with the one timeframe in between. Only
        # blocks a fast, real breakdown over the last few
        # minutes (see recent_momentum_supports_entry's own
        # docstring) - a normal, moderate dip still passes.
        and self.strategy.recent_momentum_supports_entry(
            symbol, "BUY"
        )
        # By request: "also include not only short term
        # patterns like 5-10 mins, but also 1 day and 5 day
        # and month." Completes the timeframe chain with
        # real daily-bar-derived 1-day/5-day/~month checks
        # - see multi_day_momentum_supports_entry's own
        # docstring for why it's deliberately more
        # permissive at longer horizons than the short-term
        # checks above.
        and self.strategy.multi_day_momentum_supports_entry(
            symbol, "BUY", price
        )
        # TWO independent, OR'd entry triggers: the dip-buy
        # signal (gated behind micro-exhaustion confirmation
        # below) and a Heikin-Ashi confirmed bullish reversal
        # candle.
        #
        # Live incident (this bug): the Dual-Thrust-style
        # opening-range breakout trigger that used to sit
        # here as a third OR'd path had NO follow-through
        # confirmation at all - unlike dip-signal (gated
        # behind volatility_scalp_micro_exhaustion_confirmed
        # below), it fired the INSTANT price crossed the
        # breakout level, with no check that the breakout
        # actually held. ORBS: bought at $0.9589, one tick
        # before the local peak ($0.9594), then fell ~4.2%
        # from there - a textbook failed breakout, buying
        # right at the top of a run-up that immediately
        # reversed. By explicit request, after confirming
        # this: removed entirely rather than adding a
        # confirmation gate - dip-signal (with its real
        # exhaustion confirmation) and reversal already
        # cover this cohort's real setups.
        and (
            (
                self.strategy.volatility_scalp_dip_signal(symbol, price)
                and self.strategy.volatility_scalp_micro_exhaustion_confirmed(
                    symbol, price, batch_moment
                )
            )
            or self.strategy.heikin_ashi_bullish_reversal_signal(symbol)
        )
        # By request: "we don't want to buy when there is
        # downward momentum... buy when the dip is stalled
        # or at the bottom, or even when the momentum
        # starts to go up." An AND gate on top of all three
        # triggers above, not a fourth alternative - clearing
        # the dip-percent/breakout/HA-reversal bar doesn't
        # matter if price is still actively falling the
        # instant it does.
        and self.strategy.volatility_scalp_momentum_stalled_or_rising(
            symbol, price
        )
    ):
        scalp_quantity = self.strategy.volatility_scalp_share_count(
            price,
            buying_power=state.buying_power,
            intensity=volatility_scalp_intensity,
        )
        if scalp_quantity > 0 and price * Decimal(scalp_quantity) * Decimal(
            "1.03"
        ) > state.buying_power:
            scalp_quantity = 0
        if scalp_quantity > 0 and not self.volatility_scalp_position_value_ok(
            0, scalp_quantity, price
        ):
            scalp_quantity = 0
        if scalp_quantity > 0 and not self.volatility_scalp_total_exposure_ok(
            positions, price * Decimal(scalp_quantity)
        ):
            scalp_quantity = 0
        if scalp_quantity > 0:
            order_id = self.place_stock_scaled(
                symbol,
                "BUY",
                scalp_quantity,
                key,
                quote,
                limit_price_override=self.volatility_scalp_entry_price(
                    quote
                ),
            )
            if order_id is not None:
                self.record_trade(
                    key,
                    order_id,
                    "BUY",
                    entry_price=self.volatility_scalp_entry_price(quote),
                    quantity=scalp_quantity,
                    # Doesn't reset the general strategy's
                    # idle-cash relaxation clock - see
                    # record_trade's docstring note.
                    counts_toward_idle_cash_ramp=False,
                )
                self.volatility_scalp_positions.add(symbol)
                self.volatility_scalp_last_buy_price[symbol] = price
                buffered_price = price * Decimal("1.03")
                state.buying_power = max(
                    Decimal("0"),
                    state.buying_power - buffered_price * scalp_quantity,
                )
                positions.append(
                    {
                        "instrument_type": "EQUITY",
                        "symbol": symbol,
                        "quantity": str(scalp_quantity),
                    }
                )
                state.open_count += 1
                log.info(
                    "SCALP  | %-8s | dip entry | qty=%s | price=%s",
                    symbol,
                    scalp_quantity,
                    price,
                )


def _process_stock_symbol_volatility_scalp_averaging(
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
) -> None:
    """Volatility-scalp cohort's averaging-down sub-phase of
    _process_stock_symbol - computes this cycle's per-symbol averaging
    cap, runs the diagnostic-only per-condition pass, the real
    averaging-down gate, and order placement on a pass. Contains no
    early `return` (the original inline block never `continue`d out of
    the per-symbol loop from here), and volatility_scalp_symbol_
    averaging_cap is used only within this block in the original code,
    so it's safe to call as a plain statement with no stop sentinel or
    value to hand back.
    """
    volatility_scalp_symbol_averaging_cap = volatility_scalp_effective_max_averaging
    if symbol in self.volatility_scalp_positions:
        estimated_average_down_quantity = self.strategy.volatility_scalp_share_count(
            price, buying_power=state.buying_power, intensity=volatility_scalp_intensity
        )
        per_buy_risk_dollars = (
            price
            * Decimal(estimated_average_down_quantity)
            * self.config.volatility_scalp_hard_stop_percent
        )
        volatility_scalp_symbol_averaging_cap = self.strategy.averaging_down_capacity(
            per_buy_risk_dollars,
            state.buying_power,
            self.config.volatility_scalp_max_symbol_risk_fraction,
            volatility_scalp_effective_max_averaging,
        )
        # Diagnostic-only pass, by request to actually see
        # which specific condition is blocking an averaging-
        # down add on a given cycle instead of inferring it
        # from aggregate counts - the fresh-entry gate above
        # already has this (see "scalp - momentum still
        # falling"), this block never did. Purely additive:
        # evaluates the same conditions in the same order as
        # the real gate immediately below and records only
        # the FIRST one that fails - never affects the real
        # gate or submits anything itself.
        for reason, ok in (
            (
                "scalp avgdown - averaging cap reached",
                self.volatility_scalp_average_down_count[symbol]
                < volatility_scalp_symbol_averaging_cap,
            ),
            (
                "scalp avgdown - reentry cooldown",
                (
                    time.monotonic()
                    - self.last_volatility_average_down.get(symbol, 0.0)
                )
                >= float(self.config.volatility_scalp_reentry_cooldown_seconds),
            ),
            (
                "scalp avgdown - not X% below average cost",
                self.strategy.volatility_scalp_average_down_signal(
                    price,
                    cost,
                    level=self.volatility_scalp_average_down_count[symbol],
                ),
            ),
            (
                "scalp avgdown - not below last buy price",
                (
                    symbol not in self.volatility_scalp_last_buy_price
                    or price < self.volatility_scalp_last_buy_price[symbol]
                ),
            ),
            (
                "scalp avgdown - momentum still falling",
                self.strategy.volatility_scalp_momentum_stalled_or_rising(
                    symbol, price
                ),
            ),
            (
                "scalp avgdown - not statistically oversold (RSI)",
                self.strategy.rsi_supports_entry(symbol),
            ),
        ):
            if not ok:
                self.avgdown_gate_rejections[reason] += 1
                break
    if (
        quantity > 0
        # Same "no volatility scalp in extended hours" hard
        # gate as the fresh-entry block above - averaging
        # down is still a new BUY commitment, just against
        # an existing position instead of a flat one.
        and core_session_active
        # symbol in self.volatility_scalp_positions is the
        # real gate here (an open position this strategy
        # itself opened, and is therefore eligible to average
        # down on) - condensed off the narrower curated
        # self.volatility_scalp_symbols cohort list, same as
        # every other gate in this block.
        and symbol in self.volatility_scalp_positions
        # Same fix as the fresh-entry gate above -
        # volatility_scalp_reentry_cooldown_seconds is
        # zeroed by request, so the elapsed-time check just
        # below is a no-op (0 >= 0 is always true one cycle
        # later); has_pending_buy_order is the real guard
        # against stacking a second averaging buy while the
        # first is still resting unfilled.
        and not self.has_pending_buy_order(key)
        and symbol not in self.broker_conflict_symbols
        and symbol not in self.entry_restricted_symbols
        and self.volatility_scalp_average_down_count[symbol]
        < volatility_scalp_symbol_averaging_cap
        # Sanity-check fix: the fresh-entry gate above
        # deliberately still checks regime_gate_active (a
        # market-wide VIXY-spike gate is kept even though
        # this cohort's own loss-driven gates are bypassed),
        # but this averaging-down block had no such check -
        # meaning a market-wide vol spike would block NEW
        # dip-buys while still letting the bot add to an
        # EXISTING losing position, backwards from what a
        # risk-off signal should do.
        and not regime_gate_active
        # In-process, race-free throttle (same reasoning as
        # the fresh-entry guard above) - without it, several
        # cycles within one ACCOUNT_REFRESH_SECONDS window
        # could each independently see the same still-low
        # cost basis and fire a fresh averaging buy before
        # the last one's fill ever updates it.
        and (
            time.monotonic()
            - self.last_volatility_average_down.get(symbol, 0.0)
        )
        >= float(self.config.volatility_scalp_reentry_cooldown_seconds)
        and self.strategy.volatility_scalp_average_down_signal(
            price,
            cost,
            level=self.volatility_scalp_average_down_count[symbol],
        )
        # By request: "when you average down, you buy at a
        # lower price, not the same price." The signal above
        # only checks price against the BLENDED average cost,
        # which a repeated buy at the same price barely
        # moves - so the same price could keep re-qualifying
        # as "X% below average cost" indefinitely without
        # ever making a genuinely new, lower low. Requires
        # strictly lower than the actual price of the last
        # buy (fresh entry or a prior averaging-down) on this
        # symbol.
        and (
            symbol not in self.volatility_scalp_last_buy_price
            or price < self.volatility_scalp_last_buy_price[symbol]
        )
        # Reverses an earlier by-request decision ("not
        # averaging down enough") that deliberately let this
        # block skip the fresh-entry momentum-stall check,
        # on the theory that averaging down should catch a
        # dip "while it's still happening." Live evidence
        # this backfired: CELU averaged down 5 times in ~6
        # minutes and BTCT 6 times in ~35, each add landing
        # at a still-lower price than the one before it,
        # both eventually hard-stopping out and erasing the
        # day's gains (+$6.97 peak -> -$0.26). By request:
        # "average down when the declining momentum ends and
        # wait for the uptrend to sell the stock then" - same
        # stall check fresh entries already use (requires a
        # genuine consecutive decline to have just stopped
        # getting worse, fails open on too little history),
        # applied here too so a level only gets added once
        # THIS specific decline shows signs of stopping,
        # instead of on every strictly-lower tick regardless
        # of whether the fall is still accelerating.
        and self.strategy.volatility_scalp_momentum_stalled_or_rising(
            symbol, price
        )
        # By request: "these commonly seen patterns should
        # also influence averaging down and stop loss...
        # not just entries and exits." Same RSI oversold
        # check fresh entries already require - don't add
        # to a losing position just because price ticked
        # lower, only when that lower price is ALSO a
        # genuine statistical extreme, same historically-
        # standard bar a fresh entry has to clear.
        and self.strategy.rsi_supports_entry(symbol)
    ):
        average_down_quantity = self.strategy.volatility_scalp_share_count(
            price,
            buying_power=state.buying_power,
            intensity=volatility_scalp_intensity,
        )
        # By request: "even it is supposed to avg down it is
        # not executing it" - the real averaging-down GATE
        # (immediately above) has its own AVGDOWN diagnostic,
        # but once the gate passes, THESE three checks can
        # still silently zero the quantity out with no
        # logging at all - a candidate could clear every
        # AVGDOWN condition and still never actually place an
        # order, invisibly. Logged here (not affecting the
        # real zeroing logic itself) so a silent-execution-
        # failure complaint can be diagnosed with real
        # evidence instead of guesswork, same reasoning as
        # the AVGDOWN diagnostic itself.
        if average_down_quantity > 0 and price * Decimal(
            average_down_quantity
        ) * Decimal("1.03") > state.buying_power:
            average_down_quantity = 0
            self.avgdown_gate_rejections[
                "scalp avgdown - sized qty not affordable"
            ] += 1
        if (
            average_down_quantity > 0
            and not self.volatility_scalp_position_value_ok(
                quantity, average_down_quantity, price
            )
        ):
            average_down_quantity = 0
            self.avgdown_gate_rejections[
                "scalp avgdown - would exceed per-symbol "
                "position value cap"
            ] += 1
        if (
            average_down_quantity > 0
            and not self.volatility_scalp_total_exposure_ok(
                positions, price * Decimal(average_down_quantity)
            )
        ):
            average_down_quantity = 0
            self.avgdown_gate_rejections[
                "scalp avgdown - would exceed whole-cohort "
                "exposure cap"
            ] += 1
        if average_down_quantity > 0:
            # By request: "why is average down buying at
            # the top of the spread?" - this used to share
            # volatility_scalp_entry_price (the aggressive,
            # ask-crossing price) with fresh entries. That
            # urgency makes sense for a fresh entry (miss
            # the fill, miss the move entirely), but works
            # directly against averaging down's whole point
            # - improving the blended cost - by paying the
            # single worst available price on every add.
            # The averaging-down signal already requires
            # price to have dropped a real amount below the
            # average cost first (not the same split-second
            # urgency as a fresh momentum entry), so the
            # general strategy's own passive bid/ask
            # midpoint pricing (stock_limit_price) fits
            # better here - falls back to the old
            # aggressive price only if the quote's bid/ask
            # is invalid, so this is never MORE likely to
            # skip a fill than before, just cheaper when it
            # succeeds.
            try:
                average_down_price = self.api.stock_limit_price(
                    quote, "BUY"
                )
            except Exception:
                average_down_price = self.volatility_scalp_entry_price(
                    quote
                )
            order_id = self.place_stock_scaled(
                symbol,
                "BUY",
                average_down_quantity,
                key,
                quote,
                limit_price_override=average_down_price,
            )
            if order_id is not None:
                self.record_trade(
                    key,
                    order_id,
                    "BUY",
                    entry_price=average_down_price,
                    quantity=average_down_quantity,
                    counts_toward_idle_cash_ramp=False,
                )
                self.volatility_scalp_average_down_count[symbol] += 1
                self.last_volatility_average_down[symbol] = (
                    time.monotonic()
                )
                self.volatility_scalp_last_buy_price[symbol] = price
                state.buying_power = max(
                    Decimal("0"),
                    state.buying_power
                    - price * Decimal("1.03") * average_down_quantity,
                )
                log.info(
                    "SCALP  | %-8s | average down | qty=%s | price=%s "
                    "| count=%s",
                    symbol,
                    average_down_quantity,
                    price,
                    self.volatility_scalp_average_down_count[symbol],
                )
            else:
                # place_stock_scaled returns None silently
                # (price-sanity cooldown or a fat-finger
                # check) - same "even it is supposed to avg
                # down it is not executing it" visibility
                # gap as the three checks above.
                self.avgdown_gate_rejections[
                    "scalp avgdown - order placement declined "
                    "(price-sanity check)"
                ] += 1
