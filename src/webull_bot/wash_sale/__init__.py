import json
import threading
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
        # RLock (not Lock): block() calls blocked_until() while
        # already holding this - a plain Lock would deadlock on that
        # reentrant acquisition from the same thread.
        self._lock = threading.RLock()
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
        # Drop entries whose block has already expired.
        #
        # blocked_until() prunes lazily, but only for a symbol someone
        # asks about - so a name the scanner never surfaces again keeps
        # its entry forever. Live 2026-09-23 this file held 336 blocks,
        # most of them long-dead penny stocks from weeks earlier, and
        # every one was re-read, re-serialised and re-written on every
        # save. By explicit request ("there is no need to save so much
        # data"), expired blocks are dropped once at load instead of
        # accumulating for the life of the deployment.
        expired = []
        now = datetime.now(self.timezone)
        for symbol, value in blocks.items():
            try:
                blocked_at = datetime.fromisoformat(value["blocked_at"])
            except (KeyError, ValueError, TypeError):
                expired.append(symbol)
                continue
            if now >= blocked_at + timedelta(days=self.block_days):
                expired.append(symbol)
        for symbol in expired:
            blocks.pop(symbol, None)
        if expired:
            self.log.info(
                "WASH   | dropped %s expired block(s) at load | %s remain",
                len(expired),
                len(blocks),
            )
        if migrated:
            self.log.warning(
                "WASH   | migrated %s legacy block(s) to the current "
                "WASH_SALE_BLOCK_DAYS setting",
                migrated,
            )
        if migrated or expired:
            # Persist immediately so the shrunken set is what the next
            # start reads, rather than re-deriving it every boot.
            self.blocks = blocks
            self._save()
        return blocks

    def _save(self) -> None:
        """Live incident: concurrent block() calls for different
        symbols (each reading/writing the same shared self.blocks
        dict and the same fixed ".tmp" path) raced the write-then-
        replace pair, the same class of bug fixed for DailyPnlTracker
        - a second concurrent save could observe the first save's
        temp file mid-consumption, or json.dumps could observe
        self.blocks mid-mutation from another thread ("dictionary
        changed size during iteration"). Callers must hold self._lock
        for the whole read-modify-write, not just this file I/O -
        see block()/blocked_until()'s self-heal paths.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.blocks, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def blocked_until(self, symbol: str) -> datetime | None:
        symbol = symbol.upper()
        with self._lock:
            entry = self.blocks.get(symbol)
            if not entry:
                return None
            try:
                blocked_at = datetime.fromisoformat(entry["blocked_at"])
            except (KeyError, ValueError, TypeError):
                # Live incident: a live block entry was found with a
                # non-string "blocked_at" (raises TypeError, not the
                # ValueError a malformed-but-string value would raise) -
                # every scan of an already-blocked symbol crashed with
                # "fromisoformat: argument must be str" instead of
                # self-healing like the other corrupt-entry case below.
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
        with self._lock:
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
