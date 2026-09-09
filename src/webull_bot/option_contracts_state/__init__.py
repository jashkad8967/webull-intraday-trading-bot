import json
from datetime import date
from decimal import Decimal
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

    Also persists each held position's averaging-down ladder state
    (count + last buy price) - by request ("do a full on options
    sanity check"), a review found that this used to live ONLY in
    plain in-memory dicts (option_average_down_count/option_last_buy_
    price in AutoTrader), which reset to empty on every restart while
    the broker's real position/cost basis did not. A position that
    had already used both its allowed averaging-down buys before a
    restart would get a FRESH allotment of option_max_averaging_buys
    more after one, silently exceeding the configured per-position
    risk cap - the same "state that should track a held position's
    real risk exposure but doesn't survive a restart" bug family as
    the held-contract-backfill fix above. The reentry-cooldown
    timestamp is deliberately NOT persisted here - it's a
    time.monotonic() value, meaningless across a process restart (the
    monotonic clock itself resets), and losing it only costs a few
    seconds of extra caution after a restart, not a risk-budget
    violation the way losing the count/last-price does.
    """

    def __init__(self, state_file: str, log):
        self.path = Path(state_file)
        self.log = log

    def load(self) -> tuple[list[dict], set[str], dict[str, dict]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return [], set(), {}
        except Exception as exc:
            self.log.warning("OPTIONS| contract state read failed | %s", exc)
            return [], set(), {}
        if not isinstance(payload, dict):
            return [], set(), {}
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
        raw_averaging = payload.get("averaging")
        averaging_state: dict[str, dict] = {}
        if isinstance(raw_averaging, dict):
            for symbol, entry in raw_averaging.items():
                if not isinstance(entry, dict):
                    continue
                try:
                    count = int(entry.get("count", 0))
                    last_buy_price = Decimal(str(entry.get("last_buy_price")))
                except Exception:
                    continue
                if count <= 0:
                    continue
                averaging_state[str(symbol)] = {
                    "count": count,
                    "last_buy_price": last_buy_price,
                }
        if contracts:
            self.log.info(
                "OPTIONS| restored %s contract(s) from prior session",
                len(contracts),
            )
        if averaging_state:
            self.log.info(
                "OPTIONS| restored averaging-down state for %s position(s) "
                "from prior session",
                len(averaging_state),
            )
        return contracts, attempted_symbols, averaging_state

    def save(
        self,
        contracts: list[dict],
        attempted: set[str],
        averaging: dict[str, dict] | None = None,
    ) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            payload = {"contracts": contracts, "attempted": sorted(attempted)}
            if averaging is not None:
                payload["averaging"] = {
                    symbol: {
                        "count": entry["count"],
                        "last_buy_price": str(entry["last_buy_price"]),
                    }
                    for symbol, entry in averaging.items()
                    if entry.get("count", 0) > 0
                }
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            temporary.replace(self.path)
        except Exception as exc:
            self.log.warning("OPTIONS| contract state save failed | %s", exc)
