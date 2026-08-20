import json
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class LeverSpec:
    field: str
    minimum: Decimal
    maximum: Decimal
    # True: "increase" raises the field's value. False: "increase" LOWERS
    # it (e.g. "tighter" stops means a SMALLER range multiplier) - the
    # lever's own English meaning of "increase" doesn't always point the
    # same direction as the underlying number.
    increase_raises: bool
    enabled_field: str | None = None


# Every field named here must be a real Settings field in config.py.
# Deliberately excludes every safety/circuit-breaker field
# (daily_loss_circuit_breaker_enabled, daily_max_loss_dollars,
# min_cash_reserve_dollars, stop_loss_guard_*, wash_sale_*,
# live_trading_enabled) - MarketResearchAgent._VALID_LEVERS never
# offered the model those as an option, and this table enforces that
# boundary a second time at apply time (see SAFETY_DENYLIST below).
#
# max_order_notional and symbol_quarantine_loss_dollars have no upper
# Pydantic bound (gt=0 only) - their maximum here is a self-imposed
# ceiling (HARD_ORDER_NOTIONAL_CEILING for the former; a level past
# which quarantine would essentially never trip for the latter), not
# read from the Field itself, precisely because the Field doesn't
# provide one.
LEVER_SPECS: dict[str, LeverSpec] = {
    "stop-loss tightness": LeverSpec(
        field="stock_stop_loss_range_multiplier",
        minimum=Decimal("0"),
        maximum=Decimal("5"),
        increase_raises=False,
    ),
    "profit-target distance": LeverSpec(
        field="stock_target_stop_multiple",
        minimum=Decimal("0.5"),
        maximum=Decimal("5"),
        increase_raises=True,
    ),
    "position size": LeverSpec(
        field="max_order_notional",
        minimum=Decimal("50"),
        maximum=Decimal("2000"),
        increase_raises=True,
    ),
    "entry selectivity": LeverSpec(
        field="reenter_confirmation_polls",
        minimum=Decimal("1"),
        maximum=Decimal("20"),
        increase_raises=True,
    ),
    "symbol-quarantine aggressiveness": LeverSpec(
        field="symbol_quarantine_loss_dollars",
        minimum=Decimal("0.05"),
        maximum=Decimal("10"),
        increase_raises=False,
        enabled_field="symbol_quarantine_enabled",
    ),
    "time-aware-stop widen window": LeverSpec(
        field="time_aware_stop_widen_seconds",
        minimum=Decimal("1"),
        maximum=Decimal("3600"),
        increase_raises=True,
        enabled_field="time_aware_stop_enabled",
    ),
    # "fractional-vs-whole-share balance" is deliberately NOT here - see
    # apply_lever_adjustment's special case (_apply_paired_balance). It
    # shifts stock_core_session_position_fraction/stock_whole_share_core_
    # session_fraction in opposite directions by the same step so their
    # sum stays exactly 1.0 (a documented invariant in config.py:
    # together they're the entire per-cycle entry budget - drifting the
    # sum would leave capital unintentionally idle, directly against
    # "all the money gets invested"), which a single-field LeverSpec
    # can't express.
}

# Enforced a second time here, independent of LEVER_SPECS only ever
# containing safe fields above - if anyone ever adds a lever whose field
# collides with one of these, this denylist stops it being usable rather
# than relying solely on LEVER_SPECS having been written correctly.
SAFETY_DENYLIST: frozenset[str] = frozenset(
    {
        "daily_loss_circuit_breaker_enabled",
        "daily_max_loss_dollars",
        "min_cash_reserve_dollars",
        "stop_loss_guard_trade_limit",
        "stop_loss_guard_lookback_seconds",
        "wash_sale_block_days",
        "live_trading_enabled",
    }
)

_PAIR_FIELD_A = "stock_core_session_position_fraction"
_PAIR_FIELD_B = "stock_whole_share_core_session_fraction"


@dataclass(frozen=True)
class AdjustmentResult:
    field: str
    old_value: Decimal
    new_value: Decimal
    # Set only for the fractional-vs-whole-share paired adjustment - the
    # opposite field's own old/new value, since one auto-apply cycle
    # changes both together.
    paired_field: str | None = None
    paired_old_value: Decimal | None = None
    paired_new_value: Decimal | None = None


