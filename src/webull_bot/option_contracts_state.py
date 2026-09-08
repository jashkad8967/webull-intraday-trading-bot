import json
from datetime import date
from pathlib import Path


class OptionContractsStateStore:
    """Persists discovered option contracts (and which underlyings have
    already been attempted) across restarts.

    By request: "is there a way to save these option contracts" -
    discover_option_contracts previously rebuilt self.option_contracts
    from scratch every process start, purely in memory. Live evidence
    (see the earlier "are you sure there is no way to load options
    more quickly" investigation) showed real discovery ramp-up taking
    many minutes on a cold start - every restart threw that progress
    away and paid the ramp-up cost again. Same load/save-on-write
    pattern as WashSaleTracker/InvalidSymbolTracker.
    """

    def __init__(self, state_file: str, log):
        self.path = Path(state_file)
        self.log = log

    def load(self) -> tuple[list[dict], set[str]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return [], set()
        except Exception as exc:
            self.log.warning("OPTIONS| contract state read failed | %s", exc)
            return [], set()
        if not isinstance(payload, dict):
            return [], set()
        raw_contracts = payload.get("contracts")
        if not isinstance(raw_contracts, list):
            raw_contracts = []
        today = date.today()
        contracts: list[dict] = []
        for item in raw_contracts:
            if not isinstance(item, dict):
                continue
            try:
                expiration = date.fromisoformat(item.get("expiration_date", ""))
            except ValueError:
                continue
            # Drop anything that's already expired - no point carrying
            # a dead contract forward, and option_min_hold_dte/max_dte
            # will re-filter everything else on the next real discovery
            # pass anyway.
            if expiration < today:
                continue
            contracts.append(item)
        attempted = payload.get("attempted")
        attempted_symbols = (
            {str(symbol) for symbol in attempted} if isinstance(attempted, list) else set()
        )
        if contracts:
            self.log.info(
                "OPTIONS| restored %s contract(s) from prior session",
                len(contracts),
            )
        return contracts, attempted_symbols

    def save(self, contracts: list[dict], attempted: set[str]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {"contracts": contracts, "attempted": sorted(attempted)},
                ),
                encoding="utf-8",
            )
            temporary.replace(self.path)
        except Exception as exc:
            self.log.warning("OPTIONS| contract state save failed | %s", exc)
