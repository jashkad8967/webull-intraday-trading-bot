import time


def volatility_scalp_reentry_ready(self, key: str) -> bool:
    """A much shorter reentry cooldown than the normal trend-entry
    path's (STOCK_REENTRY_COOLDOWN_SECONDS, 180s default) - the whole
    point of volatility-scalp is cycling the same volatile symbol's
    capital back in as soon as it dips again, not waiting out a
    cooldown sized for a slower, trend-following re-entry.
    """
    elapsed = time.monotonic() - self.last_exit_at.get(key, float("-inf"))
    return elapsed >= float(self.config.volatility_scalp_reentry_cooldown_seconds)
