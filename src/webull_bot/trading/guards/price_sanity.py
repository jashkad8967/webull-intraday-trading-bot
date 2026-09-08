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
