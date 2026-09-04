def agent_assessment(self, symbol: str) -> dict | None:
    """The agent no longer scores individual symbols (see
    MarketResearchAgent - it now reviews account-wide performance,
    not per-symbol setups). Kept as an always-None stub so
    prioritized_stock_batch/stock_decision's existing "no
    assessment" handling doesn't need to change.
    """
    return None
