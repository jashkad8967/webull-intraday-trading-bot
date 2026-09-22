import logging
from datetime import datetime

log = logging.getLogger("webull-bot")


def _both_directions_blocked(self, symbol: str) -> bool:
    """True when neither a CALL nor a PUT can be opened on this
    underlying because both sides are wash-sale blocked.

    Blocks are scoped to underlying+option_type (see the
    wash_sales.block call in option_entry_exit), so one blocked side
    still leaves a perfectly tradeable strategy - only losing BOTH
    makes a symbol useless to focus mode.
    """
    return bool(
        self.wash_sales.blocked_until(f"{symbol}:CALL")
        and self.wash_sales.blocked_until(f"{symbol}:PUT")
    )


def select_focus_cohort(self, moment: datetime) -> None:
    """By explicit request: "allow a cohort of 5-10 stocks then, that
    all fit the criteria so that there are more options to play with"
    - superseding the original "find one really good volatile stock to
    play with and go all in on that for the day."

    Stage two of the funnel refresh_daily_batch starts - it narrows
    this morning's researched shortlist to the set of names the
    account will trade options on today. Every member clears the SAME
    structural gates the single focus pick used to; the cohort is more
    candidates, not weaker ones. The single-symbol version made the
    whole session contingent on one name happening to have a contract
    inside a narrow affordable band, so one unaffordable pick meant
    zero trades all day (live: GOOGL at ~$790/contract against $363).

    Capital is NOT split across the cohort - see focus_cohort_size's
    own comment for why dividing a small account across 10 slots would
    size every entry to zero. Entries remain first-come-first-served
    on the full balance.

    Runs at focus_lock_time (08:45 CT / 09:45 ET by default) rather
    than at the bell, since the first 15 minutes is where opening-range
    fakeouts concentrate.

    Ranks by the existing priority_score and takes the top
    focus_cohort_size survivors, gated only on what is STRUCTURAL - a
    live price, the same price band and share-volume floor the batch
    itself already enforced, affordability, and not being wash-blocked
    in both directions. Deliberately does NOT re-require a live RVOL/
    volatility reading here (an earlier version did): that duplicated
    what direction the batch was already built on and, in practice,
    produced exactly the failure mode being fixed - after ANY restart,
    volume_delta and volatility_price_history reset to empty, so this
    second gate could sit unsatisfied for many minutes even on a
    perfectly good candidate, leaving the account earning nothing while
    a real setup went untraded. Momentum is not this function's job:
    pressure_supports_entry, rsi_divergence and the direction signal
    already gate every individual option ENTRY once the cohort is
    locked (_evaluate_option_entry) - that is where "by the momentum"
    belongs, checked against a live contract quote instead of a value
    that just reset to zero.

    Contract-level quality (premium floor, chain spread, moneyness, IV
    percentile) is likewise not re-checked here for the same reason
    plus the added cost of extra option-chain API calls.

    If nothing clears the structural gates, no cohort is set and the
    bot does not trade options that day.
    """
    if not self.config.focus_mode_enabled:
        return
    if moment < self.session_moment(moment, self.config.focus_lock_time):
        return
    if self.focus_cohort_date == moment.date():
        survivors = [
            symbol
            for symbol in self.focus_cohort
            if symbol not in self.focus_symbol_no_chain
            and not _both_directions_blocked(self, symbol)
        ]
        if survivors == self.focus_cohort and survivors:
            # Already locked today and every member is still tradeable.
            return
        if not self.config.focus_repick_when_blocked:
            self.focus_cohort = survivors
            return
        if survivors:
            # Some members dropped out (wash-blocked both ways, or
            # disqualified for having no chain / nothing affordable by
            # ensure_focus_cohort_contracts). Keep trading the rest
            # rather than idling the account, and fall through to
            # backfill from the batch - a name that failed the gates at
            # lock time can clear them later in the session.
            self.focus_cohort = survivors
        elif self.focus_cohort:
            # Live incident ("if discovery failed why is it still on
            # that stock") - the whole cohort is gone. That is a
            # disqualification, not "nothing to do": fall through and
            # rebuild instead of leaving the account with no symbols
            # for the rest of the day. Guarded on the cohort being
            # non-empty so this logs on the transition only - once it
            # is empty, every later cycle would otherwise re-log the
            # same line while the rebuild keeps finding nothing.
            log.info(
                "FOCUS  | every cohort member is disqualified or "
                "wash-blocked - rebuilding from today's batch"
            )
            self.focus_cohort = []
    # Deliberately NOT date-stamped until a cohort is actually locked -
    # a container that starts near focus_lock_time (every mid-session
    # restart) has no price data at all for the first instant, and
    # stamping early would mark the day done before a real pick was
    # ever possible.
    scored: list[tuple[float, str]] = []
    for symbol in self.daily_batch:
        # Confirmed this session to have no discoverable option chain
        # at all - see ensure_focus_cohort_contracts. Permanent for
        # today: a listed chain does not appear mid-session.
        if symbol in self.focus_symbol_no_chain:
            continue
        price = self.strategy.prices.get(symbol)
        if price is None or price <= 0:
            continue
        if _both_directions_blocked(self, symbol):
            continue
        if not (self.config.focus_min_price <= price <= self.config.focus_max_price):
            continue
        metrics = self.strategy.metrics.get(symbol, {})
        if metrics.get("volume", 0) < self.config.popular_stock_min_volume:
            continue
        # By explicit request ("we want affordable options only so
        # the stocks should also be focused like that"): checked here
        # (last, after every free structural gate) rather than only
        # reactively after a lock - admitting an established name whose
        # cheapest contract still exceeds buying power (GOOGL: $790/
        # contract against $363) and then immediately having to
        # disqualify it wastes real trading time. See
        # focus_symbol_is_affordable's own docstring.
        if not self.focus_symbol_is_affordable(symbol):
            continue
        score = self.strategy.priority_score(symbol, self.agent_assessment(symbol))
        scored.append((score, symbol))
    if not scored:
        # Keep retrying while there is still a session left to trade;
        # stop once the option closeout window begins, since a cohort
        # locked then could only be flattened immediately.
        give_up = moment >= self.session_moment(
            moment, self.config.option_eod_close_time
        )
        if give_up:
            self.focus_cohort_date = moment.date()
        if self.focus_logged_empty_date != moment.date():
            self.focus_logged_empty_date = moment.date()
            log.info(
                "FOCUS  | no symbol in today's batch (%s) cleared the "
                "cohort gates - %s",
                ",".join(self.daily_batch) or "empty",
                "not trading options today" if give_up else "will retry",
            )
        return
    scored.sort(key=lambda row: row[0], reverse=True)
    selected = scored[: self.config.focus_cohort_size]
    cohort = [symbol for _, symbol in selected]
    if cohort == self.focus_cohort:
        # Identical to what is already locked - nothing to announce.
        # Scores drift every cycle, so re-logging an unchanged cohort
        # would fill the log with noise.
        return
    verb = "rebuilt" if self.focus_cohort_date == moment.date() else "locked"
    self.focus_cohort = cohort
    self.focus_cohort_date = moment.date()
    log.info(
        "FOCUS  | cohort of %s %s (%s cleared the gates) | %s",
        len(cohort),
        verb,
        len(scored),
        " | ".join(f"{symbol}({score:.1f})" for score, symbol in selected),
    )
