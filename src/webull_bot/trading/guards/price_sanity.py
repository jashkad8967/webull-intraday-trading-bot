import logging
import time
from decimal import Decimal

log = logging.getLogger("webull-bot")

PRICE_SANITY_TOLERANCE = Decimal("0.05")
# By request: "make sure your buy and sell price will actually be
# executed inside the spread for options, similar to stocks" - stocks
# already ran every order through price_sanity_ok as a fat-finger
# backstop; options never did at all, a real gap given a stale/corrupt
# option quote can misprice a limit just as badly as a stock one can.
# A materially wider bound than the stock tolerance above - options
# routinely carry much wider REAL relative bid-ask spreads than stocks
# (a $1.00 contract quoting $0.90/$1.10 is a normal, liquid 20% spread,
# not a broken quote), so reusing the 5% stock tolerance here would
# reject a large share of genuinely fine option orders. Still hardcoded,
# not config, same "sanity backstop, not a tuning knob" reasoning as
# PRICE_SANITY_TOLERANCE - this catches a truly stale/corrupt quote
# (deviation far beyond even a wide real spread), not normal option
# pricing.
OPTION_PRICE_SANITY_TOLERANCE = Decimal("0.30")


def price_sanity_ok(
    self,
    symbol: str,
    last_price: Decimal,
    limit_price: Decimal,
    tolerance: Decimal = PRICE_SANITY_TOLERANCE,
) -> bool:
    """Fat-finger guard: reject a limit price that's implausibly far
    from the last observed trade price instead of trusting sizing/
    pricing math blindly. Catches a stale or corrupted quote producing
    a wildly wrong limit before it ever reaches the broker - hardcoded,
    not config, since this is a sanity backstop, not a tuning knob.
    tolerance defaults to the stock bound (PRICE_SANITY_TOLERANCE) but
    callers can pass a wider one - see OPTION_PRICE_SANITY_TOLERANCE.

    Records the rejection in price_sanity_rejected_at - see
    price_sanity_cooldown_ready. Live incident: one illiquid
    symbol's bid/ask sat consistently ~9-10% off its own last-trade
    price (a real market condition on a thin quote, not a bad
    broker read - past _sane_bid_or_ask's own, looser 8% tolerance,
    but still past this stricter 5% one), rejecting an entry attempt
    on essentially every single scan cycle for hours with no
    backoff between attempts and no symbol in the log line to even
    identify which stock it was.
    """
    if last_price <= 0:
        return True
    deviation = abs(limit_price - last_price) / last_price
    if deviation > tolerance:
        self.price_sanity_rejected_at[symbol] = time.monotonic()
        log.error(
            "GUARD  | %-8s | price sanity check failed | last=%.4f limit=%.4f "
            "deviation=%.1f%% (max %.0f%%) | order skipped",
            symbol,
            last_price,
            limit_price,
            deviation * 100,
            tolerance * 100,
        )
        return False
    return True


def option_entry_spread_ok(
    bid: Decimal | None, ask: Decimal | None, max_spread_percent: Decimal
) -> bool:
    """True while a contract's own bid/ask spread is tight enough to
    realistically liquidate later - by explicit request ("just do not
    trade contracts that are not easy to liquidify"). Live incident:
    ORCL was bought into, then its own STOP-loss could never find a
    buyer at any sane price (a genuine 40%+ bid/ask spread) and just
    kept resubmitting and timing out unfilled while the position sat
    exposed. This is a structural entry-side floor, not a fix for a
    stuck exit - a refusal to ever create one. Passes open (True) on
    a missing/invalid quote, matching every other gate's fail-open
    convention for missing data.
    """
    if not bid or not ask or bid <= 0:
        return True
    spread_percent = (ask - bid) / bid * 100
    return spread_percent <= max_spread_percent


def option_entry_breakeven_hurdle(
    bid: Decimal | None,
    ask: Decimal | None,
    sell_fee_per_share: Decimal,
) -> Decimal | None:
    """How far the BID must rise, as a fraction of itself, before the
    position is merely FLAT after the sell fee.

    This is the cost of the round trip, paid before direction matters
    at all. We buy at the mid, we sell into the bid, so the position
    opens half a spread underwater and owes the exit fee on top.

    Returns None on a missing/unusable quote, matching the fail-open
    convention of every other gate here.
    """
    if not bid or not ask or bid <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2
    return (mid + sell_fee_per_share - bid) / bid


