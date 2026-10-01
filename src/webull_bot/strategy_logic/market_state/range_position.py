"""Where price sits inside today's range - the difference between a
breakout and a failing gap.

ENCODED FROM LIVE DISCRETIONARY TRADING, 2026-10-01. On a day the bot's
own gates produced one losing option trade, two manual share entries were
taken and both closed green (NVDA +$0.05, +$0.01). The criterion used was
not in the bot: position within the day's range.

What it accepted and what it refused, same session, real numbers:

    NVDA   +1.32%  range  87%  -> taken, closed green
    NVDA   +1.52%  range  89%  -> taken, closed green
    PLTR   +1.92%  range  78%  -> would take
    ACN   +17.95%  range  14%  -> REFUSED
    GOOGL  -1.43%  range  21%  -> REFUSED

ACN is the case that matters. It gapped +18%, sat in the day's top four
by the bot's own priority_score, and had already faded from 227.58 to
216.28 - a failing gap, 14% off its low. GOOGL scored 29.6, second
highest, having opened at 351.44 and sold down to 339.16. Both would have
been attractive on gap and score alone, which is all the existing gates
measure. Range position is what separated them.

This is NOT range_ratio, which already exists: that is (high - low) /
price, a measure of how WIDE the day's range is (volatility), used only
to scale the stop. This is where inside that range the current price
actually sits. A name can be highly volatile and sitting on its low.

Costs no API calls - high, low and price are already in self.metrics and
self.prices from the regular snapshot.

Honest limit on the evidence: one session, five names. The default
threshold is therefore set at "upper half of the day's range", which is
the weakest defensible statement of "not currently fading", rather than a
value tuned to these particular trades. It cleanly separates the set
above (lowest accepted 78%, highest refused 21%) with wide margin either
side, so precision is not what is being claimed.
"""

from decimal import Decimal


def range_position(self, symbol: str) -> Decimal | None:
    """Price's position in today's high/low range, 0 (on the low) to 1
    (on the high).

    Returns None when the range is unusable - no snapshot yet, or a high
    equal to the low (a symbol that has not moved, or a single-print
    quote). Callers treat None as "unknown" and fall open, matching every
    other best-effort gate in this codebase.
    """
    metrics = self.metrics.get(symbol) or {}
    high = metrics.get("high")
    low = metrics.get("low")
    price = self.prices.get(symbol)
    if price is None or high is None or low is None:
        return None
    try:
        high = Decimal(str(high))
        low = Decimal(str(low))
        price = Decimal(str(price))
    except Exception:
        return None
    if high <= low or price <= 0:
        return None
    position = (price - low) / (high - low)
    # Clamp: a quote can print fractionally outside the session high/low
    # recorded in the same snapshot.
    if position < 0:
        return Decimal("0")
    if position > 1:
        return Decimal("1")
    return position


def range_position_supports_long(self, symbol: str, minimum: Decimal) -> bool:
    """True while price is high enough in today's range to be buying
    strength rather than catching a fade.

    LONG ONLY, deliberately. A short entry wants the mirror image - price
    low in its range - so this must not be reused as a generic gate if
    short selling is ever re-enabled (it is currently disabled below
    Webull's $2000 equity minimum). Writing it as a one-sided test rather
    than a band keeps that asymmetry explicit instead of silently
    inverting on the short side.
    """
    if minimum <= 0:
        return True
    position = range_position(self, symbol)
    if position is None:
        return True
    return position >= minimum
