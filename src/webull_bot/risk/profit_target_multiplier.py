from decimal import Decimal


def profit_target_multiplier(
    daily_realized_pnl: Decimal,
    account_value: Decimal | None,
    threshold_fraction: Decimal,
    widen_multiplier: Decimal,
) -> Decimal:
    """By request: "we basically just want to be able to stay in a
    significant profit until eod" -> "let winners run further
    before taking profit." Once today's realized pnl reaches
    threshold_fraction of account_value, returns widen_multiplier
    (applied to the general path's stop-scaled profit target in
    stock_decision) instead of 1 (no change). Fails safe (returns
    1) with an unknown/non-positive account value - never widens
    based on a stale or missing read.
    """
    if account_value is None or account_value <= 0:
        return Decimal("1")
    if daily_realized_pnl >= account_value * threshold_fraction:
        return widen_multiplier
    return Decimal("1")
