def options_priority_window_active(
    minutes_since_open: float,
    priority_minutes: float,
    core_session_active: bool,
) -> bool:
    """By request: "keep it separate, all the bp should be for option,
    then at 9am cst whatever is remaining can be used for stocks."
    True for the first priority_minutes of the core session - blocks
    FRESH stock entries only (same scope as fresh_entry_blackout_active,
    not averaging down, not any exit) so options get first claim on
    the account's buying power before stock positions start consuming
    any of it. Always False once core_session_active is already False,
    same "only established/popular symbols trade outside core hours"
    convention fresh_entry_blackout_active already follows.
    """
    return (
        core_session_active
        and 0 <= minutes_since_open < priority_minutes
    )
