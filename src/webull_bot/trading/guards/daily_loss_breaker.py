import logging

log = logging.getLogger("webull-bot")


def handle_daily_loss_breaker(self) -> bool:
    """Halt entries for the rest of the day once realized stop-loss
    exits alone (not counting the expected EOD closeout) add up past
    daily_max_loss_fraction of account equity. The per-position stop
    already bounds any single loss; this bounds how many of those a
    bad day can rack up before the bot stops opening new positions.

    By request, after finding this circuit breaker disabled both by
    code default and on the live host: enabled by default now, and
    the threshold is a fraction of account equity (the researched
    3-5% daily-drawdown convention) instead of a flat dollar amount
    that doesn't scale with account size - $50 used to be 25% of
    this account's equity, nowhere near a real daily limit. Falls
    back to daily_realized_loss never tripping (rather than raising)
    if account value isn't cached yet - same "no data -> don't
    block" convention as every other gate, applied to a circuit
    breaker's own inputs.
    """
    if not self.config.daily_loss_circuit_breaker_enabled:
        return False
    if self.daily_loss_breaker_triggered:
        return True
    if not self.cached_account_value or self.cached_account_value <= 0:
        return False
    max_loss_dollars = (
        self.cached_account_value * self.config.daily_max_loss_fraction
    )
    if self.daily_realized_loss < max_loss_dollars:
        return False
    log.critical(
        "CIRCUIT | DAILY LOSS LIMIT | realized=$%.2f >= limit=$%.2f "
        "(%.0f%% of $%.2f equity) | halting new entries for the "
        "rest of the trading day",
        self.daily_realized_loss,
        max_loss_dollars,
        self.config.daily_max_loss_fraction * 100,
        self.cached_account_value,
    )
    submitted = self.api.close_all_positions(loss_callback=self.wash_sales.block)
    log.warning(
        "CIRCUIT | close orders submitted=%s | entries halted until "
        "tomorrow's session",
        len(submitted),
    )
    self.daily_loss_breaker_triggered = True
    self.last_account_refresh = 0.0
    return True
