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


def select_focus_symbol(self, moment: datetime) -> None:
    """By explicit request: "find one really good volatile stock to
    play with and go all in on that for the day" - and, when this
    first shipped and stalled with nothing locking for 20+ minutes:
    "once you figure out the stock, it should just be scanning
    contracts by the momentum and entering."

    Stage two of the funnel refresh_daily_batch starts - it narrows
    this morning's researched shortlist to the ONE name the whole
    account will trade options on today.

    Runs at focus_lock_time (08:45 CT / 09:45 ET by default) rather
    than at the bell, since the first 15 minutes is where opening-range fakeouts
    concentrate.

    Picks the top-scored survivor of the daily batch, gated only on
    what is STRUCTURAL - a live price, the same price band and share-
    volume floor the batch itself already enforced, and not being
    wash-blocked in both directions. Deliberately does NOT re-require
    a live RVOL/volatility reading here (an earlier version did): that
    duplicated what direction the batch was already built on and, in
    practice, produced exactly the failure mode being fixed - after
    ANY restart, volume_delta and volatility_price_history reset to
    empty, so this second gate could sit unsatisfied for many minutes
    even on a perfectly good candidate, leaving the account earning
    nothing while a real setup went untraded. Momentum is not this
    function's job: pressure_supports_entry, rsi_divergence and the
    direction signal already gate every individual option ENTRY once
    a symbol is locked (_evaluate_option_entry) - that is where "by
    the momentum" belongs, checked against a live contract quote
    instead of a value that just reset to zero.

    Contract-level quality (premium floor, chain spread, moneyness,
    IV percentile) is likewise not re-checked here for the same
    reason plus the added cost of extra option-chain API calls.

    If nothing even clears the structural gates, no focus symbol is
    set and the bot does not trade options that day.
    """
    if not self.config.focus_mode_enabled:
        return
    if moment < self.session_moment(moment, self.config.focus_lock_time):
        return
    if self.focus_symbol_date == moment.date():
        if self.focus_symbol is None:
            # Live incident ("if discovery failed why is it still on
            # that stock") - ensure_focus_symbol_contracts clears
            # focus_symbol (but NOT focus_symbol_date) when a locked
            # symbol turns out to have no discoverable option chain
            # at all (GRML: a 286.7% gapper with no listed options).
            # That is a disqualification, not "nothing to do" - fall
            # through and pick a replacement instead of leaving the
            # account stuck with no symbol for the rest of the day.
            pass
        elif not self.config.focus_repick_when_blocked:
            # Already locked today with a live symbol, and the ONE
            # OTHER sanctioned re-pick (wash-blocked both directions)
            # is turned off - nothing to do.
            return
        elif not _both_directions_blocked(self, self.focus_symbol):
            return
        else:
            log.info(
                "FOCUS  | %s is wash-blocked on both CALL and PUT - "
                "re-picking a replacement for the rest of the session",
                self.focus_symbol,
            )
    # Deliberately NOT date-stamped until a symbol is actually
    # locked - a container that starts near focus_lock_time (every
    # mid-session restart) has no price data at all for the first
    # instant, and stamping early would mark the day done before a
    # real pick was ever possible.
    scored: list[tuple[float, str]] = []
    for symbol in self.daily_batch:
        # Confirmed this session to have no discoverable option chain
        # at all - see ensure_focus_symbol_contracts. Permanent for
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
        # reactively after a lock - locking onto an established name
        # whose cheapest contract still exceeds buying power (GOOGL:
        # $790/contract against $363) and then immediately having to
        # disqualify and re-pick wastes real trading time. See
        # focus_symbol_is_affordable's own docstring.
        if not self.focus_symbol_is_affordable(symbol):
            continue
        score = self.strategy.priority_score(symbol, self.agent_assessment(symbol))
        scored.append((score, symbol))
    if not scored:
        # Keep retrying while there is still a session left to trade;
        # stop once the option closeout window begins, since a symbol
        # locked then could only be flattened immediately.
        give_up = moment >= self.session_moment(
            moment, self.config.option_eod_close_time
        )
        if give_up:
            self.focus_symbol_date = moment.date()
        if self.focus_logged_empty_date != moment.date():
            self.focus_logged_empty_date = moment.date()
            log.info(
                "FOCUS  | no symbol in today's batch (%s) has price "
                "data yet - %s",
                ",".join(self.daily_batch) or "empty",
                "not trading options today" if give_up else "will retry",
            )
        return
    scored.sort(key=lambda row: row[0], reverse=True)
    best_score, best_symbol = scored[0]
    self.focus_symbol = best_symbol
    self.focus_symbol_date = moment.date()
    log.info(
        "FOCUS  | %s locked for the session | score=%.1f | beat %s other "
        "candidate(s): %s",
        best_symbol,
        best_score,
        len(scored) - 1,
        ", ".join(f"{symbol}({row_score:.1f})" for row_score, symbol in scored[1:])
        or "none",
    )
