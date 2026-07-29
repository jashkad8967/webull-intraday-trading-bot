import json
from datetime import datetime, timedelta
from pathlib import Path

# The day count baked into every block written before this tracker switched
# to storing the loss date instead of a precomputed block-until date. Used
# once, at load time, to convert legacy entries - never touched afterward.
_LEGACY_BLOCK_DAYS = 60


class WashSaleTracker:
    """Persistent conservative symbol repurchase blocks after loss exits.

    Stores the date each block was triggered, not a precomputed
    block-until date, so that changing WASH_SALE_BLOCK_DAYS (including
    this one, 60 -> 31) retroactively re-shortens or re-lengthens every
    existing block instead of freezing in whatever day count was
    configured when the block was originally written.
    """

    def __init__(self, state_file: str, block_days: int, timezone, log):
        self.path = Path(state_file)
        self.block_days = block_days
        self.timezone = timezone
        self.log = log
        self.blocks = self._load()

    def _load(self) -> dict[str, dict]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception as exc:
            self.log.warning("WASH   | state read failed | %s", exc)
            return {}
        if not isinstance(payload, dict):
            return {}
        blocks: dict[str, dict] = {}
        migrated = 0
        for symbol, value in payload.items():
            if isinstance(value, str):
                # Legacy format: a precomputed "until" date written under
                # the old fixed 60-day rule. Recover the original loss
                # date so it can be evaluated against the current
                # block_days instead.
                try:
                    until = datetime.fromisoformat(value)
                except ValueError:
                    continue
                blocked_at = until - timedelta(days=_LEGACY_BLOCK_DAYS)
                blocks[symbol] = {"blocked_at": blocked_at.isoformat()}
                migrated += 1
            elif isinstance(value, dict) and "blocked_at" in value:
                blocks[symbol] = value
        if migrated:
            self.log.warning(
                "WASH   | migrated %s legacy block(s) to the current "
                "WASH_SALE_BLOCK_DAYS setting",
                migrated,
            )
            self.blocks = blocks
            self._save()
        return blocks

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
        entry = self.blocks.get(symbol)
        if not entry:
            return None
        try:
            blocked_at = datetime.fromisoformat(entry["blocked_at"])
        except (KeyError, ValueError):
            self.blocks.pop(symbol, None)
            self._save()
            return None
        until = blocked_at + timedelta(days=self.block_days)
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
        now = datetime.now(self.timezone)
        self.blocks[symbol] = {"blocked_at": now.isoformat()}
        self._save()
        until = now + timedelta(days=self.block_days)
        self.log.warning(
            "WASH   | %-8s | blocked until %s | %s",
            symbol,
            until.strftime("%Y-%m-%d"),
            reason,
        )
        return until
