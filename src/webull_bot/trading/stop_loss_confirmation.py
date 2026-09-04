import time


def stop_ready_to_submit(self, key: str, symbol: str) -> bool:
    """An escalated stop must resubmit immediately after its cancel, not
    wait out the normal trade cooldown - that cooldown was timed from
    the original (now-cancelled) submission, so honoring it here would
    leave the position with no working stop order for several more
    seconds while price keeps moving against it.
    """
    if symbol in self.pending_stock_exits:
        return False
    return symbol in self.stop_loss_escalated or self.cooldown_ready(key)


def stop_loss_confirmed(self, symbol: str) -> bool:
    """True once price has sat continuously at/below the stop level for
    STOP_LOSS_CONFIRMATION_SECONDS - see stop_condition_since. An
    escalated stop (already submitted, just resubmitting at a more
    aggressive price after sitting unfilled) skips this: the breach
    was already confirmed once, and escalation is itself a response to
    elapsed time, not a fresh signal that could be a single bad tick.

    Also skips it for the volatility-scalp cohort. Live incident:
    MYND sat well past its stop level (11%+ underwater against a
    ~1.5% max stop) for many minutes without ever stopping out -
    "GATES | stop breach not yet confirmed" kept firing intermittently,
    meaning price ticked back above the stop line often enough that
    the 2s confirmation window never completed. That grace exists to
    filter a single-tick wick; for a symbol whose entire selection
    criterion IS being unusually choppy, the same real, sustained
    loss can cross the stop/un-cross it fast enough to never confirm
    at all, indefinitely deferring real protection on exactly the
    positions most likely to need it fast.
    """
    if (
        not self.config.stop_loss_confirmation_enabled
        or symbol in self.stop_loss_escalated
        or self.strategy.is_volatility_scalp_eligible(symbol)
    ):
        return True
    since = self.stop_condition_since.get(symbol)
    if since is None:
        return False
    return (
        time.monotonic() - since
        >= float(self.config.stop_loss_confirmation_seconds)
    )
