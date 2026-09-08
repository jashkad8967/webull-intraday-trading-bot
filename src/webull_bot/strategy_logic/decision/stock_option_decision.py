from decimal import Decimal

from webull_bot.strategy_logic.types import Decision


def adaptive_stop_percent(
    self, symbol: str, seconds_since_entry: float | None = None
) -> Decimal:
    range_ratio = Decimal(str(self.metrics.get(symbol, {}).get("range_ratio", 0)))
    scaled = range_ratio * self.config.stock_stop_loss_range_multiplier
    percent = max(
        self.config.stock_stop_loss_min_percent,
        min(self.config.stock_stop_loss_max_percent, scaled),
    )
    if (
        self.config.time_aware_stop_enabled
        and seconds_since_entry is not None
        and seconds_since_entry < self.config.time_aware_stop_widen_seconds
    ):
        # Deliberately widens past the normal max - quote noise right
        # at fill shouldn't stop a position out before the strategy's
        # real edge has had a chance to play out. Tightens back to the
        # normal adaptive value once TIME_AWARE_STOP_WIDEN_SECONDS
        # elapses.
        percent *= self.config.time_aware_stop_widen_multiplier
    return percent


def stock_decision(
    self,
    key: str,
    price: Decimal,
    quantity: int,
    average_cost: Decimal,
    assessment: dict | None = None,
    opening_grace_active: bool = False,
    idle_relaxation_multiplier: Decimal = Decimal("1"),
    idle_relaxation_amount: Decimal = Decimal("0"),
    seconds_since_entry: float | None = None,
    core_session_active: bool = True,
    profit_target_multiplier: Decimal = Decimal("1"),
    stop_tighten_multiplier: Decimal = Decimal("1"),
) -> Decision:
    trend = self.trend_signal(key, price)
    symbol = key.split(":", 1)[-1]
    if quantity > 0:
        # The flat per-sell fee doesn't scale with share count, so it
        # has to be converted to a per-share amount before it can be
        # added to a per-share target/breakeven price - otherwise a
        # target hit at exactly the percentage-based price would still
        # net a loss once the flat fee comes out of the actual fill.
        fee_per_share = self.config.sell_fee_dollars / quantity
        # By request: "when we have a certain profit we should
        # also not allow stops to be too low" - stop_tighten_
        # multiplier (<1 once significantly ahead for the day -
        # see AutoTrader.stop_tighten_multiplier) shrinks the
        # stop distance, protecting more of an already-built lead
        # from a reversal. 1 (no change) otherwise. Kept separate
        # from raw_stop_percent (used below for the profit
        # target) so tightening the stop and widening the target
        # are independent effects, not multiplicatively canceling
        # each other out through a shared base value.
        raw_stop_percent = self.adaptive_stop_percent(symbol, seconds_since_entry)
        stop_percent = raw_stop_percent * stop_tighten_multiplier
        # A fractional (core-session dollar-sized) position targets a
        # much smaller move than a whole-share one - it can only be
        # exited during core hours at all (see is_fractional_quantity
        # usage in bot.py), so it should cycle capital quickly within
        # that window (many trades/hour) rather than sit waiting for
        # the same larger move a whole-share position can afford to
        # hold toward across a longer stretch of the day.
        # quantity isn't guaranteed to already be a Decimal here (a
        # plain int is a valid whole-share quantity too) - only
        # Decimal has to_integral_value().
        quantity_decimal = (
            quantity if isinstance(quantity, Decimal) else Decimal(quantity)
        )
        is_fractional = quantity_decimal != quantity_decimal.to_integral_value()
        floor_percent = (
            self.config.stock_min_net_profit_percent
            + self.config.stock_estimated_round_trip_cost_percent
        )
        # A tiny fractional position's fee-per-share (the flat
        # SELL_FEE_DOLLARS spread over a quantity well under 1) is
        # already a meaningfully larger relative cost than a
        # whole-share position's - scaling the target off the
        # adaptive stop on top of that (like whole-share does below)
        # ends up demanding much more absolute price appreciation
        # than "capture the profit quickly" intends. A fractional
        # position's target is just the flat cost-recovery floor -
        # fires on any solidly fee-covered gain - with no additional
        # stop-scaled requirement layered on top of it.
        # By request: "we basically just want to be able to stay in
        # a significant profit until eod" -> "let winners run
        # further before taking profit." profit_target_multiplier
        # (>1 once the day is already significantly ahead - see
        # AutoTrader.profit_target_multiplier) widens the stop-
        # scaled component only, never the flat fractional/cost-
        # recovery floor - a small account's fee-covered floor
        # exists for basic solvency, not something a "run further"
        # mode should ever push out past.
        target_percent = (
            floor_percent
            if is_fractional
            else max(
                floor_percent,
                raw_stop_percent
                * self.config.stock_target_stop_multiple
                * profit_target_multiplier,
            )
        )
        base_target = average_cost * (Decimal("1") + target_percent) + fee_per_share
        stop = average_cost * (Decimal("1") - stop_percent)
        if average_cost > 0 and price <= stop:
            return Decision("LOSS", "percentage stop reached", price)
        if average_cost > 0 and price > 0:
            # Checked here, not before the stop check above: a stop-
            # loss must still fire even when average_cost has drifted
            # a lot from the live price (a real, if large, move over
            # several days is a legitimate reason for a stop to
            # trigger - live incident: AZI, down ~30% since entry, sat
            # completely unprotected because this used to gate the
            # stop check too, not just the target below). Only
            # deriving a PROFIT target from a suspect average_cost is
            # the actual risk (an unreachable target that just churns
            # failed orders forever - see stock_price_sanity_percent).
            divergence = abs(average_cost - price) / price
            if divergence > self.config.stock_price_sanity_percent:
                return Decision(
                    "HOLD",
                    "cost basis diverges implausibly from live price",
                    price,
                )
        bias = self._exit_bias(assessment)
        target = base_target
        if (
            self.config.agent_exit_influence_enabled
            and average_cost > 0
            and bias >= self.config.agent_runner_bias_threshold
        ):
            target = max(
                base_target,
                average_cost * (Decimal("1") + self.config.agent_runner_profit_percent)
                + fee_per_share,
            )
        if average_cost > 0 and price >= target:
            # By explicit request: "when you buy and there is
            # momentum up, only sell when the momentum shifts to
            # down, or when the profit is decreasing... sell during
            # that initial momentum run itself after the buy" - was
            # taking profit the instant price crossed the target,
            # even mid-run, capping every winner at the same fixed
            # size regardless of how much further it kept climbing.
            # Reuses the exact same momentum-stall confirmation
            # already built and tuned for the volatility-scalp
            # cohort's own profit exit (two consecutive ticks
            # failing to make a fresh high - a real plateau, not
            # single-tick noise) - only the BASE target-reached path
            # waits for it; the agent-runner/de-risk overrides below
            # are deliberate, distinct mechanisms and stay immediate.
            # Whole-share only - a fractional position is deliberately
            # designed to cycle capital fast (many trades/hour, see
            # its own target-sizing comment above), and waiting for a
            # momentum stall would work directly against that intent.
            # volatility_scalp_momentum_stalling itself fails CLOSED
            # (False) with no/insufficient price history, which is
            # right for its ORIGINAL caller (an override that should
            # only act on positive evidence) but wrong here, where
            # the default action is "take the profit" and withholding
            # it needs to be the exceptional case - with no real
            # momentum data at all, this must still fire immediately,
            # not hold forever waiting for data that isn't coming.
            price_history = self.volatility_price_history.get(symbol)
            has_momentum_data = price_history and len(price_history) >= 2
            if (
                target > base_target
                or is_fractional
                or not has_momentum_data
                or self.volatility_scalp_momentum_stalling(symbol, price)
            ):
                reason = (
                    "agent runner target reached"
                    if target > base_target
                    else "percentage profit reached"
                )
                return Decision("PROFIT", reason, target)
        if (
            self.config.agent_exit_influence_enabled
            and average_cost > 0
            and bias <= self.config.agent_derisk_bias_threshold
        ):
            breakeven = average_cost * (
                Decimal("1")
                + self.config.stock_estimated_round_trip_cost_percent
            ) + fee_per_share
            if price >= breakeven:
                return Decision(
                    "PROFIT",
                    "agent de-risk lock-in on fading catalyst",
                    breakeven,
                )
        return Decision("HOLD", "position between target and stop", target)
    if quantity < 0:
        # Mirror image of the long-side math above: a short's
        # average_cost is the price it was sold short at, so it
        # profits as price falls and loses as price rises - target/
        # stop are below/above cost respectively, the opposite of a
        # long position's.
        short_quantity = -quantity
        fee_per_share = self.config.sell_fee_dollars / short_quantity
        # See the long-side comment above - raw_stop_percent keeps
        # the target computation independent of stop-tightening.
        raw_stop_percent = self.adaptive_stop_percent(symbol, seconds_since_entry)
        stop_percent = raw_stop_percent * stop_tighten_multiplier
        target_percent = max(
            self.config.stock_min_net_profit_percent
            + self.config.stock_estimated_round_trip_cost_percent,
            raw_stop_percent
            * self.config.stock_target_stop_multiple
            * profit_target_multiplier,
        )
        base_target = average_cost * (Decimal("1") - target_percent) - fee_per_share
        stop = average_cost * (Decimal("1") + stop_percent)
        if average_cost > 0 and price >= stop:
            return Decision("LOSS", "percentage stop reached (short)", price)
        if average_cost > 0 and price > 0:
            # See the mirrored long-side comment above - a stop must
            # still fire on a real, large move; only the profit target
            # below needs protecting from a suspect average_cost.
            divergence = abs(average_cost - price) / price
            if divergence > self.config.stock_price_sanity_percent:
                return Decision(
                    "HOLD",
                    "cost basis diverges implausibly from live price",
                    price,
                )
        bias = self._exit_bias(assessment)
        target = base_target
        if (
            self.config.agent_exit_influence_enabled
            and average_cost > 0
            and bias <= self.config.agent_derisk_bias_threshold
        ):
            # A negative exit_bias means "de-risk/exit now" for a long;
            # for a short that same bearish-catalyst-fading signal is
            # the runner case - a strong bearish thesis still playing
            # out supports holding for a larger move down.
            target = min(
                base_target,
                average_cost * (Decimal("1") - self.config.agent_runner_profit_percent)
                - fee_per_share,
            )
        if average_cost > 0 and price <= target:
            # Mirror of the long-side momentum-stall gate above - a
            # short profits as price falls, so "momentum still
            # running" here means still making fresh LOWS, not
            # highs. Same "no data -> fire immediately, don't hold
            # forever" fallback as the long side.
            price_history = self.volatility_price_history.get(symbol)
            has_momentum_data = price_history and len(price_history) >= 2
            if (
                target < base_target
                or not has_momentum_data
                or self.volatility_scalp_momentum_stalling_short(symbol, price)
            ):
                reason = (
                    "agent runner target reached (short)"
                    if target < base_target
                    else "percentage profit reached (short)"
                )
                return Decision("PROFIT", reason, target)
        if (
            self.config.agent_exit_influence_enabled
            and average_cost > 0
            and bias >= self.config.agent_runner_bias_threshold
        ):
            # A positive exit_bias (bullish catalyst) is the de-risk
            # signal for a short - lock in whatever's there once past
            # breakeven rather than let a reversal erase it.
            breakeven = average_cost * (
                Decimal("1")
                - self.config.stock_estimated_round_trip_cost_percent
            ) - fee_per_share
            if price <= breakeven:
                return Decision(
                    "PROFIT",
                    "agent de-risk lock-in on fading short catalyst",
                    breakeven,
                )
        return Decision("HOLD", "short position between target and stop", target)
    if not self.entry_spread_ok(
        key, opening_grace_active, idle_relaxation_multiplier, core_session_active
    ):
        return Decision("HOLD", "spread too wide to scalp profitably")
    if trend == "SHORT" and self.config.short_selling_enabled:
        if not self.vwap_supports_entry(
            symbol, price, "SHORT", idle_relaxation_multiplier
        ):
            return Decision("HOLD", "price above session VWAP")
        if not self.entry_extension_ok(
            symbol, price, opening_grace_active, "SHORT", idle_relaxation_multiplier
        ):
            return Decision(
                "HOLD", "price already extended near today's low"
            )
        if not self.sma_trend_supports_entry(symbol, price, "SHORT"):
            return Decision(
                "HOLD", "price above the higher-timeframe SMA trend"
            )
        if self.tick_direction_ok(key, "SHORT", idle_relaxation_amount):
            return Decision("SHORT", "EMA bearish entry confirmed")
        return Decision(
            "HOLD", "recent ticks trending against the short entry"
        )
    if not self.vwap_supports_entry(
        symbol, price, "BUY", idle_relaxation_multiplier
    ):
        return Decision("HOLD", "price below session VWAP")
    if not self.entry_extension_ok(
        symbol, price, opening_grace_active, "BUY", idle_relaxation_multiplier
    ):
        return Decision("HOLD", "price already extended near today's high")
    if not self.sma_trend_supports_entry(symbol, price):
        return Decision("HOLD", "price below the higher-timeframe SMA trend")
    if trend == "BUY":
        if self.tick_direction_ok(key, "BUY", idle_relaxation_amount):
            return Decision("BUY", "EMA entry confirmed")
        return Decision(
            "HOLD", "recent ticks trending against the EMA entry"
        )
    if self.research_supports_entry(assessment):
        return Decision(
            "BUY",
            "strong liquid short-horizon research setup",
        )
    return Decision("HOLD", "EMA entry not ready")


