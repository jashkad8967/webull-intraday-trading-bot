from decimal import Decimal, ROUND_DOWN

from webull_bot.strategy_logic.types import Decision


def volatility_scalp_exit_override(
    self,
    decision: Decision,
    quantity,
    average_cost: Decimal,
    price: Decimal,
    averaging_available: bool = True,
    symbol: str = "",
) -> Decision:
    """Called for any held position currently in the volatility-scalp
    cohort - not just ones opened via the dip-buy path (a position
    already held through the normal trend entry gets the same fast
    profit-take once its symbol is picked into the cohort).

    Two things, by explicit request: lets its own small, fast profit
    target fire the exit earlier than stock_decision's normal
    adaptive target would (promotes a HOLD to PROFIT once price
    clears the quick target) - this always applies; and suppresses a
    LOSS entirely - "focus less on the stop loss" - since a dip on
    this cohort is meant to be averaged into instead of stopped out
    of. average_cost keeps reflecting the broker's own blended cost
    across every averaging buy, so the quick-target sell price
    naturally sits above the new average once one fires, with no
    extra tracking needed here.

    The LOSS suppression only applies when averaging_available is
    True (the position is actually eligible for AutoTrader's
    averaging-buy entry path, i.e. it was opened via the dip-buy
    path in the first place) - sanity-check fix: a position that
    got the fast profit-take purely because its symbol is in the
    cohort, but was never opened by this strategy, has no averaging-
    down recovery plan behind it. Suppressing its stop-loss with
    nothing else backing it up would just leave it bleeding
    indefinitely with no path back to even. Its normal stop-loss
    stays fully in effect instead.

    A THIRD, independent way to reach PROFIT: a Parabolic SAR trend
    reversal (see parabolic_sar_exit_signal), but ONLY once price
    has at least cleared cost - this locks in a reversal early
    rather than waiting for the full quick target, without ever
    turning into a second, backdoor stop-loss (which the LOSS
    suppression above deliberately disables for this cohort).

    A FOURTH, even more eager way to reach PROFIT: by request, "if
    there is a profit and it doesn't seem to be going much higher,
    then sell it off... before the next dip." Recalibrated by a
    later request - "too trigger happy to sell... not capturing the
    profits when it can" - this used to fire on ANY real profit at
    all (even a fraction of a cent) combined with a single stalled
    tick, which was cashing out winners before they had room to
    run. Now requires price to have already covered at least
    VOLATILITY_SCALP_MOMENTUM_STALL_MIN_PROFIT_FRACTION of the full
    distance from cost to the quick target (default 60%) AND a
    stronger two-tick stall confirmation (see
    volatility_scalp_momentum_stalling) before taking the early
    exit. Checked last (after the bigger, slower targets) so a
    position that's still climbing - or hasn't earned enough of the
    move yet - keeps riding toward the larger target instead of
    being cashed out early.
    """
    if quantity <= 0 or average_cost <= 0:
        return decision
    if decision.action == "LOSS" and averaging_available:
        # Research finding (freqtrade's documented DCA pattern,
        # compared against ours after "basically only taking
        # losses" was reported live): a mature DCA implementation
        # NEVER fully suppresses the stop-loss during averaging - it
        # keeps a wide-but-always-active hard stop live from entry,
        # sized to not fight the DCA ladder, specifically as a
        # catastrophic-loss backstop distinct from the per-level
        # re-buy logic. Ours removed the stop-loss entirely instead,
        # leaving an averaging-eligible position with NO risk
        # ceiling until all averaging attempts were exhausted - the
        # likely root cause of realized losses running several
        # times the size of this cohort's own tiny profit-takes.
        # VOLATILITY_SCALP_HARD_STOP_PERCENT (default 5%) restores
        # that backstop: still lets a normal-sized dip average down
        # freely (the 5-level, 0.2%-per-level DCA ladder covers
        # about 1% of adverse movement), but a drop beyond it means
        # a real breakdown, not a normal dip - the actual stop-loss
        # is allowed through instead of being suppressed forever.
        drop = (average_cost - price) / average_cost
        # By request: "these commonly seen patterns should also
        # influence averaging down and stop loss... not just
        # entries and exits." Never LOOSENS this backstop below
        # its configured value (still a real catastrophic-loss
        # floor for a genuinely good setup) - only tightens it when
        # there's POSITIVE evidence (not just missing data, which
        # both checks fail open on) that this dip was never a real
        # statistical extreme in the first place: RSI wasn't
        # actually oversold when it should be, or the real
        # multi-day/week/month bars show a genuine sustained
        # breakdown, not routine chop. A low-quality setup by the
        # same historically-standard reads a fresh entry needs
        # doesn't get to ride the full DCA-ladder-sized floor.
        hard_stop_percent = self.config.volatility_scalp_hard_stop_percent
        if symbol:
            rsi = self.relative_strength_index(symbol)
            rsi_confirms_dip = (
                rsi is None or rsi <= self.config.rsi_oversold_threshold
            )
            if not rsi_confirms_dip or not self.multi_day_momentum_supports_entry(
                symbol
            ):
                hard_stop_percent = hard_stop_percent / Decimal("2")
        if drop < hard_stop_percent:
            return Decision(
                "HOLD",
                "volatility scalp - averaging down instead of stopping out",
                price,
            )
        return decision
    if decision.action != "HOLD":
        return decision
    target = self.volatility_scalp_target_price(average_cost)
    if price >= target:
        return Decision("PROFIT", "volatility scalp quick target reached", target)
    if symbol and price >= average_cost:
        if self.parabolic_sar_exit_signal(symbol, price):
            return Decision(
                "PROFIT", "parabolic SAR trend reversal exit", price
            )
        # By request: "these commonly seen patterns should also
        # influence... entries and exits." RSI overbought is the
        # historically-standard mirror of the oversold entry check
        # above - a real statistical peak, not just a small bounce.
        if self.rsi_overbought_exit(symbol):
            return Decision(
                "PROFIT", "RSI overbought - selling into the peak", price
            )
        min_stall_price = average_cost + (target - average_cost) * (
            self.config.volatility_scalp_momentum_stall_min_profit_fraction
        )
        if price >= min_stall_price and self.volatility_scalp_momentum_stalling(
            symbol, price
        ):
            return Decision(
                "PROFIT",
                "momentum stalling on a profitable position - selling "
                "ahead of the next dip",
                price,
            )
    return decision


