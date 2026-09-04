def is_short_selling_unsupported(exc: Exception) -> bool:
    """True for Webull's OAUTH_OPENAPI_NEW_NO_POSITION_MARGIN_ACCOUNT_
    CAN_NOT_SELL_SHORT_FOR_LT_2K rejection - short selling requires at
    least $2,000 in account equity (a standard margin-account
    minimum, not something specific to one security), so every short
    attempt keeps failing identically until equity grows past that
    threshold. Retrying changes nothing here, same reasoning as
    is_fractional_trading_not_enabled.
    """
    return "CAN_NOT_SELL_SHORT_FOR_LT_2K" in str(exc).upper()
