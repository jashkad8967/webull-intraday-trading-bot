from decimal import ROUND_DOWN, Decimal


def _stall_exit_price(
    self,
    quote: dict,
    average_cost: Decimal,
    min_profit: Decimal,
    fee_per_share: Decimal,
    max_spread_percent: Decimal | None = None,
) -> Decimal | None:
    """Pick the best available green exit price for a stalled position.

    Prefers the bid (fills immediately) whenever it alone clears cost +
    min_profit + fee. If the bid doesn't clear but the ask (top of the
    spread) does, rest a passive limit there instead of giving up - a
    stalled position sitting inside the spread shouldn't be abandoned
    just because the aggressive/immediate price isn't green yet. Never
    prices below cost + min_profit + fee on either side, so this can
    only ever produce a genuinely profitable exit or no exit at all.

    max_spread_percent defaults to stock_entry_max_spread_percent (the
    stall-breaker's own long-standing bound, tuned for a quote-glitch
    on an otherwise normal, liquid stock - see the TBB incident
    below). Callers whose positions are deliberately choppy/wide-
    spread by their own selection criterion (the volatility-scalp
    cohort) should pass a wider bound explicitly - live incident:
    GAUZ routinely quoted 2-7% spreads (its normal character, not a
    glitch), so the 0.50% default meant the ask-fallback almost
    never fired, exits depended entirely on the bid alone clearing
    cost, and the strategy kept averaging into new dip-buys (a much
    looser bar) far faster than it could ever exit.
    """
    if max_spread_percent is None:
        max_spread_percent = self.config.stock_entry_max_spread_percent
    floor = average_cost + min_profit + fee_per_share
    bid = self.api.quote_bid(quote)
    if bid is not None:
        # By request: these smaller/cheaper stocks have real
        # sub-penny precision (a live quote showed bid=0.4592) -
        # quantizing to a flat cent throws away real value on
        # exactly the stocks where a cent is a meaningful fraction
        # of the price. See WebullAPI.price_tick_size.
        sell_price = bid.quantize(
            self.api.price_tick_size(bid), rounding=ROUND_DOWN
        )
        if sell_price >= floor:
            return sell_price
    ask = self.api.quote_ask(quote)
    if ask is not None:
        # A resting limit at the ask only has a realistic chance of
        # filling if the spread itself is reasonably tight - the same
        # bound entries are already held to (entry_spread_ok). On a
        # thin/illiquid name with an artificially wide quoted spread,
        # the ask can sit far above where the stock is actually
        # trading (live incident: TBB quoted bid=19.39/ask=19.89 while
        # prints were at 19.41) - resting there submits an order that
        # can never fill, times out, and gets resubmitted at the
        # identical unreachable price every stall cycle, forever.
        # Skip the fallback entirely in that case and wait for the
        # spread to normalize instead of spinning on a doomed order.
        if bid is not None and bid > 0:
            spread_percent = (ask - bid) / bid * 100
            if spread_percent > max_spread_percent:
                return None
        sell_price = ask.quantize(
            self.api.price_tick_size(ask), rounding=ROUND_DOWN
        )
        # By request: "you cannot always go to the top of the spread
        # when it is big, you must ask a reasonable price, not too
        # far from the last [trade]." Even within max_spread_percent,
        # the raw ask can still sit meaningfully far from where the
        # stock is actually trading on a genuinely wide (not just
        # glitchy) spread - the exact TBB pattern above, just not
        # extreme enough to trip the spread-sanity skip entirely.
        # Caps the fallback at half the allowed spread's distance
        # above the last print instead of the literal ask, so a big
        # spread means "rest closer to reality," not "chase the far
        # edge of the book."
        try:
            last_price = self.api.quote_price(quote)
        except Exception:
            last_price = None
        if last_price and last_price > 0:
            reasonable_cap = last_price * (
                Decimal("1") + max_spread_percent / Decimal("200")
            )
            sell_price = min(
                sell_price,
                reasonable_cap.quantize(
                    self.api.price_tick_size(reasonable_cap),
                    rounding=ROUND_DOWN,
                ),
            )
        if sell_price >= floor:
            return sell_price
    return None
