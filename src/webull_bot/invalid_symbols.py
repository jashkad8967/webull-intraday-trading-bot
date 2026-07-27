import json
from pathlib import Path


class InvalidSymbolTracker:
    """Persistent set of symbols that failed broker quote validation."""

    def __init__(self, state_file: str, log):
        self.path = Path(state_file)
        self.log = log
        self.symbols = self._load()

    def _load(self) -> set[str]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return set()
        except Exception as exc:
            self.log.warning("INVALID | state read failed | %s", exc)
            return set()
        if isinstance(payload, list):
            return {str(symbol).upper() for symbol in payload if str(symbol).strip()}
        return set()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(sorted(self.symbols), indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def add(self, symbols) -> set[str]:
        candidates = {str(symbol).upper() for symbol in symbols if str(symbol).strip()}
        new = candidates - self.symbols
        if new:
            self.symbols |= new
            self._save()
        return new

    def __contains__(self, symbol: str) -> bool:
        return str(symbol).upper() in self.symbols
