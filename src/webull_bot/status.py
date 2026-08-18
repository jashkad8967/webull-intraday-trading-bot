import json
import logging
import time
from collections import deque
from decimal import Decimal
from pathlib import Path

log = logging.getLogger("webull-bot")


class StatusWriter:
    """Writes a small JSON snapshot the dashboard UI polls; read-only for trading."""

    def __init__(
        self,
        path: str,
        trade_history: int = 50,
        state_file: str | None = None,
        balance_history_length: int = 1440,
    ):
        self.path = Path(path)
        self.trade_history = trade_history
        self.state_path = Path(state_file) if state_file else None
        loaded_trades, loaded_balance_history = self._load_state()
        self.trades: deque = deque(loaded_trades, maxlen=trade_history)
        self.balance_history: deque = deque(
            loaded_balance_history, maxlen=balance_history_length
        )

    def _load_state(self) -> tuple[list[dict], list[dict]]:
        if self.state_path is None:
            return [], []
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return [], []
        except Exception as exc:
            log.warning("STATUS | state read failed | %s", exc)
            return [], []
        if isinstance(payload, list):
            # Pre-existing state file from before balance history existed -
            # just a plain list of trades.
            return payload, []
        if isinstance(payload, dict):
            trades = payload.get("trades", [])
            balance_history = payload.get("balance_history", [])
            return (
                trades if isinstance(trades, list) else [],
                balance_history if isinstance(balance_history, list) else [],
            )
        return [], []

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "trades": list(self.trades),
                    "balance_history": list(self.balance_history),
                }
            ),
            encoding="utf-8",
        )
        temporary.replace(self.state_path)

    def record_trade(
        self,
        instrument_type: str,
        symbol: str,
        action: str,
        limit_price: Decimal | None,
        order_id: str,
        pnl: Decimal | None = None,
        entry_price: Decimal | None = None,
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
                "entry_price": str(entry_price) if entry_price is not None else None,
            }
        )
        self._save_state()

    def discard_trade(self, order_id: str) -> None:
        """Removes a trade-log entry that record_trade wrote optimistically
        at order submission time, once AutoTrader.reverse_phantom_exit has
        confirmed the order never actually filled. Without this, a
        cancelled order stays on the dashboard's Recent Trades list
        forever, labeled as a completed profit that never happened.
        """
        before = len(self.trades)
        self.trades = deque(
            (trade for trade in self.trades if trade.get("order_id") != order_id),
            maxlen=self.trade_history,
        )
        if len(self.trades) != before:
            self._save_state()

    def record_balance(self, balance: Decimal) -> None:
        """Appends one point to the account-equity history the dashboard
        charts - see AutoTrader.write_status_snapshot for the throttling
        (this is called far less often than every status write; a point
        every few seconds is already more than a chart needs).
        """
        self.balance_history.append({"time": time.time(), "balance": str(balance)})
        self._save_state()

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
            "balance_history": list(self.balance_history),
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
