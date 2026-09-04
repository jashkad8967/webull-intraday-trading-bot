def is_fractional_trading_not_enabled(exc: Exception) -> bool:
    """True for Webull's OAUTH_OPENAPI_OPENAPI_FRACT_VERSION2_ACCOUNT_
    NOT_TRADE rejection - the account itself hasn't agreed to Webull's
    fractional-trading terms (a one-time click-through at a URL Webull
    includes in the error), so every fractional order will keep failing
    identically until that happens. Retrying changes nothing here.
    """
    return "FRACT_VERSION2_ACCOUNT_NOT_TRADE" in str(exc).upper()
