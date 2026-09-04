import logging
from datetime import datetime

log = logging.getLogger("webull-bot")


def refresh_agent_predicted_gainers(self, moment: datetime) -> None:
    """By request: "pre trading should not be too intense, and it
    should just set up the main gainers for the day, in fact have
    the research agent return stocks likely to be top gainers
    before core hours start, then put those stocks in some sort of
    priority list to also look at." Complementary to refresh_
    premarket_gainers (Webull's own real PRE_MARKET screener data)
    - this is the research agent's own speculative pick list, once
    per day, same "priority list" mechanism (seed_popular_symbols +
    merged into stock_symbols/stock_categories so trade_stocks'
    force_scan can pick it up).
    """
    if self.agent_predicted_gainers_date == moment.date():
        return
    self.agent_predicted_gainers_date = moment.date()
    if self.market_agent is None:
        return
    try:
        symbols = self.market_agent.predict_likely_gainers()
    except Exception as exc:
        log.warning("LOAD   | agent gainer prediction failed | %s", exc)
        return
    if not symbols:
        return
    self.agent_predicted_gainers = {str(symbol).upper() for symbol in symbols}
    self.seed_popular_symbols |= self.agent_predicted_gainers
    new_to_universe = [
        symbol
        for symbol in self.agent_predicted_gainers
        if symbol not in self.stock_categories
    ]
    for symbol in new_to_universe:
        self.stock_categories[symbol] = "US_STOCK"
    if new_to_universe:
        self.stock_symbols = self.stock_symbols + new_to_universe
    log.info(
        "LOAD   | agent-predicted gainers | %s symbols (%s new to "
        "the universe) | %s",
        len(self.agent_predicted_gainers),
        len(new_to_universe),
        ",".join(sorted(self.agent_predicted_gainers)),
    )
