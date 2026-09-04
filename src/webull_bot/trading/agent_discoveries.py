def refresh_agent_discoveries(self) -> None:
    """Sourced from the deterministic market_pulse screener data, not
    the research agent - this keeps working (and keeps priority
    scanning pointed at today's actual movers) even if AGENT_ENABLED
    is false or a Groq request fails.
    """
    self.refresh_market_pulse()
    available = set(self.stock_symbols)
    pulse_symbols = {
        entry["symbol"]
        for bucket in self.market_pulse_cache.values()
        for entry in bucket
    }
    self.agent_popular_symbols = {
        symbol for symbol in pulse_symbols if symbol in available
    }
