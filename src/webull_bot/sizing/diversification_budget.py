from decimal import Decimal


def diversification_capped_entry_budget(
    buying_power: Decimal,
    max_position_fraction: Decimal,
    min_notional_floor: Decimal,
) -> Decimal:
    """By request: "out of 7500 stocks it should easily be able to
    find enough stocks to invest everything" - a real per-position
    diversification cap (max_position_fraction of the CURRENT,
    already cycle-shrinking buying_power - see
    stock_max_position_fraction_of_buying_power), so one candidate
    can't absorb most of a bucket's whole allocation the way a
    live FDX entry did (~43% of the whole account in one trade).

    Live evidence this needed a floor: with buying_power already
    down to ~$45 later in the same cycle, 15% of that is ~$6.75 -
    well under fractional_shares_min_notional ($25 default) - a
    rigid fraction alone would have STARVED further deployment
    entirely below that floor (a cap that can never be satisfied
    isn't diversification, it's accidentally stranding capital),
    directly contradicting "100% of buying power should be used."
    Once the fraction alone would fall under min_notional_floor,
    allows up to the smaller of (the floor) or (everything that's
    actually left) instead - still meaningfully smaller than the
    full remaining balance on anything but the very last sliver of
    it, but never zeroed out by the cap alone.
    """
    cap = buying_power * max_position_fraction
    if cap < min_notional_floor:
        cap = min(buying_power, min_notional_floor)
    return cap
