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
        quantity: Decimal | None = None,
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
                "quantity": str(quantity) if quantity is not None else None,
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

    def rekey_trade(self, old_order_id: str, new_order_id: str) -> None:
        """Repoints a trade-log entry's order_id after AutoTrader.
        reprice_resting_exits cancels the resting order and places a new
        one for the same logical exit, without calling record_trade again
        (see that function's docstring for why - it would double-count
        the pnl). Without this, discard_trade later looks for new_order_id
        (the only id reverse_phantom_exit ever sees, since old_order_id
        was never passed anywhere past this point) and finds nothing to
        remove, since the visible entry is still filed under old_order_id
        - leaving a cancelled order's phantom profit on the dashboard
        forever, same symptom as a missing discard_trade call entirely.
        """
        for trade in self.trades:
            if trade.get("order_id") == old_order_id:
                trade["order_id"] = new_order_id
                self._save_state()
                return

    def record_balance(self, balance: Decimal) -> None:
        """Appends one point to the account-equity history the dashboard
        charts - see AutoTrader.write_status_snapshot for the throttling
        (this is called far less often than every status write; a point
        every few seconds is already more than a chart needs).
        """
        self.balance_history.append({"time": time.time(), "balance": str(balance)})
        self._save_state()

    @staticmethod
    def pnl_today_payload(
        realized_pnl_today: Decimal,
        open_pnl_total: Decimal,
        account_day_pnl_total: Decimal | None,
    ) -> dict:
        """open: today's mark-to-market move on currently-held positions
        (Webull's own day_profit_loss per position, net of fee - see
        TradingStrategy.position_day_pnl), already largely Webull-sourced.

        total: Webull's own account-level total_day_profit_loss when
        available (see WebullAPI.account_day_pnl_from_balance) - ground
        truth for the account's actual today P&L, not a locally-summed
        estimate.

        realized: backed out as total - open when total is available, so
        the three figures are always internally consistent (realized +
        open == total, exactly). Live incident: showing the bot's own
        separately-tracked realized_pnl_today here (only ever an at-
        submission-time estimate - see record_realized_exit's own
        docstring) alongside a Webull-sourced total produced a headline
        total that visibly didn't match its own breakdown, on top of the
        drift the total-sourcing fix already addressed. Falls back to
        the bot's local realized_pnl_today/sum only when Webull doesn't
        report a total (e.g. paper mode).
        """
        if account_day_pnl_total is not None:
            total = account_day_pnl_total
            realized = total - open_pnl_total
        else:
            realized = realized_pnl_today
            total = realized_pnl_today + open_pnl_total
        return {
            "realized": str(realized),
            "open": str(open_pnl_total),
            "total": str(total),
        }

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
        open_pnl_total: Decimal = Decimal("0"),
        account_day_pnl_total: Decimal | None = None,
        account_value: Decimal | None = None,
        user_watchlist: list[str] | None = None,
        pending_orders: list[dict] | None = None,
    ) -> None:
        pending_orders = pending_orders or []
        # By request: "no pending order should go into the recent
        # trades." record_trade writes to self.trades optimistically at
        # ORDER SUBMISSION time (before it's actually filled) - see
        # discard_trade's docstring, which already reverses this for a
        # CONFIRMED-never-filled order, but a still-resting order (not
        # yet filled, not yet cancelled either) previously showed up in
        # both the pending-orders and recent-trades lists at once. This
        # doesn't change how/when self.trades itself gets written
        # (still optimistic, for the exact reasons discard_trade already
        # documents), just filters the DISPLAYED recent-trades list
        # against whatever's currently still pending.
        pending_ids = {order.get("order_id") for order in pending_orders}
        recent_trades = [
            trade for trade in self.trades if trade.get("order_id") not in pending_ids
        ]
        payload = {
            "updated_at": time.time(),
            "mode": mode,
            "paused": paused,
            "buying_power": str(buying_power),
            # Total net liquidation value (cash + market value of every
            # held position) - the account's actual full worth, distinct
            # from buying_power (spendable cash only). None (and shown
            # as "—") when Webull doesn't report it (e.g. paper mode).
            "account_value": str(account_value) if account_value is not None else None,
            "positions": positions,
            "watchlist": watchlist,
            "user_watchlist": user_watchlist or [],
            "agent": agent_summary,
            "universe": {"stocks": stock_count, "options": option_count},
            "recent_trades": recent_trades,
            "pending_orders": pending_orders,
            "balance_history": list(self.balance_history),
            "pnl_today": self.pnl_today_payload(
                realized_pnl_today, open_pnl_total, account_day_pnl_total
            ),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, default=str), encoding="utf-8")
        temporary.replace(self.path)
