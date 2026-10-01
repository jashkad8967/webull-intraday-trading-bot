import logging
from decimal import Decimal

log = logging.getLogger("webull-bot")


def stock_entries_suspended(self) -> bool:
    """By explicit request ("go all in on that for the day... make
    sure you use all of the capital"): while focus mode is on, the
    multi-symbol stock scalper stops opening NEW positions, so
    nothing competes with the focus symbol for buying power.

    Existing stock positions are untouched - they still exit on
    their own signals, still stop-loss, and still get flattened by
    the end-of-day closeout. This suspends opening, not holding.

    Note this returns True even on a day where no focus symbol ever
    locked. That is deliberate rather than an oversight: the reason
    nothing locked is that nothing cleared the quality bar, and the
    designed response to a weak field is to sit out, not to fall
    back to spraying capital across the scanner's leftovers.

    Now gated on focus_mode_suspends_stock_entries, which defaults to
    False. Reserving the whole account for options only makes sense
    while options can actually be traded; measured 2026-10-01 they
    cannot at this account size - 8.1% round-trip friction against a
    10% stop, and 9 of 10 recorded positions never traded above
    cost+fee on the bid. The cohort's screening is still doing real
    work, so the account now deploys into those same names as SHARES
    (0.5% max spread, 1.8:1 reward:risk) rather than sitting idle
    waiting for a contract the hurdle gate will refuse. See
    focus_mode_suspends_stock_entries for the full arithmetic.

    BUT ONLY INTO THE SCREENED SET, which is the whole point and was
    briefly broken. Live 2026-10-01 10:27, within minutes of shipping
    the flag above: "ORDER | STOCK | BUY | MEDS | limit=4.00" - a $4
    name, below focus_min_price ($10), so neither the daily batch nor
    the cohort could contain it. _build_forced_stock_scan_batch returns
    None when BOTH are empty, and the scanner then rotates over the
    full universe, of which stock_penny_fraction (10%) is deliberately
    sub-$5 names. Allowing entries unconditionally therefore did not
    redirect the account into screened names, it opened it to the
    entire universe on exactly the days the screening had rejected
    everything.

    The paragraph above this one already said so - "the designed
    response to a weak field is to sit out, not to fall back to
    spraying capital across the scanner's leftovers" - and the flag
    shipped anyway. So the condition is an AND on having something
    screened to trade: with a locked cohort the scan batch is forced to
    those names, with only a daily batch it is forced to that (both
    enforce focus_min_price..focus_max_price), and with neither there is
    nothing to resume INTO and the original sit-out stands.
    """
    if not self.config.focus_mode_enabled:
        return False
    if self.config.focus_mode_suspends_stock_entries:
        return True
    return not (self.focus_cohort or self.daily_batch)


def stock_entry_symbol_allowed(self, symbol: str) -> bool:
    """True while this symbol is one the day's screening actually chose.

    stock_entries_suspended answers "may share entries happen at all".
    This answers "may THIS name be bought", and the two are not the same
    question - which is how XOM got bought.

    Live 2026-10-01 10:50: with the cohort locked to [ACN, MRNA, PLTR]
    and the batch [GOOGL, NVDA, AAPL, ACN, MRNA, RIVN, PLTR, GME], the
    bot opened XOM. XOM is in neither. The scan batch is built from
    prioritized_stock_batch over the whole universe UNION
    scan_watch_symbols (seed_popular | agent_popular | user_watchlist),
    and XOM sits in the watchlist - so simply allowing entries re-opened
    the account to 116 watchlist names that cleared no gate that day.

    That position then did real damage despite being flat: $24 of a $85
    account, in a mega-cap whose daily range is about 1% against a
    0.9-1.5% stop, which capped option premium at $0.61 and left the
    whole CALL side of SPY unreachable (0.46/0.47 = 5.4% hurdle, past
    the 5.0% ceiling) on a day the account was meant to trade options.

    Screening on gap, RVOL, volume and spread is the only reason to
    believe a name is worth risk at all. Buying something that cleared
    none of it is not a smaller version of the strategy, it is the
    absence of one.
    """
    if not self.config.focus_mode_enabled:
        return True
    screened = {str(s).upper() for s in self.focus_cohort}
    screened |= {str(s).upper() for s in self.daily_batch}
    if not screened:
        # Nothing cleared the gates today. stock_entries_suspended
        # already sits out in this state; this is deliberate defence in
        # depth, because that guard has now been wrong twice.
        return False
    return str(symbol).upper() in screened


