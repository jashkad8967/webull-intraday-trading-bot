import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal

# Hardcoded, not config: this is a fixed second strategy the user asked to
# graft on, not a per-account tuning surface. Liquid, historically
# correlated cross-sector pairs - correlation isn't static, so this list
# is a starting point to revisit, not a guarantee.
PAIRS: list[tuple[str, str]] = [
    ("KO", "PEP"),
    ("XOM", "CVX"),
    ("V", "MA"),
    ("GOOGL", "META"),
    ("AMD", "NVDA"),
]

# Spread sampled at most this often - this strategy trades on a minutes
# scale (per the user's own framing), not tick noise, so it deliberately
# doesn't resample on every 0.25s poll like the single-symbol strategy.
PAIRS_SAMPLE_SECONDS = 30
PAIRS_LOOKBACK = 120  # samples (~1 hour at the default sample interval)
PAIRS_MIN_SAMPLES = 20  # don't trade a z-score computed from a handful of points

PAIRS_ENTRY_Z = Decimal("2.0")
PAIRS_EXIT_Z = Decimal("0.5")
PAIRS_STOP_Z = Decimal("3.5")
PAIRS_MAX_HOLD_MINUTES = 240

PAIRS_CAPITAL_FRACTION = Decimal("0.10")  # of buying power, split across all legs
PAIRS_MAX_CONCURRENT = 2


@dataclass(frozen=True)
class PairsDecision:
    action: str  # NO_DATA | HOLD | ENTER_LONG_A_SHORT_B | ENTER_LONG_B_SHORT_A | UNWIND | STOP
    reason: str
    z_score: Decimal | None = None


class PairsStrategy:
    """Correlated-pair mean reversion: track the spread between two
    historically-correlated liquid stocks, and when it drifts far enough
    from its own recent average (a z-score extreme), bet on it reverting -
    long the relatively cheap leg, short the relatively expensive one.
    """

    def __init__(self):
        self._spread_history: dict[tuple[str, str], deque] = defaultdict(
            lambda: deque(maxlen=PAIRS_LOOKBACK)
        )
        self._last_sample_at: dict[tuple[str, str], float] = {}
        self._entered_at: dict[tuple[str, str], float] = {}

    def update(
        self,
        pair: tuple[str, str],
        price_a: Decimal,
        price_b: Decimal,
    ) -> None:
        if price_a <= 0 or price_b <= 0:
            return
        now = time.monotonic()
        last = self._last_sample_at.get(pair, 0.0)
        if now - last < PAIRS_SAMPLE_SECONDS:
            return
        self._last_sample_at[pair] = now
        spread = math.log(float(price_a)) - math.log(float(price_b))
        self._spread_history[pair].append(spread)

    def _z_score(self, pair: tuple[str, str]) -> Decimal | None:
        history = self._spread_history.get(pair)
        if not history or len(history) < PAIRS_MIN_SAMPLES:
            return None
        mean = sum(history) / len(history)
        variance = sum((value - mean) ** 2 for value in history) / len(history)
        stdev = math.sqrt(variance)
        if stdev == 0:
            return None
        current = history[-1]
        return Decimal(str((current - mean) / stdev))

    def mark_entered(self, pair: tuple[str, str]) -> None:
        self._entered_at[pair] = time.monotonic()

    def mark_exited(self, pair: tuple[str, str]) -> None:
        self._entered_at.pop(pair, None)

    def decision(self, pair: tuple[str, str], is_open: bool) -> PairsDecision:
        z = self._z_score(pair)
        if z is None:
            return PairsDecision("NO_DATA", "insufficient spread history")
        if is_open:
            held_minutes = (
                (time.monotonic() - self._entered_at.get(pair, time.monotonic())) / 60
            )
            if abs(z) >= PAIRS_STOP_Z:
                return PairsDecision("STOP", "spread kept diverging past the stop z-score", z)
            if held_minutes >= PAIRS_MAX_HOLD_MINUTES:
                return PairsDecision("UNWIND", "max hold time reached", z)
            if abs(z) <= PAIRS_EXIT_Z:
                return PairsDecision("UNWIND", "spread reverted toward the mean", z)
            return PairsDecision("HOLD", "waiting for reversion", z)
        if z >= PAIRS_ENTRY_Z:
            # A is rich relative to B: short A, long B.
            return PairsDecision("ENTER_LONG_B_SHORT_A", "spread stretched wide (A rich)", z)
        if z <= -PAIRS_ENTRY_Z:
            # A is cheap relative to B: long A, short B.
            return PairsDecision("ENTER_LONG_A_SHORT_B", "spread stretched wide (B rich)", z)
        return PairsDecision("NO_DATA", "spread within normal range", z)
