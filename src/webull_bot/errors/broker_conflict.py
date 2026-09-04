def is_broker_position_conflict(exc: Exception) -> bool:
    """True for Webull's "this order would reverse an existing
    position" rejection - a sign our local view of the position is out
    of sync with the broker's (a stale quantity, a partially-filled
    order, or account state from outside the bot). No amount of
    retrying with the same (wrong) assumption will fix this - it needs
    the account state to actually resolve, so the caller should stop
    hammering the symbol instead of just backing off and trying again.
    """
    return "REVERSE" in str(exc).upper()