def _exit_bias(self, assessment: dict | None) -> Decimal:
    if not assessment:
        return Decimal("0")
    try:
        confidence = Decimal(str(assessment.get("confidence", 0)))
        bias = Decimal(str(assessment.get("exit_bias", 0)))
    except Exception:
        return Decimal("0")
    if confidence < self.config.agent_exit_min_confidence:
        return Decimal("0")
    return max(Decimal("-1"), min(Decimal("1"), bias))


def entry_spread_ok(
    self,
    key: str,
    opening_grace_active: bool = False,
    idle_relaxation_multiplier: Decimal = Decimal("1"),
    core_session_active: bool = True,
) -> bool:
    symbol = key.split(":", 1)[-1]
    spread = self.metrics.get(symbol, {}).get("spread_percent")
    if spread in (None, ""):
        return True
    threshold = self.config.stock_entry_max_spread_percent
    multiplier = idle_relaxation_multiplier
    if opening_grace_active:
        multiplier = max(multiplier, self.config.opening_grace_spread_multiplier)
    # By request: "we do not want intense play in extended hours,
    # but we want play for sure" - modestly loosens (not fully
    # opens) the spread bar outside core hours, since real pre-
    # market liquidity genuinely can't clear the tight core-hours
    # threshold most of the time. Takes the max with any other
    # active multiplier (same convention as opening_grace above),
    # not a separate additive bonus.
    if not core_session_active:
        multiplier = max(
            multiplier, self.config.extended_hours_spread_multiplier
        )
    threshold *= multiplier
    try:
        return Decimal(str(spread)) <= threshold
    except Exception:
        return True


