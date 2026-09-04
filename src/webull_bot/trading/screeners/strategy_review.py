from decimal import Decimal

from webull_bot.status import StatusWriter


def submit_strategy_review(
    self,
    positions: list[dict],
    buying_power: Decimal,
    force: bool = False,
    event: str = "ROUTINE_REVIEW",
) -> None:
    """Sends the agent a compact snapshot of real account performance
    (holdings, today's pnl, recent trades) - not per-symbol setups -
    and lets it assess whether the CURRENT strategy is working. See
    MarketResearchAgent.submit_strategy_review's docstring: this is
    review-gated, the result is only ever a logged/dashboard
    suggestion, never applied automatically.
    """
    if not self.market_agent or not self.config.strategy_review_enabled:
        return
    held = [
        {
            "symbol": str(item.get("symbol", "")).upper(),
            "type": item.get("instrument_type"),
            "qty": self._compact_number(item.get("quantity")),
            "cost": self._compact_number(item.get("cost_price"), 2),
            "unrealized_pnl": self._compact_number(
                self.strategy.position_unrealized_pnl(item), 2
            ),
            "day_pnl": self._compact_number(
                self.strategy.position_day_pnl(item), 2
            ),
        }
        for item in positions
        if Decimal(str(item.get("quantity", "0"))) != 0
    ]
    recent_trades = [
        {
            "symbol": trade.get("symbol"),
            "action": trade.get("action"),
            "entry": trade.get("entry_price"),
            "exit": trade.get("limit_price"),
            "qty": trade.get("quantity"),
            "pnl": trade.get("pnl"),
        }
        for trade in list(self.status.trades)[
            : self.config.strategy_review_trade_history_limit
        ]
    ]
    self.market_agent.submit_strategy_review(
        {
            "event": event,
            "buying_power": self._compact_number(buying_power, 0),
            "holdings": held,
            # Same reconciliation StatusWriter's own dashboard total
            # uses - Webull's own account_day_pnl when available
            # (ground truth), not just the bot's local estimate. The
            # agent should review real performance, not the same
            # drifting number this session's earlier fixes addressed.
            "pnl_today": StatusWriter.pnl_today_payload(
                self.daily_realized_pnl,
                sum(
                    (Decimal(str(item["day_pnl"])) for item in held),
                    Decimal("0"),
                ),
                self.cached_account_day_pnl,
            ),
            "recent_trades": recent_trades,
        },
        force=force,
    )
