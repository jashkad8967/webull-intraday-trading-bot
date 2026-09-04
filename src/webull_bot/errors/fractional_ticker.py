def is_fractional_ticker_unsupported(exc: Exception) -> bool:
    """True for Webull's OAUTH_OPENAPI_FRACT_TICKER_DONT_SUPPORT_TRADE
    rejection - unlike FRACT_VERSION2_ACCOUNT_NOT_TRADE (an account-
    wide agreement gate), this is a per-security restriction: some
    tickers just aren't fractional-eligible on Webull regardless of
    account status, and every other symbol is unaffected. Retrying
    the same symbol changes nothing; retrying a different one is fine.
    """
    return "FRACT_TICKER_DONT_SUPPORT_TRADE" in str(exc).upper()
