def is_order_not_cancelable(exc: Exception) -> bool:
    """True for Webull's OPENAPI_ORDER_CAN_NOT_CANCEL rejection - a
    benign race, not a fault: the order is already filling or has
    just filled by the time a repricer/escalator tries to cancel
    it. The working order will resolve itself (fill and drop out
    of open_ids, or genuinely still be cancelable) on the next
    monitor_working_orders poll, so this is a WARNING, same
    "expected, not a fault" convention as QuoteUnavailableError -
    not an ERROR needing investigation.
    """
    return "ORDER_CAN_NOT_CANCEL" in str(exc).upper()
