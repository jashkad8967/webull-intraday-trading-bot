"""Atomic file replace that survives a concurrent reader on Windows.

os.replace is atomic on both platforms, but they differ in a way that
matters here: on POSIX it succeeds even while another process holds the
target open, and on Windows it raises PermissionError (WinError 5) until
that handle is released.

Live 2026-10-01, within minutes of the dashboard starting on the same
Windows machine as the trader:

    PROTECT| position-protection cycle failed | [WinError 5]
    Access is denied: 'status.tmp' -> 'status.json'

The bot rewrites status.json roughly every poll interval; the dashboard
reads it on every HTTP request. Overlap one with the other and the write
fails. On the Linux host this could not happen, which is why the writers
were built without a retry.

A brief retry is the right remedy rather than locking: the reader holds
the file for microseconds, so a handful of short sleeps clears essentially
every collision, while a lock would put the trading loop behind a
dashboard request. Still raises if it genuinely cannot write - a caller
that needs to know must still be told.
"""

import logging
import os
import time
from pathlib import Path

log = logging.getLogger("webull-bot")

ATTEMPTS = 5
BACKOFF_SECONDS = 0.02


def atomic_replace(temporary: Path, target: Path) -> None:
    """Replace `target` with `temporary`, retrying a Windows collision.

    Raises the last error if every attempt fails, so a caller that treats
    a failed write as significant still sees it.
    """
    last: Exception | None = None
    for attempt in range(ATTEMPTS):
        try:
            os.replace(temporary, target)
            if attempt:
                log.debug(
                    "atomic_replace| %s succeeded on attempt %s",
                    target.name, attempt + 1,
                )
            return
        except PermissionError as exc:
            # Windows only: another process holds the target open. The
            # holder releases within microseconds, so sleeping briefly is
            # enough. Linear rather than exponential - this must not add
            # meaningful latency to a 0.5s trading loop.
            last = exc
            if attempt < ATTEMPTS - 1:
                time.sleep(BACKOFF_SECONDS * (attempt + 1))
        except OSError as exc:
            # Anything else (missing temp file, cross-device, bad path) is
            # not a contention problem and will not be fixed by waiting.
            raise exc
    assert last is not None
    raise last
