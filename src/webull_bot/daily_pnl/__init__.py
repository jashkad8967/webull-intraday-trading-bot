import json
import threading
from datetime import datetime
from decimal import Decimal
from pathlib import Path


class DailyPnlTracker:
    """Persists today's running realized P&L/loss totals across restarts.

    Stores the date the totals belong to alongside the totals themselves,
    so that a load on a genuinely new trading day starts fresh instead of
    rehydrating the previous day's numbers - matching the existing
    in-memory reset that already happens once per day in
    AutoTrader.resolve_targets.
    """

    def __init__(self, state_file: str, timezone, log):
        self.path = Path(state_file)
        self.timezone = timezone
        self.log = log
        self._save_lock = threading.Lock()
        # The date the loaded totals belong to, or None when nothing
        # usable was on disk. Read by the once-daily reset so a
        # RESTART mid-session is not mistaken for a new trading day -
        # see belongs_to_today.
        self.stored_date: str | None = None
        self.realized_pnl, self.realized_loss = self._load()

    def _load(self) -> tuple[Decimal, Decimal]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return Decimal("0"), Decimal("0")
        except Exception as exc:
            self.log.warning("PNL    | state read failed | %s", exc)
            return Decimal("0"), Decimal("0")
        if not isinstance(payload, dict):
            return Decimal("0"), Decimal("0")
        today = datetime.now(self.timezone).date().isoformat()
        if payload.get("date") != today:
            return Decimal("0"), Decimal("0")
        self.stored_date = today
        try:
            realized_pnl = Decimal(str(payload["realized_pnl"]))
            realized_loss = Decimal(str(payload["realized_loss"]))
        except (KeyError, ArithmeticError, ValueError, TypeError):
            return Decimal("0"), Decimal("0")
        return realized_pnl, realized_loss

    def belongs_to_today(self) -> bool:
        """True when the totals now held were loaded from disk for
        TODAY - i.e. this process restarted mid-session rather than
        starting a genuinely new trading day.

        The once-daily reset in universe_resolution_body keys off
        resolved_date, which is in-memory and therefore None after
        every restart. That made each restart look like a new day and
        wipe the running realized totals AND re-arm the daily-loss
        circuit breaker. Live 2026-09-23: a -$52.14 realized loss was
        erased and the breaker re-armed roughly eight times across a
        single session's deploys, so it could never have tripped -
        which defeats the entire point of persisting this file.
        """
        return self.stored_date == datetime.now(self.timezone).date().isoformat()

    def record(self, realized_pnl: Decimal, realized_loss: Decimal) -> None:
        self.realized_pnl = realized_pnl
        self.realized_loss = realized_loss
        self._save()

    def reset(self) -> None:
        self.record(Decimal("0"), Decimal("0"))

    def _save(self) -> None:
        """Live incident: two concurrent record()/reset() calls (an
        exit's realized-pnl recording landing on the same poll cycle
        as another order event's) both wrote the same fixed ".tmp"
        path and both called replace() on it - whichever ran second
        found the first had already consumed (renamed away) the
        file, failing with ENOENT ("No such file or directory:
        daily_pnl.tmp -> daily_pnl.json"). A lock serializes the
        write-then-replace pair so a second concurrent save can't
        observe the first's temp file mid-consumption.
        """
        payload = {
            "date": datetime.now(self.timezone).date().isoformat(),
            "realized_pnl": str(self.realized_pnl),
            "realized_loss": str(self.realized_loss),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with self._save_lock:
            temporary.write_text(
                json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
            )
            temporary.replace(self.path)