def volatility_scalp_entry_spread_ok(self, symbol: str) -> bool:
    """By request: "make sure the algo plays around in the spread
    while ensuring a profit, or a profitable entry." Entries had NO
    spread-quality check at all - only exits did (_stall_exit_price's
    VOLATILITY_SCALP_MAX_EXIT_SPREAD_PERCENT bound). Buying into an
    absurdly wide spread sets up a losing trade before it even
    starts: the fill happens near the ask/mid, but the very next
    exit still has to clear the SAME wide spread to reach a real
    bid-side profit. Reuses the exit side's own bound for symmetry -
    this cohort's naturally wide (but legitimate, not glitchy)
    spreads should be tradable on both sides equally. True (don't
    block) when spread data isn't available yet, same "no data ->
    don't block" convention as every other entry gate.
    """
    spread = self.metrics.get(symbol, {}).get("spread_percent")
    if spread in (None, ""):
        return True
    try:
        return (
            Decimal(str(spread))
            <= self.config.volatility_scalp_max_exit_spread_percent
        )
    except Exception:
        return True


def entry_extension_ok(
    self,
    symbol: str,
    price: Decimal,
    opening_grace_active: bool = False,
    direction: str = "BUY",
    idle_relaxation_multiplier: Decimal = Decimal("1"),
) -> bool:
    """Block chasing a name that's already sitting at today's high (or,
    for a short, today's low) WHILE it's still actively jumping toward
    it.

    A crossover that only confirms once price is already at the peak
    of a fast spike is buying the top, not the move - require some
    room below today's high before allowing a fresh entry (mirrored:
    a short needs room above today's low, not already chasing the
    bottom). Right after the open, today's high/low is barely
    established yet and gets set/reset constantly, so the grace window
    shrinks the room required (smaller buffer = more lenient) instead
    of dropping the check entirely. idle_relaxation_multiplier does the
    same shrink for the opposite reason - cash sitting idle too long,
    not the opening print.

    By request: "days high only matters when it is a straight jump
    pattern, once it stabilizes it is fine." Only enforced while
    recent_momentum shows the stock is still genuinely racing toward
    that high/low (see entry_extension_jump_momentum_percent) - once
    that recent momentum has cooled, price sitting near the high is a
    stabilized level, not a spike being chased, so the buffer is
    skipped entirely. Fails open (no buffer required) with no recent-
    momentum reading yet, same "no data -> don't block" convention as
    every other gate in this file.
    """
    reference = self.metrics.get(symbol, {}).get(
        "low" if direction == "SHORT" else "high"
    )
    if not reference:
        return True
    try:
        reference_decimal = Decimal(str(reference))
    except Exception:
        return True
    if reference_decimal <= 0:
        return True
    momentum = self.recent_momentum.get(symbol)
    jump_threshold = self.config.entry_extension_jump_momentum_percent
    still_jumping = momentum is not None and (
        momentum <= -jump_threshold
        if direction == "SHORT"
        else momentum >= jump_threshold
    )
    if not still_jumping:
        return True
    extension_percent = self.config.stock_entry_max_extension_percent
    multiplier = idle_relaxation_multiplier
    if opening_grace_active:
        multiplier = max(multiplier, self.config.opening_grace_extension_multiplier)
    extension_percent /= multiplier
    if direction == "SHORT":
        return price >= reference_decimal * (Decimal("1") + extension_percent)
    return price <= reference_decimal * (Decimal("1") - extension_percent)


