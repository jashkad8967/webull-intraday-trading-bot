"""Scale an option stop to the UNDERLYING's move, not to the premium.

THE DEFECT THIS REPLACES, measured live 2026-10-01 at a cost of $7.07 in
150 seconds:

    PFE261009C00028000, delta 0.6187, PFE spot 28.21, premium 0.50
    option_stop_loss_percent 10%  ->  a 0.29% move in PFE

PFE moves 0.29% many times an hour, so the position was not stopped out by
an adverse trend - it was stopped out by the next tick. That contract had
passed every gate the bot had: liquidity, entry spread, delta well above
the 0.20 floor, and the round-trip-cost hurdle. Nothing checked the stop
against the instrument it was applied to.

An option is levered to its underlying by premium / (delta x spot), about
35x for a cheap at-the-money contract. So a stop expressed as a percentage
of PREMIUM says almost nothing about how large a move it tolerates, and
the same percentage means something completely different on two different
contracts. Inverting the relationship fixes that:

    stop_fraction = target_underlying_move x |delta| x spot / premium

Now every position's stop corresponds to the SAME move in its underlying,
whatever the contract. For that PFE call a 0.5% target gives a 17.5% stop
rather than 10%.

This also explains, in one formula, the whole loss history previously
blamed on exit logic: the held=0s exits, the 9-of-10 positions that never
traded above cost+fee on the bid, the NKE stop that fired six minutes
after fill on a $0.15 premium, and why the 5% stop was catastrophic rather
than merely tight (5% of premium was a 0.13% underlying move - every
position dead on submission). Six exit-ladder configurations were swept
across the recorded tape hunting for this; none could have found it,
because the tape held neither delta nor spot.

WHAT THIS DELIBERATELY DOES NOT DO: clamp the stop down to fit a risk
budget. A stop shrunk to be affordable is the original defect wearing a
different number. When the correctly scaled stop costs more than the
account should risk, the correct response is to refuse the ENTRY - which
is the entry gate's job, not this function's.
"""

from decimal import Decimal

# Above this the stop is risking essentially the whole premium, and the
# contract is better declined than held on a stop that will not save
# anything. Returned as-is so the caller can see the real number and
# refuse; it is not silently applied.
IMPLAUSIBLE_STOP_FRACTION = Decimal("1.0")


def option_stop_fraction(
    premium: Decimal | None,
    delta: Decimal | None,
    underlying_price: Decimal | None,
    target_underlying_move: Decimal,
) -> Decimal | None:
    """The stop, as a fraction of premium, that corresponds to
    `target_underlying_move` in the underlying.

    Returns None when it cannot be computed - a missing delta (some
    account snapshots omit greeks), no underlying price, or a zero
    premium. Callers fall back to the configured flat percentage in that
    case, which preserves existing behaviour rather than leaving a
    position unprotected.
    """
    if not premium or not delta or not underlying_price:
        return None
    if premium <= 0 or underlying_price <= 0 or abs(delta) <= 0:
        return None
    if target_underlying_move <= 0:
        return None
    moved = target_underlying_move * abs(delta) * underlying_price
    return moved / premium


def option_stop_risk_dollars(
    premium: Decimal,
    quantity: int | Decimal,
    stop_fraction: Decimal,
    contract_multiplier: int = 100,
) -> Decimal:
    """What hitting that stop actually costs, in dollars.

    The number that makes the whole fix uncomfortable and must therefore
    be visible: a correctly scaled 17.5% stop on a $0.50 premium contract
    risks $8.75, which against $80 of buying power is 11% of the account
    on one trade - and one contract is the minimum size, so it cannot be
    reduced by trading smaller. Any honest version of this fix has to
    report that rather than tune it away.
    """
    return premium * Decimal(str(quantity)) * contract_multiplier * stop_fraction
