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
    """
    return bool(self.config.focus_mode_enabled)


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

    One-way on purpose: once armed it stays armed for the session
    even if equity dips back under the target. A throttle that
    disarmed on a pullback would re-open size into the exact
    give-back it exists to prevent.
    """
    if not self.config.focus_mode_enabled:
        return
    if total_equity is None or total_equity <= 0:
        return
    self.cached_total_equity = total_equity
    today = self.now().date()
    if self.day_start_equity_date != today:
        self.day_start_equity_date = today
        self.day_start_equity = total_equity
        self.profit_throttle_armed = False
        self.profit_throttle_streak = 0
        log.info(
            "THROTTLE| day-start equity $%s | slowing down at +%s%% "
            "($%s)",
            total_equity.quantize(Decimal("0.01")),
            (self.config.focus_daily_profit_target_fraction * 100).quantize(
                Decimal("0.1")
            ),
            (
                total_equity
                * (Decimal("1") + self.config.focus_daily_profit_target_fraction)
            ).quantize(Decimal("0.01")),
        )
        return
    if self.profit_throttle_armed or not self.day_start_equity:
        return
    target = self.day_start_equity * (
        Decimal("1") + self.config.focus_daily_profit_target_fraction
    )
    if total_equity < target:
        # Any reading back under the target breaks the streak - a
        # transient settlement spike cannot accumulate across the
        # dips between its own occurrences.
        self.profit_throttle_streak = 0
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
        "THROTTLE| armed | equity $%s (+$%s, +%s%%) held above the daily "
        "target for %s consecutive readings - no new entries for the rest "
        "of the session; exits, averaging down, the profit-lock trail and "
        "the EOD close stay active",
        total_equity.quantize(Decimal("0.01")),
        gain.quantize(Decimal("0.01")),
        (gain / self.day_start_equity * 100).quantize(Decimal("0.01")),
        needed,
    )


def new_entries_blocked(self) -> bool:
    """Single read the entry paths share - true once the daily profit
    throttle has armed. Covers fresh entries AND averaging down: both
    add risk, which is the thing being slowed down.
    """
    return bool(self.config.focus_mode_enabled and self.profit_throttle_armed)
