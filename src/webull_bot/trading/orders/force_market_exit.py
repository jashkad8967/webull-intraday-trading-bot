def should_force_market_exit(
    self, symbol: str, exit_is_fractional: bool, core_session_active: bool
) -> bool:
    """True once a symbol's exit has failed to fill
    CONSECUTIVE_EXIT_FAILURE_MARKET_THRESHOLD times in a row (see
    consecutive_exit_failures) - the next attempt should use a
    genuine MARKET order instead of another limit, guaranteed to
    fill and end the loop. Same MARKET-order eligibility constraints
    as a manual sell: whole-share, core hours, account supports it.
    """
    return (
        self.consecutive_exit_failures.get(symbol, 0)
        >= self.config.consecutive_exit_failure_market_threshold
        and core_session_active
        and not exit_is_fractional
        and self.fractional_trading_enabled
    )
