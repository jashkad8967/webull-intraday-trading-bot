"""Advisory file locking that works on POSIX and on Windows.

The command queue is read-modify-written by two separate processes - the
trader and the dashboard - so an unlocked write can lose a command, and a
lost command is a lost order.

That used to be a Linux-only concern, and CommandQueue said so:

    "fcntl is POSIX-only; production always runs in the Linux Docker
     image, so this only matters for collecting/running tests on a native
     Windows dev machine. No real advisory locking there, but nothing on
     Windows shares this file across processes either."

The second half of that stopped being true on 2026-10-01, when the bot
moved onto a Windows workstation and the dashboard moved with it. Both now
share commands.json on the same machine, with no lock between them. A
dropped order is not hypothetical here: one was already lost that day to a
container restart clearing the queue mid-cycle, and it was only noticed by
checking positions afterwards.

msvcrt.locking is the Windows equivalent of flock. It differs in ways that
matter:
  * it locks a BYTE RANGE, not the whole file, so every participant must
    agree on the same range - byte 0 here
  * LK_LOCK retries for about ten seconds and then raises, where flock
    simply waits
  * the unlock must cover the same range as the lock

Every failure is swallowed deliberately. Falling back to an unlocked write
is a small risk; refusing to read the queue at all, or crashing the
dashboard because a lock could not be taken, is a larger one.
"""

import logging

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX only
    fcntl = None
try:
    import msvcrt
except ImportError:  # pragma: no cover - Windows only
    msvcrt = None

log = logging.getLogger("webull-bot")

_LOCK_BYTES = 1


def lock_exclusive(handle) -> bool:
    """Take an exclusive advisory lock. True if it was actually taken.

    Returns False rather than raising when locking is unavailable or
    contended past its timeout, so callers proceed unlocked instead of
    failing - see the module docstring for why that is the right default
    here.
    """
    if fcntl is not None:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            return True
        except OSError as exc:
            log.debug("file_lock| flock failed, proceeding unlocked | %s", exc)
            return False
    if msvcrt is not None:
        try:
            position = handle.tell()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, _LOCK_BYTES)
                return True
            finally:
                handle.seek(position)
        except OSError as exc:
            # LK_LOCK gives up after ~10s of contention. The other writer
            # holds it only for a tiny read-modify-write, so this means
            # something is wrong rather than merely busy.
            log.warning(
                "file_lock| could not lock after retries, proceeding "
                "unlocked | %s", exc,
            )
            return False
    return False


def unlock(handle, locked: bool = True) -> None:
    """Release a lock taken by lock_exclusive.

    `locked` is the value lock_exclusive returned: unlocking a range that
    was never locked raises on Windows, so this must not be called
    unconditionally.
    """
    if not locked:
        return
    if fcntl is not None:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError as exc:
            log.debug("file_lock| unlock failed | %s", exc)
        return
    if msvcrt is not None:
        try:
            position = handle.tell()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, _LOCK_BYTES)
            finally:
                handle.seek(position)
        except OSError as exc:
            log.debug("file_lock| unlock failed | %s", exc)
