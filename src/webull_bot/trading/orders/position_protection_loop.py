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
            # By request: "the manual sell button or cancel button or
            # buy buttons are very slow and not working properly, they
            # should be put on a separate thread".
            #
            # process_ui_commands used to run inline in the main loop,
            # so a dashboard click was only acted on once per full scan
            # cycle. Measured live 2026-09-22 with the cohort at 3,408
            # discovered contracts and the host under load, consecutive
            # SCAN lines were 13:13:02, 13:17:50, 13:24:33 - a Sell
            # could sit queued for up to SEVEN MINUTES. For a button
            # whose entire purpose is "get me out of this now", that is
            # indistinguishable from broken.
            #
            # Run first in this tick, ahead of the repricers, so a
            # manual action takes effect before automated order
            # management reasons about the same position. Reads the
            # same cached snapshots every other call here uses, and
            # writes the post-command balance back so the next tick
            # sizes against what is actually left.
            self.cached_buying_power = self.process_ui_commands(
                self.cached_positions,
                self.cached_buying_power,
                self.cached_core_session_active,
            )
            self.monitor_working_orders()
            self.evaluate_held_stock_exits()
            self.reprice_resting_exits(
                self.cached_positions, self.cached_core_session_active
            )
            self.reprice_resting_option_exits(
                self.cached_positions, self.cached_core_session_active
            )
            self.reprice_volatility_scalp_exits(
                self.cached_positions, self.cached_core_session_active
            )
            self.reprice_volatility_scalp_entries()
            self.reprice_resting_entries(self.cached_core_session_active)
            self.reprice_resting_option_entries()
            self.escalate_stalled_stop_losses()
            # By request: "ui is not always showing all buy sell
            # profit accurately, and is not updating as quick as
            # needed since scans are slower, put that on a parallel
            # thread" - write_status_snapshot used to only run once
            # per full slow scan cycle (30-90s+ live), so a fill/exit
            # this fast loop had already acted on above could sit
            # invisible on the dashboard until the next slow cycle
            # finished. Now written every fast-loop tick instead -
            # write_status_snapshot has its own internal poll_seconds
            # throttle already, so this doesn't over-write.
            self.write_status_snapshot(
                self.cached_positions,
                self.cached_raw_buying_power,
                self.cached_circuit_active,
            )
        except Exception as exc:
            log.error("PROTECT| position-protection cycle failed | %s", exc)
        elapsed = time.monotonic() - started
        time.sleep(max(0.0, float(self.config.poll_seconds) - elapsed))
