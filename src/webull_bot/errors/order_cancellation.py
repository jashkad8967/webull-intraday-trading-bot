def is_order_not_cancelable(exc: Exception) -> bool:
    """True for Webull's OPENAPI_ORDER_CAN_NOT_(BE_)CANCEL rejection -
    a benign race, not a fault: the order is already filling or has
    just filled by the time a repricer/escalator tries to cancel
    it. The working order will resolve itself (fill and drop out
    of open_ids, or genuinely still be cancelable) on the next
    monitor_working_orders poll, so this is a WARNING, same
    "expected, not a fault" convention as QuoteUnavailableError -
    not an ERROR needing investigation.

    Live incident (this bug): the real error code Webull returns is
    OPENAPI_ORDER_CAN_NOT_BE_CANCEL (with "BE"), not the CAN_NOT_
    CANCEL this used to check for - so every real occurrence of this
    benign race was misclassified as an unrecognized ERROR instead
    of the WARNING it actually is, showing up as log noise on a
    resting option-entry repricer's routine cancel/replace race.
    Matches on the stable CAN_NOT and CANCEL fragments around
    whatever sits between them, so it's tolerant of either wording.
    """
    upper = str(exc).upper()
    return "CAN_NOT" in upper and "CANCEL" in upper
