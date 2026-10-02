import logging
import os
import threading
import time

log = logging.getLogger("webull-bot")

# Generous on purpose. A false restart is not free - it drops the
# in-memory peak prices the profit-lock trail rides and the working-order
# bookkeeping - so these are set well beyond the worst legitimate cycle
# ever measured, not near it.
#
# The slow scan has been observed at 5-7 MINUTES per cycle under host
# load (2026-09-22, during an on-host image build), so 15 minutes was
# roughly double the worst real case AT THE TIME.
#
# It stopped being double anything. On 2026-10-02 real cycles ran 13-20
# minutes - 165 option contracts evaluated per pass once the liquid-ETF
# chains were discovered - and this bound killed a bot that was plainly
# still working: it had logged option refusals four minutes before being
# shot. Every restart dropped the daily batch, the cohort and the
# direction-signal EMA history, and the signals never warmed up enough to
# emit anything but HOLD. Three cycles in 35 minutes, zero trades. The
# monitor was not detecting a livelock, it was causing one.
#
# The fix is NOT a bigger number - a bound tuned to today's cycle time
# would rot again the next time the option universe grows. It is measuring
# the right thing: main_loop_progress_at is updated at intermediate points
# INSIDE a cycle, so this now means "no progress at all for 15 minutes",
# which is a genuine hang rather than a slow pass.
MAIN_LOOP_STALL_SECONDS = 900
# The protection loop ticks every poll_seconds (sub-second), and it is
# the one that actually submits stops and profit exits, so a stall here
# is the more dangerous of the two and gets a tighter bound.
PROTECTION_LOOP_STALL_SECONDS = 300


def _watchdog_body(self) -> None:
    while True:
        time.sleep(30)
        try:
            now = time.monotonic()
            for name, last, limit in (
                (
                    "main scan",
                    # Progress, not cycle completion - see above.
                    # Falls back to the cycle tick if progress was
                    # never recorded, so an old caller still gets
                    # the original behaviour rather than none.
                    getattr(self, "main_loop_progress_at", None)
                    or self.main_loop_ticked_at,
                    MAIN_LOOP_STALL_SECONDS,
                ),
                (
                    "position protection",
                    self.protection_loop_ticked_at,
                    PROTECTION_LOOP_STALL_SECONDS,
                ),
            ):
                if last is None:
                    # Not started yet - startup can legitimately take a
                    # while (auth, universe resolution) and there is
                    # nothing to compare against.
                    continue
                stalled_for = now - last
                if stalled_for < limit:
                    continue
                # Deliberately os._exit, not sys.exit or an exception:
                # the point of this is that a thread is WEDGED, so
                # anything that relies on normal interpreter unwinding
                # (or on acquiring a lock the stuck thread may hold)
                # could hang too. Docker's restart:unless-stopped then
                # brings the container straight back.
                log.error(
                    "WATCHDOG| %s loop has not ticked for %.0fs "
                    "(limit %ss) - exiting so the container restarts",
                    name,
                    stalled_for,
                    limit,
                )
                # Give the log a moment to flush to stdout and the
                # daily file before the process dies.
                time.sleep(1)
                os._exit(75)
        except Exception as exc:  # never let the watchdog itself die
            log.warning("WATCHDOG| check failed | %s", exc)


def start_loop_watchdog(self) -> None:
    """Restart the container when a loop wedges rather than exits.

    restart:unless-stopped only covers a process that EXITS. A bot
    whose scan loop or protection loop is stuck - blocked on a socket
    with no timeout, deadlocked on a lock - keeps the process alive and
    the container "Up", while stops and profit exits silently stop
    being submitted on positions that are still real money. Docker's
    own HEALTHCHECK does not help either: it marks a container
    unhealthy and takes no action on it.

    So the bot watches itself and exits, which is the one signal the
    existing restart policy already acts on, and needs no second
    container to do it.
    """
    threading.Thread(target=_watchdog_body, args=(self,), daemon=True).start()
