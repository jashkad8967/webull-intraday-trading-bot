def is_sell_with_no_position(exc: Exception) -> bool:
    """True for Webull's OPENAPI_NEW_NO_POSITION_MARGIN_ACCOUNT_CAN_
    NOT_SELL_SHORT_FOR_LT_2K rejection - the same documented "our
    cached view of a position still shows shares the broker already
    knows are gone" race record_trade's own PROFIT/STOP/MANUAL_SELL
    zeroing was built for (live incident: INN, OIS, RDHL, SOAR), but
    surfacing here from a DIFFERENT angle: evaluate_held_stock_exits
    reads self.cached_positions, which only gets overwritten once per
    SLOW scan cycle - if that slow refresh lands with the broker's
    own account state not yet caught up to a fill that just happened
    moments earlier, cached_positions can show a symbol as held again
    even though it's genuinely already flat, and the fast loop tries
    to sell it a second time. Webull correctly reads "sell with
    nothing held" as an attempted short and rejects it - a benign,
    self-resolving race (the position really is closed, which is
    exactly what the bot wanted), not a fault needing investigation.
    """
    return "NO_POSITION" in str(exc).upper()
