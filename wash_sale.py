import json
from datetime import datetime, timedelta
from pathlib import Path


class WashSaleTracker:
    """Persistent conservative symbol repurchase blocks after loss exits."""

    def __init__(self, state_file: str, block_days: int, timezone, log):
        self.path = Path(state_file)
        self.block_days = block_days
        self.timezone = timezone
        self.log = log
        self.blocks = self._load()

    def _load(self) -> dict[str, str]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as exc:
            self.log.warning("WASH   | state read failed | %s", exc)
            return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.blocks, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def blocked_until(self, symbol: str) -> datetime | None:
        symbol = symbol.upper()
        value = self.blocks.get(symbol)
        if not value:
            return None
        try:
            until = datetime.fromisoformat(value)
        except ValueError:
            self.blocks.pop(symbol, None)
            self._save()
            return None
        if datetime.now(self.timezone) >= until:
            self.blocks.pop(symbol, None)
            self._save()
            return None
        return until

    def block(self, symbol: str, reason: str) -> datetime:
        symbol = symbol.upper()
        current = self.blocked_until(symbol)
        if current:
            return current
        until = datetime.now(self.timezone) + timedelta(days=self.block_days)
        self.blocks[symbol] = until.isoformat()
        self._save()
        self.log.warning(
            "WASH   | %-8s | blocked until %s | %s",
            symbol,
            until.strftime("%Y-%m-%d"),
            reason,
        )
        return until