class StrategyTuningState:
    """Tracks when each lever was last auto-adjusted, so a persistent bad
    signal can't flip the same dial every review cycle - see
    STRATEGY_TUNING_COOLDOWN_HOURS. Same small-JSON-state-file pattern
    already used by WashSaleTracker/DailyPnlTracker.
    """

    def __init__(self, state_file: str):
        self.path = Path(state_file)
        try:
            self._last_applied: dict[str, float] = json.loads(
                self.path.read_text(encoding="utf-8")
            )
        except (FileNotFoundError, json.JSONDecodeError):
            self._last_applied = {}

    def ready(self, lever: str, cooldown_hours: int) -> bool:
        last = self._last_applied.get(lever)
        if last is None:
            return True
        return time.time() - last >= cooldown_hours * 3600

    def record(self, lever: str) -> None:
        self._last_applied[lever] = time.time()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._last_applied), encoding="utf-8")


def _clamp(value: Decimal, minimum: Decimal, maximum: Decimal) -> Decimal:
    return max(minimum, min(maximum, value))


def apply_lever_adjustment(
    lever: str,
    direction: str,
    current_values: dict[str, Decimal],
    step_fraction: Decimal,
) -> AdjustmentResult | None:
    """Pure function: given the lever/direction a strategy-review
    suggestion named and the CURRENT value(s) of the field(s) it maps to
    (read by the caller from config.py, not here - this module has no
    knowledge of how config is loaded), returns the new value(s) to
    write, or None if there's nothing to do (already at the bound in
    that direction, or an enable/disable direction with no matching
    field for this lever).

    Never mutates anything itself - the caller is responsible for
    actually editing config.py and running the full verification suite
    before that edit is ever committed. Keeping this pure makes the
    lever math itself trivially testable without any file I/O.
    """
    if lever == "fractional-vs-whole-share balance":
        return _apply_paired_balance(direction, current_values, step_fraction)
    spec = LEVER_SPECS.get(lever)
    if spec is None:
        return None
    if spec.field in SAFETY_DENYLIST:
        return None
    if direction in ("enable", "disable"):
        # No numeric adjustment for an enable/disable direction - the
        # caller flips enabled_field itself (a plain bool) using
        # spec.enabled_field; nothing for this function to compute.
        return None
    current = current_values.get(spec.field)
    if current is None:
        return None
    raise_value = direction == "increase"
    if not spec.increase_raises:
        raise_value = not raise_value
    span = spec.maximum - spec.minimum
    step = span * step_fraction
    new_value = current + step if raise_value else current - step
    new_value = _clamp(new_value, spec.minimum, spec.maximum)
    if new_value == current:
        return None
    return AdjustmentResult(field=spec.field, old_value=current, new_value=new_value)


def _apply_paired_balance(
    direction: str,
    current_values: dict[str, Decimal],
    step_fraction: Decimal,
) -> AdjustmentResult | None:
    if direction not in ("increase", "decrease"):
        return None
    fractional = current_values.get(_PAIR_FIELD_A)
    whole_share = current_values.get(_PAIR_FIELD_B)
    if fractional is None or whole_share is None:
        return None
    # "increase" = shift toward fractional entries.
    step = min(Decimal("1"), max(Decimal("0"), step_fraction))
    if direction == "decrease":
        step = -step
    new_fractional = _clamp(fractional + step, Decimal("0"), Decimal("1"))
    actual_step = new_fractional - fractional
    new_whole_share = _clamp(whole_share - actual_step, Decimal("0"), Decimal("1"))
    # Keep the sum exactly 1.0 even if new_whole_share itself had to clamp
    # (e.g. whole_share was already near 0) - re-derive fractional from
    # whatever whole_share ended up at, rather than trusting actual_step
    # a second time.
    new_fractional = Decimal("1") - new_whole_share
    if new_fractional == fractional and new_whole_share == whole_share:
        return None
    return AdjustmentResult(
        field=_PAIR_FIELD_A,
        old_value=fractional,
        new_value=new_fractional,
        paired_field=_PAIR_FIELD_B,
        paired_old_value=whole_share,
        paired_new_value=new_whole_share,
    )
