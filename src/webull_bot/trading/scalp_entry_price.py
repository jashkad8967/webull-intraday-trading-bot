from decimal import ROUND_UP, Decimal


def volatility_scalp_entry_price(self, quote: dict) -> Decimal | None:
    """Aggressive, cross-the-spread BUY price for the volatility-
    scalp cohort - by request: "a lot of the orders are being
    cancelled... ensure the initial order itself is likely to be
    filled." The general stock_limit_price(quote, "BUY") used
    everywhere else prices passively at the bid/ask midpoint - fine
    for the normal strategy's slower entries, but for a strategy
    whose whole point is fast, repeated round trips, a passive mid
    that the market has to fall back down to before it ever fills
    just sits for the full ORDER_TIMEOUT_SECONDS (120s) and gets
    cancelled without ever entering the position (live incident:
    several BUY orders cancelled "unfilled after 120s" in a row).
    Crosses at the ask instead - a real cost (paying the spread
    instead of resting inside it), but guarantees the order can
    actually fill immediately in virtually all cases, which is the
    whole point of a high-frequency strategy that depends on
    actually being in the position to catch the next move.
    reprice_volatility_scalp_entries still lowers this toward a
    falling market afterward, same as before.
    """
    ask = self.api.quote_ask(quote)
    if ask is None:
        return None
    return ask.quantize(self.api.price_tick_size(ask), rounding=ROUND_UP)
