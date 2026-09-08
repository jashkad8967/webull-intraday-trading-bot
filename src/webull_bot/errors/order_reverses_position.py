def is_order_reverses_existing_position(exc: Exception) -> bool:
    """True for Webull's OPENAPI_ORDER_NOT_SUPPORT_REVERSE_OPTION
    rejection - despite the "OPTION" in the error code name, this
    fires for stock orders too. Live incident: escalate_stalled_stop_
    losses cancels a stalled exit order, then immediately submits a
    fresh aggressive-crossing sell on the same cycle - Webull's cancel
    acknowledgement doesn't guarantee the order has actually cleared
    its own book yet, so the new sell can land while the just-
    cancelled one is still technically open, and gets rejected as
    "would reverse an existing position." A benign timing race, not a
    fault: the position still has a fresh, uncontested stop order
    submitted moments later via the normal (non-escalation) exit path
    on the very next fast-loop cycle, same "expected, not a fault"
    convention as is_order_not_cancelable.
    """
    return "NOT_SUPPORT_REVERSE" in str(exc).upper()