def option_entry_breakeven_room_ok(
    bid: Decimal | None,
    ask: Decimal | None,
    stop_loss_percent: Decimal,
    sell_fee_per_share: Decimal,
    max_hurdle_fraction: Decimal,
) -> bool:
    """True while the round-trip cost leaves usable room inside the
    stop-loss budget.

    This is a DIFFERENT question from option_entry_spread_ok, which
    asks "is this contract liquid enough to get out of?" (and whose
    bound is reused on the exit path, so it must stay loose). This
    asks "can a position in this contract profit before its own stop
    kills it?" A contract can be perfectly liquid and still fail this.

    Measured 2026-10-01 against the recorded tape, the reason four
    straight sessions lost money with every mechanical defect already
    fixed: across 10 reconstructed positions the mean hurdle was 8.1%
    against a 10% stop - 1.9% of favourable room. NINE of the ten
    never traded above cost+fee on the bid at any point in their
    lives, so no exit rule could have won them; sweeping the trail
    and the take-profit across six configurations moved the total
    between -$44 and -$54 and never into profit. Three had a hurdle
    at or BEYOND the stop: guaranteed losses at the moment of entry.

    The arithmetic is driven by the $0.05 option tick, which is fixed
    in cents and therefore punishing on cheap contracts - 10% of a
    $0.50 premium, 2.5% of a $2.00 one. That is why this gate exists
    rather than a premium floor: it is the hurdle that matters, and
    on a penny-pilot name a cheap contract can pass it honestly.

    max_hurdle_fraction is the share of the stop budget friction may
    consume. At 1.0 this rejects only trades whose breakeven sits at
    or past their own stop. Below ~0.5 it demands that most of the
    risk budget remain available to the trade itself.
    """
    hurdle = option_entry_breakeven_hurdle(bid, ask, sell_fee_per_share)
    if hurdle is None:
        return True
    if stop_loss_percent <= 0:
        return True
    return hurdle <= stop_loss_percent * max_hurdle_fraction


def option_stop_implied_underlying_move(
    premium: Decimal | None,
    delta: Decimal | None,
    underlying_price: Decimal | None,
    stop_loss_percent: Decimal,
) -> Decimal | None:
    """How far the UNDERLYING must move, as a fraction of its own price,
    for an option stop to fire.

    An option is levered to its underlying by premium/(delta x spot) -
    roughly 35x for a cheap at-the-money contract - so a stop expressed
    as a percentage of PREMIUM says almost nothing about how big a move
    it actually tolerates. This converts it into the only terms that
    matter: what the market has to do.

    Returns None when any input is missing or unusable, matching the
    fail-open convention of every other gate here.
    """
    if not premium or not delta or not underlying_price:
        return None
    if premium <= 0 or underlying_price <= 0 or abs(delta) <= 0:
        return None
    implied_dollars = (premium * stop_loss_percent) / abs(delta)
    return implied_dollars / underlying_price


def option_stop_survives_noise(
    premium: Decimal | None,
    delta: Decimal | None,
    underlying_price: Decimal | None,
    stop_loss_percent: Decimal,
    min_underlying_move_percent: Decimal,
) -> bool:
    """True while the stop describes a real move rather than noise.

    Measured live 2026-10-01, at a cost of $7.07 in 150 seconds:

        PFE261009C00028000  bought ~0.50, STOP filled 0.43
        delta 0.62, PFE spot 28.21
        -> a 10% option stop is a 0.29% move in PFE

    PFE moves 0.29% many times an hour, so the position was not stopped
    out by an adverse trend, it was stopped out by the next tick. 8.3% of
    the account, gone, on a contract that passed every other gate -
    liquidity, spread, delta (0.62, comfortably above the 0.20 floor) and
    the round-trip-cost hurdle.

    This is the defect behind everything previously blamed on exit logic:
    the held=0s exits, the 9-of-10 positions that never traded above
    cost+fee on the bid, and the 5% stop that cost $60.98 in 37 minutes
    (5% of premium = a 0.13% underlying move, so every position was dead
    the moment it filled). The exit ladder was fine. The stop was
    mis-scaled by a factor of ~35 and had been the whole time.

    Note what this does NOT do: it never widens a stop or increases
    risk. It refuses entries whose stop cannot work, which is the only
    safe direction to change under pressure. It is also self-correcting
    - scale option_stop_loss_percent to the instrument (~35% of premium
    for a contract like PFE's) and these entries pass on their own.
    """
    implied = option_stop_implied_underlying_move(
        premium, delta, underlying_price, stop_loss_percent
    )
    if implied is None:
        return True
    return implied >= min_underlying_move_percent


def price_sanity_cooldown_ready(self, symbol: str) -> bool:
    """False while symbol is still within PRICE_SANITY_COOLDOWN_SECONDS
    of its last price_sanity_ok rejection - without this, a symbol
    whose quote sits just past the sanity tolerance gets retried
    (and re-rejected) on literally every scan cycle forever, wasting
    a batch slot another, viable candidate could have used instead.

    Live incident (this bug): originally entry-only (the docstring
    used to claim "unlike the exit side's stalled-order backstops,
    this only ever backs off" - that assumption was wrong in
    practice). BMEA's profit-take order re-escalated and resubmitted
    every ~15-20s continuously for over 5 HOURS, hitting this exact
    price-sanity rejection ~570 times with zero backoff, because
    place_stock_scaled itself never checked this cooldown - only the
    entry code paths checked it themselves, before ever calling
    place_stock_scaled. Now enforced directly inside
    place_stock_scaled, so it applies uniformly to every order this
    function submits - entries AND exits alike - not just whichever
    callers happened to remember to check it first.
    """
    rejected_at = self.price_sanity_rejected_at.get(symbol)
    if rejected_at is None:
        return True
    return (
        time.monotonic() - rejected_at
        >= float(self.config.price_sanity_cooldown_seconds)
    )
