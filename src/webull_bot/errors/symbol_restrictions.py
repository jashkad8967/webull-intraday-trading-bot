def is_symbol_restricted_to_closing_only(exc: Exception) -> bool:
    """True for Webull's OAUTH_OPENAPI_CAN_NOT_CREATE_A_OPEN_ORDER
    rejection ("This symbol is restricted to closing orders only") -
    a per-security, broker-side restriction (e.g. a halt, an
    emergency SSR-style curb, being pulled from tradability) that
    deterministically rejects every single opening order for this
    symbol, exactly the same way, for as long as it's in effect.
    Live incident: RFAI. Retrying changes nothing, same reasoning as
    is_short_selling_unsupported/is_fractional_ticker_unsupported -
    letting it accumulate toward the generic order-error-rate
    blacklist (5 errors in a shared, cross-symbol window) wastes
    several futile attempts and API calls first, and risks that
    window tripping on account of an unrelated symbol instead.
    """
    return "CAN_NOT_CREATE_A_OPEN_ORDER" in str(exc).upper()
