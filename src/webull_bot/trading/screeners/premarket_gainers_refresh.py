import logging
from datetime import datetime

log = logging.getLogger("webull-bot")


def refresh_premarket_gainers(self, moment: datetime) -> None:
    """By request: "get the top gainers before the day starts and
    look to invest in that for quick profit." Distinct from
    safe_top_gainers (Webull's default DAY_1/regular-session
    ranking, already folded anonymously into the once-daily full
    universe download) - this uses Webull's own rank_type=
    "PRE_MARKET" screener, fetched once per day as early as this
    method gets called (well before market_open in practice, since
    run() calls it before its own market_open gate below), so
    today's actual pre-market movers get a head start instead of
    competing for attention with the other several thousand
    symbols in the general scan rotation.

    Feeds seed_popular_symbols (POPULAR-bucket eligible, so these
    can trade in extended hours too - see the "only established/
    popular symbols trade outside core hours" gate in trade_
    stocks) and gets merged into stock_symbols/stock_categories
    directly (a symbol might not already be in the fast initial
    universe load) so trade_stocks' own force_scan mechanism
    (already used for the volatility-scalp cohort) can pick it up
    below.
    """
    if self.premarket_gainers_date == moment.date():
        return
    if self.config.premarket_gainers_limit <= 0:
        self.premarket_gainers_date = moment.date()
        return
    gainers = self.safe_premarket_gainers(
        self.config.premarket_gainers_limit,
        self.config.stock_universe_page_size,
    )
    self.premarket_gainers_date = moment.date()
    if not gainers:
        return
    self.premarket_gainers = {str(symbol).upper() for symbol in gainers}
    self.seed_popular_symbols |= self.premarket_gainers
    new_to_universe = [
        symbol
        for symbol in self.premarket_gainers
        if symbol not in self.stock_categories
    ]
    for symbol in new_to_universe:
        self.stock_categories[symbol] = "US_STOCK"
    if new_to_universe:
        self.stock_symbols = self.stock_symbols + new_to_universe
    log.info(
        "LOAD   | pre-market gainers | %s symbols (%s new to the "
        "universe) | %s",
        len(self.premarket_gainers),
        len(new_to_universe),
        ",".join(sorted(self.premarket_gainers)[:10]),
    )
