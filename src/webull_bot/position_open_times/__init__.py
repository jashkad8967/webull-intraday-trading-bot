import json
import threading
from datetime import datetime
from pathlib import Path


class PositionOpenTimeStore:
    """Persists when each open position was entered, across restarts.

    Two live failures made this necessary.

    1. Wrong ages after a restart. position_opened_at was in-memory
       only, and option_entry_exit reseeded a missing entry from
       "first sight" - so every restart reset a held position's age
       clock to zero. That delays the stale exit by a full
       option_stale_exit_minutes each time and mis-drives the
       time-aware stop. Live 2026-09-22: three positions opened at
       10:46 were still open when a 13:05 deploy restarted the
       container, and their age silently went back to zero.

    2. Positions carried overnight went undetected. This bot is
       intraday-flat by design - option_eod_close_time exists to
       guarantee it. On 2026-09-23 the host wedged and the bot was
       DOWN through its own 14:50 close window; it came back at 15:54,
       after options had stopped trading, holding three positions it
       had no record of being overdue. Without a real entry DATE
       there is no way to tell a position carried from yesterday from
       one opened this morning, and treating the second like the
       first would dump legitimate intraday positions on every
       mid-session restart.

    Deliberately tiny: one ISO timestamp per OPEN position, pruned to
    whatever is still held on every save. Bounded by position count
    (single digits here), not by time - it cannot grow the way the
    unbounded logs and images that filled this host's disk did.
    """

    def __init__(self, state_file: str, timezone, log):
        self.path = Path(state_file)
        self.timezone = timezone
        self.log = log
        self._lock = threading.Lock()
        self.opened_at: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception as exc:
            self.log.warning("POSAGE | state read failed | %s", exc)
            return {}
        if not isinstance(payload, dict):
            return {}
        return {
            str(key): str(value)
            for key, value in payload.items()
            if isinstance(value, str)
        }

    def _save(self) -> None:
        """Caller must hold self._lock - same convention as the other
        stores here, so a read-modify-write is atomic end to end.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".tmp{threading.get_ident()}")
        try:
            temporary.write_text(
                json.dumps(self.opened_at, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            temporary.replace(self.path)
        except Exception as exc:
            self.log.warning("POSAGE | state write failed | %s", exc)
            try:
                temporary.unlink()
            except OSError:
                pass

    def note_open(self, key: str) -> datetime:
        """Record an entry, or return the one already stored.

        Idempotent so a position seen again after a restart keeps its
        ORIGINAL timestamp rather than being re-stamped to now - that
        re-stamping is the age-reset bug this exists to fix.
        """
        with self._lock:
            existing = self.opened_at.get(key)
            if existing:
                parsed = self._parse(existing)
                if parsed is not None:
                    return parsed
            now = datetime.now(self.timezone)
            self.opened_at[key] = now.isoformat()
            self._save()
            return now

    def opened(self, key: str) -> datetime | None:
        with self._lock:
            return self._parse(self.opened_at.get(key))

    def _parse(self, value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            moment = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=self.timezone)
        return moment

    def forget(self, key: str) -> None:
        with self._lock:
            if self.opened_at.pop(key, None) is not None:
                self._save()

    def drop_stale_from_earlier_days(self, held_keys) -> int:
        """Remove records from a PREVIOUS day for positions no longer
        held. Returns how many were dropped.

        Two reasons this is needed, both found live 2026-09-25 with 13
        stale entries sitting in the file:

        1. note_open is idempotent - it preserves the ORIGINAL
           timestamp - so re-entering a contract that was traded
           yesterday keeps yesterday's date. opened_before_today then
           reports True and close_carried_over_options would flatten a
           legitimately fresh position the moment the session opened.
           That is a live-money bug, not just untidiness.

        2. forget() runs on exit, but the fast exit-evaluation loop
           re-stamps via note_open for a few seconds afterwards while
           cached_positions still shows the closed position - so
           records come back after being removed and the file grows.

        Deliberately scoped to EARLIER DAYS only, and never touches a
        record stamped today. An empty or momentarily stale position
        snapshot (every restart has one) must not be able to delete the
        age of something actually open - losing that would reset a
        held position's stale-exit clock, which is the very bug the
        store was written to fix.
        """
        keep = set(held_keys)
        today = datetime.now(self.timezone).date()
        with self._lock:
            stale = []
            for key, value in self.opened_at.items():
                if key in keep:
                    continue
                parsed = self._parse(value)
                if parsed is None or parsed.date() < today:
                    stale.append(key)
            for key in stale:
                self.opened_at.pop(key, None)
            if stale:
                self._save()
        return len(stale)

    def opened_before_today(self, key: str) -> bool:
        """True when this position was entered on an earlier DATE.

        Date rather than elapsed hours on purpose: "carried over from
        a previous session" is a calendar question, and an elapsed-time
        rule would either catch a legitimate position opened late
        yesterday afternoon or miss one opened early.
        """
        opened = self.opened(key)
        if opened is None:
            return False
        return opened.date() < datetime.now(self.timezone).date()
