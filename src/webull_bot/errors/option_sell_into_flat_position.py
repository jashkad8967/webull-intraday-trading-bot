_FLAT_POSITION_CODES = (
    # "Close intent mismatches position direction."
    "OPENAPI_POSITION_ORDER_INTENT_MISMATCH",
    # "You hold an insufficient number of underlying shares to sell
    # this covered call." - Webull reads a SELL with no long contract
    # behind it as WRITING a call, which needs stock to cover.
    "OPENAPI_OPTION_CAVERED_CALL_STOCK_NO_ENOUGH",
    # "You can not place order in excess of current holding quantity to
    # create a position on the other side of the market."
    "OPENAPI_OPTION_LONG_POSITION_MUST_BE_CLOSE_THAN_SELL_SHORT",
    # "This order cannot be entered because it will reverse an existing
    # position." - same cause seen from the stall sweep.
    "OPENAPI_ORDER_NOT_SUPPORT_REVERSE_OPTION",
)


def is_option_sell_into_flat_position(exc: Exception) -> bool:
    """True for the family of Webull option rejections that all mean
    one thing: the broker does not hold the long contract our cached
    view thinks it does.

    The option-side twin of is_sell_with_no_position, which only ever
    matched the stock code ("NO_POSITION") and so never fired for any
    of these - none of them contain that substring.

    Live 2026-09-28..10-01: this hit 15 times across four sessions, at
    ERROR, always 5-36 seconds after a fill warning on the same
    contract. evaluate_held_option_exits reads self.cached_positions,
    refreshed once per SLOW scan; a sell that fills between refreshes
    leaves the contract still listed as held, both the
    pending_option_exits and has_pending_sell_order guards clear
    (the order is gone - it FILLED), and the fast loop submits a
    second sell into a position that is already flat. Webull's
    rejection is what prevents the double-sell.

    Benign and self-resolving, exactly as on the stock side - but
    logged as an unexplained ERROR it masked real failures, and it is
    why this looked for days like another bare-underlying/OCC key
    mismatch rather than a stale-cache race.

    Note the third code can ALSO mean a genuine over-sell (quantity
    larger than the holding) rather than a flat position. Both readings
    have the same root cause - our cached quantity is wrong - and the
    same correct response, which is to stop trusting it and re-read.
    The broker code is kept in the log line so a real sizing bug stays
    greppable instead of vanishing into a generic warning.
    """
    text = str(exc).upper()
    return any(code in text for code in _FLAT_POSITION_CODES)
