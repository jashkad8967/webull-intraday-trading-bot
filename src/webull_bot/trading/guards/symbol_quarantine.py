import logging
import time
from decimal import Decimal

log = logging.getLogger("webull-bot")


def symbol_quarantined(self, key: str) -> bool:
    """freqtrade-style LowProfitPairs: same shape as stop_loss_guard_
    active but scoped to one symbol's own recent realized P&L instead
    of an account-wide stop count. A symbol that's been a net loser
    lately pauses new entries on just that symbol - via
    symbol_pnl_history, fed by record_trade on every PROFIT/STOP/
    MANUAL_SELL exit - while every other symbol keeps trading
    normally. Never liquidates the symbol's existing position, if any.
    """
    if not self.config.symbol_quarantine_enabled:
        return False
    now = time.monotonic()
    if now < self.symbol_quarantine_until.get(key, 0.0):
        return True
    history = self.symbol_pnl_history.get(key)
    if not history:
        return False
    lookback = float(self.config.symbol_quarantine_lookback_seconds)
    while history and now - history[0][0] > lookback:
        history.popleft()
    if len(history) < self.config.symbol_quarantine_min_trades:
        return False
    total_pnl = sum((pnl for _, pnl in history), Decimal("0"))
    if total_pnl > -self.config.symbol_quarantine_loss_dollars:
        return False
    self.symbol_quarantine_until[key] = now + float(
        self.config.symbol_quarantine_cooldown_seconds
    )
    log.warning(
        "GUARD  | symbol quarantined | %s | net $%s over %s trades in "
        "the last %ss | pausing entries for %ss",
        key,
        total_pnl,
        len(history),
        self.config.symbol_quarantine_lookback_seconds,
        self.config.symbol_quarantine_cooldown_seconds,
    )
    return True
