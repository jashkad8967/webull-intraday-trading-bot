"""A standing operator veto the bot consults BEFORE it decides.

By explicit request: "I want you to be able to activate in case the bot
is making a wrong decision so you can override it, so it has to be
before the bot makes a decision not after."

The obvious design - the bot proposes a trade and waits for approval -
does not work here, and it is worth writing down why so it is not
re-attempted. Entries are decided inside a loop running at
poll_seconds (0.5s); a human or an assistant answers in tens of seconds
at best. Every entry would stall for the timeout and then proceed
anyway, because the alternative (fail closed) means the account stops
trading the moment nobody is watching. The result is pure latency on
exactly the setups that move fastest, and no veto at all when a session
is not open.

So the veto is a FILE instead of a conversation. It is read on every
entry evaluation, costs nothing, applies whether or not anyone is
present, and can be written in a second:

    {"blocked_symbols": ["ACN", "GOOGL"],
     "halt_entries": false,
     "note": "ACN gapped +18% then faded to 14% of range"}

This is deliberately NOT the wash-sale store (that encodes a tax rule
with its own expiry) nor the command queue (that requests one-off
ACTIONS - sell, buy, cancel). This is standing policy: conditions the
operator has decided against until they say otherwise.

FAILS OPEN on a missing or unparseable file, loudly. A veto store that
halted trading because of a typo would be a worse failure than the
mistakes it exists to prevent - and a corrupt JSON file is exactly what
a hand-edit produces.
"""

import json
import logging
import os
import tempfile
import time
from pathlib import Path

log = logging.getLogger("webull-bot")


class OperatorOverrides:
    """Standing operator policy, re-read from disk when it changes.

    Cached on the file's mtime so the hot entry path does not re-parse
    JSON every cycle, while an edit still takes effect on the very next
    evaluation - no restart, no redeploy.
    """

    def __init__(self, path: str):
        self.path = Path(path)
        # (st_mtime_ns, st_size), not the float st_mtime.
        #
        # Caught by its own test: two edits in quick succession compared
        # EQUAL on the float mtime - it loses precision at current epoch
        # values - so the second veto was silently ignored. A missed
        # override is exactly the failure this class exists to prevent,
        # and it would have been invisible in production: the file says
        # one thing, the bot acts on another. mtime_ns keeps full
        # resolution, and size catches the pathological same-tick case.
        self._stamp: tuple[int, int] | None = None
        self._blocked: frozenset[str] = frozenset()
        self._halt = False
        self._note = ""
        self._warned = False

    def _reload_if_changed(self) -> None:
        try:
            info = self.path.stat()
            stamp = (info.st_mtime_ns, info.st_size)
        except OSError:
            # No file is the normal state - overrides are the exception.
            if self._stamp is not None:
                log.info("OVERRIDE| file removed - no standing vetoes")
            self._stamp = None
            self._blocked = frozenset()
            self._halt = False
            self._note = ""
            return
        if stamp == self._stamp:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("top level is not an object")
            blocked = {
                str(s).upper()
                for s in (data.get("blocked_symbols") or [])
                if str(s).strip()
            }
            halt = bool(data.get("halt_entries", False))
            note = str(data.get("note", ""))[:200]
        except Exception as exc:
            # Loudly, and keep whatever was last valid rather than
            # silently dropping a veto that is still meant to apply.
            if not self._warned:
                log.error(
                    "OVERRIDE| %s is unreadable - KEEPING the last valid "
                    "vetoes (%s blocked, halt=%s). Fix the JSON. | %s",
                    self.path, len(self._blocked), self._halt, exc,
                )
                self._warned = True
            self._stamp = stamp
            return
        self._stamp = stamp
        self._warned = False
        self._blocked = frozenset(blocked)
        self._halt = halt
        self._note = note
        log.warning(
            "OVERRIDE| reloaded | halt_entries=%s | blocked=%s%s",
            halt,
            ",".join(sorted(blocked)) or "none",
            f" | {note}" if note else "",
        )

    def entries_halted(self) -> bool:
        """True while the operator has halted ALL new entries."""
        self._reload_if_changed()
        return self._halt

    def symbol_blocked(self, symbol: str) -> bool:
        """True while this symbol is under a standing operator veto."""
        self._reload_if_changed()
        if not self._blocked:
            return False
        return str(symbol).upper() in self._blocked

    # -- writing, so an override can be set programmatically ----------

    def set(self, blocked_symbols=None, halt_entries=None, note=None) -> dict:
        """Merge changes into the file and return the new contents.

        Written atomically via a temp file in the same directory: a
        half-written veto file read by the trading loop mid-save is
        exactly the corruption this class otherwise has to tolerate.
        """
        self._reload_if_changed()
        payload = {
            "blocked_symbols": sorted(
                {str(s).upper() for s in blocked_symbols}
                if blocked_symbols is not None
                else self._blocked
            ),
            "halt_entries": (
                bool(halt_entries) if halt_entries is not None else self._halt
            ),
            "note": self._note if note is None else str(note)[:200],
            "updated_at": round(time.time(), 3),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(self.path.parent),
            prefix=".overrides-", suffix=".tmp", delete=False,
        )
        try:
            with handle:
                json.dump(payload, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self.path)
        except Exception:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise
        self._stamp = None  # force a reload on the next read
        return payload
