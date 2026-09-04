def _broker_conflict(bot, symbol: str) -> bool:
    """True once a broker-side "position reverse" conflict has been
    flagged for symbol (see is_symbol_broker_conflict/CONFLICT log
    lines) - broker_conflict_symbols is documented (see its __init__
    comment) as skipping a symbol's exit management entirely, not just
    entries, but the fast-loop functions added today (evaluate_held_
    stock_exits, reprice_resting_exits, reprice_volatility_scalp_exits,
    escalate_stalled_stop_losses) never checked it - only the slow
    loop did. Live incident: PETZ got flagged CONFLICT at 11:40:06,
    then the fast loop kept trying to act on it anyway a minute later,
    hitting OPENAPI_NEW_NO_POSITION...CAN_NOT_SELL_SHORT three times in
    a row. Module-level, not a self-method, for the same test-fixture-
    compatibility reason as _manual_touch_active above - getattr
    defaults to "no conflict" (False) when a fixture never set
    broker_conflict_symbols at all.
    """
    return symbol in getattr(bot, "broker_conflict_symbols", set())
