import time


def post_stop_reentry_ready(self, symbol: str) -> bool:
    """False while symbol is still within
    volatility_scalp_post_stop_cooldown_seconds of its last STOP-loss
    exit. By request, after the DAIC incident: 3 stop-losses in
    ~9 minutes on one symbol during a fast decline, erasing the
    day's gains, because nothing throttled re-entry into the exact
    same falling knife right after being stopped out of it - the
    volatility-scalp cohort's re-entry cooldown is deliberately
    zeroed for everything else ("orders can be made as frequently
    as possible"), and this cohort explicitly bypasses quarantine/
    the stop-loss guard/wash-sale blocks by request ("keep trading
    through losses"). This is a narrow, deliberate exception to
    that: it only pauses the ONE symbol that just stopped out, for
    a few minutes, not the strategy - compatible with "keep trading
    through losses" (the other 7 concurrent slots and every other
    symbol are completely unaffected) while closing the specific
    gap DAIC exposed. Fails open (True) for a symbol with no
    recorded stop-loss yet, same convention as every other cooldown
    gate in this file.
    """
    stopped_at = self.last_volatility_stop_loss_at.get(symbol)
    if stopped_at is None:
        return True
    return (
        time.monotonic() - stopped_at
        >= float(self.config.volatility_scalp_post_stop_cooldown_seconds)
    )
