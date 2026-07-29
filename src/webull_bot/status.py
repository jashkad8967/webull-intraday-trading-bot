import json
import time
from collections import deque
from decimal import Decimal
from pathlib import Path


class StatusWriter:
    """Writes a small JSON snapshot the dashboard UI polls; read-only for trading."""

    def __init__(self, path: str, trade_history: int = 50):
        self.path = Path(path)
        self.trades: deque = deque(maxlen=trade_history)

    def record_trade(
        self,
        instrument_type: str,
        symbol: str,
        action: str,
        limit_price: Decimal | None,
        order_id: str,
        pnl: Decimal | None = None,
    ) -> None:
        self.trades.appendleft(
            {
                "time": time.time(),
                "instrument_type": instrument_type,
                "symbol": symbol,
                "action": action,
                "limit_price": str(limit_price) if limit_price is not None else None,
                "order_id": order_id,
                "pnl": str(pnl) if pnl is not None else None,
            }
        )

    def write(
        self,
        *,
        mode: str,
        buying_power: Decimal,
        positions: list[dict],
        watchlist: list[dict],
        agent_summary: dict | None,
        paused: bool,
        stock_count: int,
        option_count: int,
        realized_pnl_today: Decimal = Decimal("0"),
        unrealized_pnl_total: Decimal = Decimal("0"),
        user_watchlist: list[str] | None = None,
        pending_orders: list[dict] | None = None,
    ) -> None:
        payload = {
            "updated_at": time.time(),
            "mode": mode,
            "paused": paused,
            "buying_power": str(buying_power),
            "positions": positions,
            "watchlist": watchlist,
            "user_watchlist": user_watchlist or [],
            "agent": agent_summary,
            "universe": {"stocks": stock_count, "options": option_count},
            "recent_trades": list(self.trades),
            "pending_orders": pending_orders or [],
            "pnl_today": {
                "realized": str(realized_pnl_today),
                "unrealized": str(unrealized_pnl_total),
                "total": str(realized_pnl_today + unrealized_pnl_total),
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, default=str), encoding="utf-8")
        temporary.replace(self.path)
