import logging
import threading
from datetime import datetime

log = logging.getLogger("webull-bot")


def resolve_targets(self, moment: datetime) -> None:
    """Kicks off the once-daily universe/VOLFILT/SMA refresh on a
    background thread and returns immediately - never blocks the
    caller. Live incident: this used to run synchronously inline in
    the main loop, meaning EVERY protective mechanism (stop-loss
    checks, order-fill monitoring, repricing) was unavailable for
    however long the whole sequence took - under a minute at the
    old 500-symbol universe, but 15-20 minutes at today's 5000-
    symbol universe. Confirmed live: real open positions (down as
    much as -7.6%) sat completely unmonitored through that entire
    window on every restart. See _resolve_targets_work for the
    actual (unchanged) slow work; this wrapper only adds the
    non-blocking dispatch.
    """
    if self.resolved_date == moment.date():
        return
    if self._resolve_targets_in_progress_for == moment.date():
        return
    self._resolve_targets_in_progress_for = moment.date()
    threading.Thread(
        target=self._resolve_targets_work,
        args=(moment,),
        daemon=True,
    ).start()


def _resolve_targets_work(self, moment: datetime) -> None:
    try:
        self._resolve_targets_work_body(moment)
    except Exception as exc:
        log.error("LOAD   | resolve_targets failed | %s", exc)
    finally:
        # Cleared on both success and failure - a failure retries
        # on the very next cycle instead of being permanently
        # stuck for the rest of the day (resolved_date is only
        # ever set on success, at the end of the body below).
        self._resolve_targets_in_progress_for = None
