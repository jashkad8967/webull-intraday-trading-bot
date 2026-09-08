from decimal import Decimal, ROUND_DOWN, ROUND_UP

from webull_bot.api.errors import QuoteUnavailableError


def _bid_ask_last_midpoint(self, quote: dict, bid: Decimal, ask: Decimal) -> Decimal:
    """bid/ask midpoint, blended with the last-trade print when it
    actually falls within the current spread - a passive mid-price
    that only ever looked at the top of the book ignored where the
    market was actually trading a moment ago. Last-trade is a print
    from BEFORE this quote's bid/ask, though, so it's only trusted
    when it's still consistent with the current spread (inside
    [bid, ask]) - a stale or off-spread print pulling the price
    outside the real market would be worse than not using it at all,
    so that case falls back to the plain midpoint.
    """
    last = self._quote_decimal(quote, "price")
    if last is not None and bid <= last <= ask:
        return (bid + ask + last) / 3
    return (bid + ask) / 2


def price_tick_size(price: Decimal) -> Decimal:
    """The real increment a stock's price actually moves in - $0.0001
    under $1 (the standard US equity sub-penny allowance for stocks
    priced under a dollar), $0.01 at or above it. By request: these
    smaller/cheaper stocks quote with real 4-decimal precision (a
    live GAUZ quote showed bid=0.4592) - blanket-rounding every
    computed price to whole cents was throwing away up to a cent of
    real value per share on exactly the stocks where a cent is a
    meaningful fraction of the price.
    """
    return Decimal("0.0001") if price < 1 else Decimal("0.01")


def stock_limit_price(self, quote: dict, side: str) -> Decimal:
    offset = self.config.stock_limit_offset
    if side in ("BUY", "SHORT"):
        # A SHORT entry is priced the same passive way as a BUY entry
        # (mid-price) - it's a normal opening trade, not an urgent
        # exit, so it shouldn't use the aggressive crossing price the
        # `else` branch below is tuned for (that one's for buy-to-cover
        # unwinds and stop/profit exits that need a fast fill).
        bid = self._sane_bid_or_ask(quote, "bid")
        ask = self._sane_bid_or_ask(quote, "ask")
        if not bid or not ask or bid > ask:
            raise QuoteUnavailableError(
                "stock quote has no valid bid/ask spread for a buy"
            )
        price = self._bid_ask_last_midpoint(quote, bid, ask)
        price = max(Decimal("0.01"), price)
        return price.quantize(self.price_tick_size(price), rounding=ROUND_DOWN)
    elif side == "COVER":
        # Buying back a short to close it out is economically a BUY,
        # not a SELL - the aggressive-crossing `else` branch below
        # prices toward the bid (correct for an urgent SELL, backwards
        # for a BUY that needs to guarantee a fast fill). Cross above
        # the ask instead, mirroring the same offset/urgency the SELL
        # branch uses on the other side of the book.
        base = (
            self._sane_bid_or_ask(quote, "ask")
            or self._quote_decimal(quote, "price")
            or self._sane_bid_or_ask(quote, "bid")
        )
        price = base * (Decimal("1") + offset)
        price = max(Decimal("0.01"), price)
        return price.quantize(self.price_tick_size(price), rounding=ROUND_UP)
    else:
        base = (
            self._sane_bid_or_ask(quote, "bid")
            or self._quote_decimal(quote, "price")
            or self._sane_bid_or_ask(quote, "ask")
        )
        price = base * (Decimal("1") - offset)
    price = max(Decimal("0.01"), price)
    return price.quantize(self.price_tick_size(price), rounding=ROUND_UP)


def stock_stop_exit_price(self, quote: dict) -> Decimal:
    """Midpoint sell limit for a stop-loss exit.

    Deliberately gentler than stock_limit_price's SELL side (which
    crosses below the bid to guarantee a fill for closeouts): a
    stop-loss should cap the loss precisely rather than chase an
    immediate fill, since overshooting the bid on a fast-moving or
    thin quote is what turns a bounded stop into a much larger loss.
    """
    bid = self._sane_bid_or_ask(quote, "bid")
    ask = self._sane_bid_or_ask(quote, "ask")
    if not bid or not ask or bid > ask:
        raise QuoteUnavailableError(
            "stock quote has no valid bid/ask spread for a stop exit"
        )
    price = self._bid_ask_last_midpoint(quote, bid, ask)
    price = max(Decimal("0.01"), price)
    return price.quantize(self.price_tick_size(price), rounding=ROUND_DOWN)


def option_price_tick_size(premium: Decimal) -> Decimal:
    """The real increment an option premium is allowed to be quoted
    in - live incident: a real order attempt at $7.47 (AAPL, a
    premium >= $3) was rejected outright with OPENAPI_OPTION_
    PRICE_STEP_GTE ("Orders placed with a premium of $3 or more
    must be in increments of 0.05"). option_limit_price used to
    always round to a flat $0.01 regardless of premium level - this
    would have blocked every real option order whose premium ever
    cleared $3, silently until the first live attempt actually hit
    it (never verified live before this, since nothing had reached
    order placement yet).

    Live incident (this fix): a second, distinct rejection -
    OPENAPI_OPTION_PRICE_STEP_LT ("Orders placed with a premium of
    less than $3 must be in increments of 0.05") - hit repeatedly
    on a different underlying (CD), directly contradicting the
    $0.01-below-$3 assumption this used to make. Standard OCC tick
    rules only allow $0.01 increments below $3 for "Penny Pilot"
    underlyings; most names aren't enrolled, and Webull's API gives
    no cheap way to tell which is which ahead of a real order
    attempt. $0.05 is always a valid point on the $0.01 grid too,
    so quoting every premium at $0.05 (not just >= $3) satisfies
    both rule variants unconditionally, at the cost of the finer
    penny-level granularity Penny Pilot names could otherwise use.
    """
    return Decimal("0.05")


def _quantize_to_option_tick(
    cls, price: Decimal, rounding: str
) -> Decimal:
    tick = cls.option_price_tick_size(price)
    steps = (price / tick).quantize(Decimal("1"), rounding=rounding)
    return (steps * tick).quantize(Decimal("0.01"))


def option_limit_price(self, quote: dict, side: str) -> Decimal:
    offset = self.config.option_limit_offset
    if side == "BUY":
        bid = self._sane_bid_or_ask(quote, "bid")
        ask = self._sane_bid_or_ask(quote, "ask")
        if not bid or not ask or bid > ask:
            raise QuoteUnavailableError(
                "option quote has no valid bid/ask spread for a buy"
            )
        price = (bid + ask) / 2
        return self._quantize_to_option_tick(
            max(Decimal("0.01"), price), ROUND_DOWN
        )
    else:
        base = (
            self._sane_bid_or_ask(quote, "bid")
            or self._quote_decimal(quote, "price")
            or self._sane_bid_or_ask(quote, "ask")
        )
        price = base * (Decimal("1") - offset)
    return self._quantize_to_option_tick(
        max(Decimal("0.01"), price), ROUND_UP
    )
