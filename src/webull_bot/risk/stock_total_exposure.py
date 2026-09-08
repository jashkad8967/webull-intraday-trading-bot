from decimal import Decimal


def stock_total_exposure_at_cap(
    account_value: Decimal | None,
    positions: list[dict],
    max_fraction: Decimal,
) -> bool:
    """By request: "do not allow more than 20% in stocks." True once
    the current total market value of every held EQUITY position
    (cost-basis estimate, same convention volatility_scalp_total_
    exposure_ok already uses) already meets or exceeds max_fraction of
    account value - blocks FRESH stock entries only (general BUY/
    SHORT and volatility-scalp fresh entries/averaging-down, same
    scope as fresh_entry_blackout_active, whose gate this feeds into
    at every one of its existing call sites), never any exit. A
    checked-before-adding ceiling, not a per-trade "would this push us
    over" projection - simpler, and errs toward blocking one trade too
    many rather than one too few on a hard allocation limit like this.
    Fails open (False - not at cap, doesn't block) if account value
    isn't cached yet - same "missing data should never itself halt
    trading" convention every other gate in this codebase follows.
    """
    if account_value is None or account_value <= 0:
        return False
    total = Decimal("0")
    for position in positions:
        if position.get("instrument_type") != "EQUITY":
            continue
        quantity = Decimal(str(position.get("quantity", "0") or "0"))
        cost_price = Decimal(str(position.get("cost_price") or "0"))
        total += quantity * cost_price
    cap = account_value * max_fraction
    return total >= cap
