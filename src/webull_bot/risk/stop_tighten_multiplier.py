from decimal import Decimal


def stop_tighten_multiplier(
    daily_realized_pnl: Decimal,
    account_value: Decimal | None,
    threshold_fraction: Decimal,
    tighten_multiplier: Decimal,
) -> Decimal:
    """By request: "when we have a certain profit we should also
    not allow stops to be too low" - the same "significantly
    ahead for the day" trigger as profit_target_multiplier above,
    but tightens the general path's stop distance (< 1, e.g. 0.7 =
    30% tighter) instead of widening the target - once the account
    is already ahead, a reversal shouldn't be allowed to give back
    as much of that lead as a normal-conditions stop would permit.
    Same fail-safe behavior (returns 1, no change) with an
    unknown/non-positive account value.
    """
    if account_value is None or account_value <= 0:
        return Decimal("1")
    if daily_realized_pnl >= account_value * threshold_fraction:
        return tighten_multiplier
    return Decimal("1")