def update_profit_throttle(self, total_equity: Decimal) -> None:
    """By explicit request: "once you hit a certain profit slow down."

    Captures the day's starting equity on the first reading of each
    session, then arms a one-way throttle once the account is up
    focus_daily_profit_target_fraction (5%) on the day.

    Called from write_status_snapshot, which is the single place in
    this codebase that computes equity CORRECTLY - it is the only
    caller that applies the 100x option contract multiplier, the
    omission of which previously made a flat morning look like a $75
    loss. Deriving a second equity figure anywhere else would risk
    reintroducing exactly that bug.

    By explicit request 2026-09-24 - "even if it hits its target it
    should continue trading, it should just not allow losses to go
    below that 5%" - reaching the target is a FLOOR, not a stop sign.
    It used to halt new entries for the rest of the session the moment
    the target was confirmed, which threw away every remaining setup
    of the day. Now the target level is remembered and trading
    continues above it; new risk is only refused once equity falls
    back to that level, which is what actually protects the gain.
    """
    if not self.config.focus_mode_enabled:
        return
    if total_equity is None or total_equity <= 0:
        return
    self.cached_total_equity = total_equity
    today = self.now().date()
    if self.day_start_equity_date != today:
        self.day_start_equity_date = today
        # Persisted and write-once per date: a mid-session restart must
        # not re-baseline this. Live 2026-09-24 it was re-captured
        # SEVEN times, each lower, until the throttle armed on a gain
        # that never happened - see PositionOpenTimeStore's sibling
        # note in DailyPnlTracker.
        self.daily_pnl.record_day_start_equity(total_equity)
        self.day_start_equity = self.daily_pnl.day_start_equity
        self.profit_throttle_armed = False
        self.profit_throttle_streak = 0
        # Report the PERSISTED baseline, not the current equity.
        #
        # This printed total_equity and derived the target from it, so
        # after a mid-session restart it announced today's day-start as
        # whatever the account happened to be worth right then. Live
        # 2026-09-25 it logged "day-start equity $188.72 | slowing down
        # at +5.0% ($198.16)" while daily_pnl.json correctly held
        # $249.70 and the throttle was correctly using a $262.18
        # target. The logic was right and the log was wrong - which is
        # worse than the reverse, because it looked exactly like the
        # day-start persistence fix had failed and sent me to
        # re-investigate a bug that was not there.
        baseline = self.day_start_equity or total_equity
        log.info(
            "THROTTLE| day-start equity $%s | slowing down at +%s%% "
            "($%s)",
            baseline.quantize(Decimal("0.01")),
            (self.config.focus_daily_profit_target_fraction * 100).quantize(
                Decimal("0.1")
            ),
            (
                baseline
                * (Decimal("1") + self.config.focus_daily_profit_target_fraction)
            ).quantize(Decimal("0.01")),
        )
        return
    if not self.day_start_equity:
        return
    target = self.day_start_equity * (
        Decimal("1") + self.config.focus_daily_profit_target_fraction
    )
    if total_equity < target:
        # Any reading back under the target breaks the streak - a
        # transient settlement spike cannot accumulate across the
        # dips between its own occurrences.
        self.profit_throttle_streak = 0
        if self.profit_throttle_armed:
            # The banked gain has eroded back to the floor. THIS is
            # where new risk stops: not on the way up through the
            # target, but on the way back down to it.
            if not self.profit_floor_breached:
                self.profit_floor_breached = True
                log.warning(
                    "THROTTLE| equity $%s fell back to the +%s%% floor "
                    "($%s) - no new entries; exits, the profit-lock "
                    "trail and the EOD close stay active",
                    total_equity.quantize(Decimal("0.01")),
                    (
                        self.config.focus_daily_profit_target_fraction * 100
                    ).quantize(Decimal("0.1")),
                    target.quantize(Decimal("0.01")),
                )
        return
    # Above the floor again - trading resumes. Deliberately two-way
    # here, unlike the old one-way halt: the floor protects the gain,
    # and there is no reason to sit out the rest of a session the
    # account is winning.
    if self.profit_floor_breached:
        self.profit_floor_breached = False
        log.info(
            "THROTTLE| equity $%s back above the +%s%% floor - entries "
            "re-enabled",
            total_equity.quantize(Decimal("0.01")),
            (
                self.config.focus_daily_profit_target_fraction * 100
            ).quantize(Decimal("0.1")),
        )
    if self.profit_throttle_armed:
        return
    self.profit_throttle_streak += 1
    needed = self.config.profit_throttle_confirm_readings
    if self.profit_throttle_streak < needed:
        # Not confirmed yet. Deliberately quiet at INFO - a real
        # target crossing logs once, below, and a phantom spike
        # should not announce itself at all.
        log.debug(
            "THROTTLE| equity $%s is above target but unconfirmed "
            "(%s/%s consecutive readings)",
            total_equity.quantize(Decimal("0.01")),
            self.profit_throttle_streak,
            needed,
        )
        return
    self.profit_throttle_armed = True
    gain = total_equity - self.day_start_equity
    log.info(
        "THROTTLE| daily target reached | equity $%s (+$%s, +%s%%) held "
        "for %s consecutive readings - this level is now a FLOOR: "
        "trading continues, and new entries stop only if equity falls "
        "back to it",
        total_equity.quantize(Decimal("0.01")),
        gain.quantize(Decimal("0.01")),
        (gain / self.day_start_equity * 100).quantize(Decimal("0.01")),
        needed,
    )


def new_entries_blocked(self) -> bool:
    """Single read the entry paths share. Covers fresh entries AND
    averaging down: both add risk, which is the thing being stopped.

    By explicit request 2026-09-24 - "even if it hits its target it
    should continue trading, it should just not allow losses to go
    below that 5%" - this is no longer true merely because the daily
    target was REACHED. Hitting the target used to halt entries for
    the remainder of the session, which surrendered every setup left
    in the day at exactly the point the account was doing well.

    It is now true only once a reached target has been GIVEN BACK:
    equity ran to +5%, then fell back to that level. That is the
    moment adding risk threatens the banked gain, and it is two-way -
    recovering above the floor re-enables entries.
    """
    # Hard halt first, and independent of focus mode: when this is set
    # nothing new opens at all. Exits, the stop, the profit-lock trail,
    # the stale exit and the EOD close are all downstream of this and
    # stay active - see option_entries_halted.
    if self.config.option_entries_halted:
        return True
    return bool(
        self.config.focus_mode_enabled
        and self.profit_throttle_armed
        and self.profit_floor_breached
    )