def volatility_scalp_average_down_signal(
    self, price: Decimal, average_cost: Decimal, level: int = 0
) -> bool:
    """True when price has dropped enough below the position's OWN
    average cost (not the rolling window's local high, unlike the
    fresh-entry dip signal) - "if they dip a lot after you buy,
    average it out with another buy."

    Structural fix (not a same-day band-aid): compared against
    freqtrade's documented DCA pattern after watching a real
    position (BTCT) burn through its averaging buys at 1.79 -> 1.78
    -> essentially the same price, no real risk-reduction gained
    per add. freqtrade's own docs warn a tight, non-widening re-buy
    trigger "runs out of money" refilling into noise rather than a
    real dip. The required drop now WIDENS with each successive
    averaging level (0-indexed: the first averaging buy still uses
    the base VOLATILITY_SCALP_DIP_ENTRY_PERCENT, the second requires
    1.5x that, the third 2x, etc., via
    VOLATILITY_SCALP_AVERAGING_STEP_MULTIPLIER) - a position that's
    already averaged down several times needs a genuinely bigger
    move to justify yet another add, not just another noise-level
    tick, and the whole averaging ladder now spans a real range
    instead of exhausting itself within ~1% of movement.
    """
    if average_cost <= 0 or price <= 0:
        return False
    drop = (average_cost - price) / average_cost
    required = self.config.volatility_scalp_dip_entry_percent * (
        Decimal("1") + self.config.volatility_scalp_averaging_step_multiplier * level
    )
    return drop >= required


def averaging_down_capacity(
    self,
    per_buy_risk_dollars: Decimal,
    buying_power: Decimal,
    max_symbol_risk_fraction: Decimal,
    max_averaging_buys: int,
) -> int:
    """Bounds how many ADDITIONAL averaging-down buys a single
    symbol can take, on top of its already-configured ceiling
    (max_averaging_buys), so total worst-case exposure to one
    symbol - even fully averaged down and hitting the hard-stop
    floor - can't exceed max_symbol_risk_fraction of buying_power.

    Research finding acted on directly: "doubling down three times
    can turn a 7% position into an 18% loss... in a bad market that
    50% can be 80%." Each volatility-scalp buy (fresh entry and
    every averaging-down add) targets roughly the same per-trade
    notional (see volatility_scalp_share_count), so per_buy_risk_
    dollars (that one buy's notional times the hard-stop-floor
    percent) approximates every subsequent buy's incremental risk
    too - total risk after N total buys is roughly
    N * per_buy_risk_dollars.

    Returns 0 (no more averaging at all) if even the second buy
    (the first averaging-down add) would already breach the
    fraction. This is a CAP on top of max_averaging_buys, not a
    replacement - whichever is smaller wins; a small account's real
    exposure limit may bind well before the configured "5" ever
    would, and that's the point.
    """
    if per_buy_risk_dollars <= 0 or buying_power <= 0:
        return max_averaging_buys
    max_symbol_risk_dollars = buying_power * max_symbol_risk_fraction
    total_buys_affordable = int(max_symbol_risk_dollars / per_buy_risk_dollars)
    # -1 for the initial fresh entry itself, which has already
    # happened and isn't part of "additional averaging capacity."
    capacity = max(0, total_buys_affordable - 1)
    return min(capacity, max_averaging_buys)


