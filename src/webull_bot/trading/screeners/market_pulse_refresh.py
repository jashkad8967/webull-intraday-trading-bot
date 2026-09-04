import time

MARKET_PULSE_REFRESH_SECONDS = 120


def refresh_market_pulse(self) -> None:
    """Small, fixed-size, fully deterministic market context (Webull's
    own gainers/losers/most-active screeners) refreshed on a slow,
    fixed cadence independent of the poll loop - this replaces asking
    the research agent to discover movers via open-ended web search,
    which was the actual source of unpredictable request size (and the
    occasional Groq 413). Each of the three lists is capped at
    AGENT_MARKET_PULSE_SYMBOLS, so the payload this feeds downstream
    never grows with market conditions. Uses its own small-list
    fallback (empty, not the prior universe) on a screener failure -
    this is market color, not the trading universe.
    """
    now = time.monotonic()
    if now - self.last_market_pulse_refresh < MARKET_PULSE_REFRESH_SECONDS:
        return
    self.last_market_pulse_refresh = now
    limit = self.config.agent_market_pulse_symbols
    gainers = self.safe_top_gainers(limit, limit)
    losers = self.safe_top_losers(limit, limit)
    most_active = self.safe_market_pulse_active(limit, limit)
    self.market_pulse_cache = {
        "gainers": self._market_pulse_entries(gainers),
        "losers": self._market_pulse_entries(losers),
        "most_active": self._market_pulse_entries(most_active),
    }
    # Feeds a direct priority_score bonus (see
    # TradingStrategy.most_active_priority_bonus) - distinct from
    # agent_popular_symbols below, which just marks a symbol eligible
    # for the POPULAR bucket without weighting most-active names any
    # higher than a gainer/loser inside it.
    self.strategy.most_active_symbols = {
        str(symbol).upper() for symbol in most_active
    }
