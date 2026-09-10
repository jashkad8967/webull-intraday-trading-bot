def is_otc_extended_hours_unsupported(exc: Exception) -> bool:
    """True for Webull's OPENAPI_OTC_TICKER_NOT_SUPPORT_X_P rejection -
    an OTC-listed security simply has no extended-hours (pre/post
    market) session at all, unlike NYSE/NASDAQ-listed names. This is
    a per-security, permanent structural restriction, not a fault -
    retrying the same symbol during extended hours will always fail
    the same way; a core-hours attempt on the same symbol is
    unaffected. stock_categories only distinguishes US_STOCK/US_ETF
    (falling back to US_STOCK for anything unmatched, including OTC
    names), so there's no proactive way to know a symbol is OTC
    before this rejection actually surfaces once.
    """
    return "OTC_TICKER_NOT_SUPPORT" in str(exc).upper()
