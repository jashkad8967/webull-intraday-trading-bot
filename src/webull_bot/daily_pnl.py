import json
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
        try:
            realized_pnl = Decimal(str(payload["realized_pnl"]))
            realized_loss = Decimal(str(payload["realized_loss"]))
        except (KeyError, ArithmeticError, ValueError, TypeError):
            return Decimal("0"), Decimal("0")
        return realized_pnl, realized_loss

    def record(self, realized_pnl: Decimal, realized_loss: Decimal) -> None:
        self.realized_pnl = realized_pnl
        self.realized_loss = realized_loss
        self._save()

    def reset(self) -> None:
        self.record(Decimal("0"), Decimal("0"))

    def _save(self) -> None:
        payload = {
            "date": datetime.now(self.timezone).date().isoformat(),
            "realized_pnl": str(self.realized_pnl),
            "realized_loss": str(self.realized_loss),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)
