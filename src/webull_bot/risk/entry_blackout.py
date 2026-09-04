def fresh_entry_blackout_active(
    minutes_until_close: float,
    blackout_minutes: float,
    core_session_active: bool,
) -> bool:
    """By request, after live evidence (WNW/WKHS stopping out
    shortly after core hours ended): true once fewer than
    blackout_minutes remain in the core session - blocks FRESH
    entries only (not averaging down, not any exit), since a
    brand-new position opened this close to the bell has almost no
    runway to reach its target before conditions change. Always
    False once core_session_active is already False - the existing
    "only established/popular symbols trade outside core hours"
    gate already covers that case, and a negative minutes_until_
    close (core hours already ended) shouldn't itself trigger this
    for a symbol that gate already lets through.
    """
    return (
        core_session_active
        and 0 <= minutes_until_close < blackout_minutes
    )
