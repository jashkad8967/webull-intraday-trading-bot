import logging
import time

log = logging.getLogger("webull-bot")


def _position_protection_loop(self) -> None:
    """Runs fill/cancel detection, exit repricing, and stop-loss
    escalation on their OWN cadence (poll_seconds, default 0.25s),
    independent of the main loop's much slower full-universe-scan
    cadence (SCAN cycles observed 30-90s+ live). By request:
    "held positions should be checked every 0.25s separately, the
    rest of the scan can take its own time" - live evidence (CHOW)
    showed a stuck PROFIT order sit unrefreshed far longer than
    intended because monitor_working_orders/the repricers/
    escalate_stalled_stop_losses previously ran inline in the same
    single-threaded loop body as trade_stocks' slow, batched
    universe scan, inheriting its cadence instead of the real
    poll_seconds target.

    Runs as a daemon thread (see run(), which starts this once and
    removes these same calls from its own sequential body so they
    never run twice concurrently). self.cached_positions and
    self.cached_core_session_active are read-only snapshots here,
    refreshed by the main thread each cycle - a single attribute
    read is safe under the GIL without its own lock, same
    "atomic reassignment" convention already used for
    stock_symbols/stock_categories in resolve_targets. Everything
    that actually touches self.working_orders (and reads/writes it
    from the main thread's record_trade for fresh entries) goes
    through _working_orders_lock/​_rekey_working_order instead.
    """
    while True:
        started = time.monotonic()
        try:
            self.monitor_working_orders()
            self.evaluate_held_stock_exits()
            self.reprice_resting_exits(
                self.cached_positions, self.cached_core_session_active
            )
            self.reprice_volatility_scalp_exits(
                self.cached_positions, self.cached_core_session_active
            )
            self.reprice_volatility_scalp_entries()
            self.reprice_resting_entries(self.cached_core_session_active)
            self.escalate_stalled_stop_losses()
        except Exception as exc:
            log.error("PROTECT| position-protection cycle failed | %s", exc)
        elapsed = time.monotonic() - started
        time.sleep(max(0.0, float(self.config.poll_seconds) - elapsed))