def research_supports_entry(assessment: dict | None) -> bool:
    if not assessment:
        return False
    return (
        float(assessment.get("confidence", 0)) >= 0.70
        and float(assessment.get("quick_trade_score", 0)) >= 0.70
        and float(assessment.get("symbol_volatility", 0)) >= 0.60
        and float(assessment.get("expected_move_percent", 0)) > 0
        and float(assessment.get("catalyst_strength", 0)) > 0
        and float(assessment.get("liquidity_risk", 1)) <= 0.40
        and float(assessment.get("downside_risk", 1)) <= 0.50
        and int(assessment.get("horizon_minutes", 390)) <= 30
    )


def option_decision(
    self,
    price: Decimal,
    quantity: int,
    average_cost: Decimal,
    days_to_expiration: int,
) -> Decision:
    """Exit-only: entries are now decided externally by
    option_direction_signal/option_entry_confirmed (bot.py calls those
    before ever opening a position), since a call needs a bullish
    underlying and a put needs a bearish one - a single BUY/HOLD signal
    on the option's own premium can't express that distinction.
    """
    if quantity <= 0:
        return Decision("HOLD", "no position")
    # Same flat-fee-to-per-share conversion as stock_decision, but one
    # contract represents 100 shares, so the fee is spread over
    # quantity * 100, not quantity alone.
    fee_per_share = self.config.sell_fee_dollars / (quantity * 100)
    if average_cost > 0 and days_to_expiration <= self.config.option_min_hold_dte:
        # Forced exit regardless of target/stop - theta/gamma accelerate
        # sharply in the final days before expiration, and holding
        # through that isn't a directional bet anymore, it's a coin
        # flip on pin risk.
        reason = f"time decay exit - {days_to_expiration}d to expiration"
        if price > average_cost:
            return Decision("PROFIT", reason, price)
        return Decision("LOSS", reason, price)
    target = average_cost * (
        Decimal("1") + self.config.option_take_profit_percent
    ) + fee_per_share
    stop = average_cost * (
        Decimal("1") - self.config.option_stop_loss_percent
    )
    if average_cost > 0 and price <= stop:
        return Decision("LOSS", "option percentage stop reached", price)
    if average_cost > 0 and price >= target:
        return Decision("PROFIT", "option profit target reached", target)
    return Decision("HOLD", "option waiting for profit", target)


def option_average_down_signal(
    self, price: Decimal, average_cost: Decimal, level: int = 0
) -> bool:
    """Options analog of volatility_scalp_average_down_signal - by
    request: "you can also use averaging down... for options as
    well." Same widening-ladder shape (each successive averaging
    level requires a bigger drop than the last, so a losing
    position can't refill into noise) - options move far more,
    percentage-wise, than stocks, so this uses its own, wider dip/
    step config (option_averaging_down_dip_percent/option_
    averaging_step_multiplier) rather than reusing the stock-side
    volatility-scalp knobs.
    """
    if average_cost <= 0 or price <= 0:
        return False
    drop = (average_cost - price) / average_cost
    required = self.config.option_averaging_down_dip_percent * (
        Decimal("1") + self.config.option_averaging_step_multiplier * level
    )
    return drop >= required
