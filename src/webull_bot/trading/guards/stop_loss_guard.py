import logging
import time

log = logging.getLogger("webull-bot")


def stop_loss_guard_active(self) -> bool:
    """freqtrade-style StoplossGuard: pause NEW entries (stock and
    short, see trade_stocks) if STOP_LOSS_GUARD_TRADE_LIMIT or more
    stop-losses have fired within the trailing STOP_LOSS_GUARD_
    LOOKBACK_SECONDS window - a frequency-based signal that the
    strategy is currently whipsawing in a bad regime, distinct from
    the dollar/equity-based breakers in handle_portfolio_circuit_
    breaker/handle_daily_loss_breaker (which can take much longer to
    trip, and which liquidate everything when they do). This never
    liquidates anything - existing positions keep being managed
    normally; it just declines to add new risk until the recent stop
    rate cools off, then resumes on its own (no restart needed).
    """
    if not self.config.stop_loss_guard_enabled:
        return False
    now = time.monotonic()
    if now < self.stop_loss_guard_until:
        return True
    lookback = float(self.config.stop_loss_guard_lookback_seconds)
    while self.recent_stop_losses and now - self.recent_stop_losses[0] > lookback:
        self.recent_stop_losses.popleft()
    if len(self.recent_stop_losses) < self.config.stop_loss_guard_trade_limit:
        return False
    self.stop_loss_guard_until = now + float(
        self.config.stop_loss_guard_cooldown_seconds
    )
    log.warning(
        "GUARD  | stop-loss guard tripped | %s stops in the last %ss | "
        "pausing new entries for %ss",
        len(self.recent_stop_losses),
        self.config.stop_loss_guard_lookback_seconds,
        self.config.stop_loss_guard_cooldown_seconds,
    )
    return True
