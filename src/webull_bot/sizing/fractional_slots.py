from decimal import Decimal


def max_fractional_position_slots(
    max_open_positions: int,
    fractional_fraction: Decimal,
    whole_share_fraction: Decimal,
) -> int:
    """Caps how many concurrently-open fractional-quantity stock
    positions there can be, reserved in the same proportion as
    fractional's capital share. A fractional position can't be exited
    outside core hours (Webull constraint - see is_fractional_quantity
    gating in trade_stocks), so letting fractional entries alone fill
    every MAX_OPEN_POSITIONS slot during core hours would strand the
    account with an unexitable, maxed-out position count for the rest
    of the day - no new entries of any style until the next core
    session. At least 1 slot is always reserved when fractional
    capital is allocated at all.
    """
    capital_split = fractional_fraction + whole_share_fraction
    if capital_split <= 0:
        return max_open_positions
    return max(1, int(max_open_positions * fractional_fraction / capital_split))
