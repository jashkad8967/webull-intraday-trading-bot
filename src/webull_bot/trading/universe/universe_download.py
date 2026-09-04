import logging

log = logging.getLogger("webull-bot")


def _download_and_filter_universe(
    self, limit: int, pool: int
) -> tuple[dict[str, str], list[str], list[str]]:
    """The "STOCK_SYMBOLS=ALL" universe download+filter pipeline,
    extracted as a pure(ish) helper (reads self.invalid_symbols/
    self.config only, never mutates self.stock_symbols/
    self.stock_categories itself) so it can run more than once per
    day at different sizes - see _resolve_targets_work_body (the
    fast initial pass) and _grow_stock_universe (the background
    continuation toward the full universe). Returns (categories,
    stock_symbols, reserve_symbols).
    """
    log.info(
        "LOAD   | downloading stocks and ETFs | limit=%s | pool=%s",
        limit,
        pool,
    )
    categories = self.api.stock_universe(
        lambda category, count, category_limit: log.info(
            "LOAD   | %-8s | %s/%s",
            category,
            count,
            category_limit or "ALL",
        ),
        limit=pool,
    )
    preferred = self.config.popular_stocks()
    preferred_categories = self.api.stock_categories(preferred)
    added = 0
    for symbol in preferred:
        if symbol not in categories and symbol in preferred_categories:
            categories[symbol] = preferred_categories[symbol]
            added += 1
    if added:
        log.info(
            "LOAD   | added %s popular symbols outside directory cap",
            added,
        )
    if self.config.top_gainers_limit > 0:
        gainers = self.safe_top_gainers(
            self.config.top_gainers_limit,
            self.config.stock_universe_page_size,
        )
        gainers_added = 0
        for symbol in gainers:
            if symbol not in categories:
                categories[symbol] = "US_STOCK"
                gainers_added += 1
        if gainers_added:
            log.info(
                "LOAD   | added %s top-gainer symbols outside directory cap",
                gainers_added,
            )
    if self.config.exclude_etfs:
        etfs = [
            symbol
            for symbol, category in categories.items()
            if category == "US_ETF"
        ]
        for symbol in etfs:
            categories.pop(symbol, None)
        if etfs:
            log.info("LOAD   | excluded %s ETFs", len(etfs))
    for symbol in self.invalid_symbols.symbols:
        categories.pop(symbol, None)
    eligible = [
        symbol for symbol in categories if symbol not in self.invalid_symbols
    ]
    eligible = self.filter_with_popular_reinstated(eligible)
    return categories, eligible[:limit], eligible[limit:]