def volatility_scalp_partial_exit_quantity(
    self,
    total_quantity: int,
    price: Decimal,
    last_partial_exit_price: Decimal | None,
) -> int:
    """By request: "when buying multiple shares, if needed be able
    to sell them in parts as the value shifts, to maximize
    profits... buy 20, sell 5 every 5 cents it goes up." Returns
    how many shares to sell on THIS profit exit - a shrinking
    ladder of partial slices instead of one all-or-nothing sale.

    Returns total_quantity unchanged (a full, ordinary exit) when:
    partial exits are disabled; there's no prior partial-exit price
    yet AND the computed slice would be too small to leave a
    meaningful remainder; or the remainder after this slice would
    drop to/below the configured floor (VOLATILITY_SCALP_PARTIAL_
    EXIT_MIN_REMAINDER_SHARES) - ends the ladder with one final
    clean sale instead of grinding into odd-lot slivers.

    Returns 0 (sell nothing THIS cycle, decision-caller must treat
    that as "wait, don't submit an order") when a prior partial
    exit already happened and price hasn't moved another
    VOLATILITY_SCALP_PARTIAL_EXIT_REPRICE_PERCENT beyond it yet -
    this is what actually implements "every 5 cents it goes up"
    instead of firing again on the very next 0.25s cycle at
    essentially the same price (the quick target itself doesn't
    move once a position is partially closed, only the held
    quantity shrinks).
    """
    if not self.config.volatility_scalp_partial_exit_enabled or total_quantity <= 0:
        return total_quantity
    if last_partial_exit_price is not None and last_partial_exit_price > 0:
        moved = (price - last_partial_exit_price) / last_partial_exit_price
        if moved < self.config.volatility_scalp_partial_exit_reprice_percent:
            return 0
    partial = int(
        (
            Decimal(total_quantity)
            * self.config.volatility_scalp_partial_exit_fraction
        ).to_integral_value(rounding=ROUND_DOWN)
    )
    remainder = total_quantity - partial
    min_remainder = self.config.volatility_scalp_partial_exit_min_remainder_shares
    if partial <= 0 or remainder <= min_remainder:
        return total_quantity
    return partial


def volatility_scalp_share_count(
    self,
    price: Decimal,
    buying_power: Decimal | None = None,
    intensity: Decimal = Decimal("1"),
) -> int:
    """Dollar-notional-target sizing for the volatility-scalp
    strategy, by request: don't cap every penny stock at a flat 100
    shares or every $1+ stock at a flat small count - size UP toward
    a target notional instead, rounded to a clean lot.

    The target itself is min(VOLATILITY_SCALP_TARGET_NOTIONAL, a
    fraction of the caller's own buying_power) when buying_power is
    passed - a flat dollar target alone doesn't scale with account
    size (live sanity check caught this: on a small account, a
    large flat target gets silently zeroed by the caller's
    affordability check on nearly every attempt, meaning close to
    ZERO trades instead of "high frequency" - see trade_stocks).
    buying_power=None (the caller doesn't have it handy) just uses
    the flat target as-is.

    intensity (0-1, default 1 = full size) further scales the
    target down. AutoTrader.trade_stocks always passes 1 now -
    volatility-scalp entries never fire outside core hours at all
    (by request, after pre-market losses), so there's no dampened-
    intensity case left to apply. The parameter itself stays, since
    the sub-$1 rounding-up behavior below is keyed off it too.

    Under $1, always rounds UP to at least 100 shares regardless of
    how small the target (or intensity) computes to - Webull's own
    lot-restricted-band minimum there (see minimum_lot_size) leaves
    no smaller valid order to fall back to, so intensity dampening
    has no effect on sub-$1 trade size specifically - the caller's
    own affordability/exposure checks are the real backstop on this
    floor, not this function. At $1 and up (no exchange-mandated
    minimum), rounds to the nearest 10 shares when the target
    affords at least one full 10-share lot - "in the tens, if not
    the hundreds" - but degrades to whatever whole-share quantity
    the target actually affords (down to 1) rather than forcing a
    10-share lot a small target can't comfortably support, or
    skipping an otherwise fine smaller trade purely over lot-
    rounding.

    0 (skip) for anything priced at or below zero, above
    VOLATILITY_SCALP_MAX_PRICE, or too small to afford even one
    share of a $1+ stock.
    """
    if price <= 0 or price > self.config.volatility_scalp_max_price:
        return 0
    target_notional = self.config.volatility_scalp_target_notional
    if buying_power is not None and buying_power > 0:
        target_notional = min(
            target_notional,
            buying_power
            * self.config.volatility_scalp_target_notional_buying_power_fraction,
        )
    target_notional *= max(Decimal("0"), min(Decimal("1"), intensity))
    raw_quantity = int(
        (target_notional / price).to_integral_value(rounding=ROUND_DOWN)
    )
    if price < Decimal("1"):
        return max(100, (raw_quantity // 100) * 100)
    if raw_quantity >= 10:
        return (raw_quantity // 10) * 10
    return max(0, raw_quantity)


def volatility_scalp_target_price(self, average_cost: Decimal) -> Decimal:
    """Sell the rip: a small, fixed quick-profit target above cost -
    deliberately not the normal adaptive-stop-scaled target
    (stock_decision's base_target), since the whole point of this
    path is cycling capital fast on a volatile symbol's own natural
    back-and-forth rather than waiting for one bigger move.
    """
    return average_cost * (
        Decimal("1") + self.config.volatility_scalp_target_percent
    )
