import time
from decimal import Decimal

from webull_bot.trading.orders.locks import _working_orders_lock


def write_status_snapshot(
    self,
    positions: list[dict],
    buying_power: Decimal,
    paused: bool,
) -> None:
    if time.monotonic() - self.last_status_write < float(self.config.poll_seconds):
        return
    self.last_status_write = time.monotonic()
    position_rows = []
    for position in positions:
        quantity = Decimal(str(position.get("quantity", "0")))
        if quantity == 0:
            continue
        symbol = str(position.get("symbol", "")).upper()
        # Live incident (this bug, caught from a real UI report): the
        # displayed last_price used to come from self.strategy.prices
        # (the bot's own scan-cycle quote cache), while the P&L
        # figures shown right next to it (position_unrealized_pnl/
        # position_day_pnl) prefer the BROKER's own reported
        # last_price/market_price field on the position itself when
        # present - two independently-refreshed sources on different
        # cadences, so the displayed price and the P&L shown beside
        # it could silently disagree (a live example: KNRX showed
        # last_price=0.392, but that price doesn't reconcile with
        # the unrealized_pnl shown alongside it). Prefer the SAME
        # broker-native fields the P&L math already uses, falling
        # back to strategy.prices only when the broker hasn't
        # reported one - makes price and P&L internally consistent.
        last_price = (
            position.get("last_price")
            or position.get("market_price")
            or self.strategy.prices.get(symbol)
            or position.get("cost_price", "0")
        )
        position_rows.append(
            {
                "symbol": symbol,
                "instrument_type": position.get("instrument_type"),
                "quantity": str(quantity),
                "cost_price": str(position.get("cost_price", "0")),
                "last_price": str(last_price),
                "unrealized_pnl": str(self.strategy.position_unrealized_pnl(position)),
                "day_pnl": str(self.strategy.position_day_pnl(position)),
                "bucket": self.position_buckets.get(symbol, "DISCOVERY"),
            }
        )
    held_symbols = {row["symbol"] for row in position_rows}
    watchlist_rows = [
        {
            "symbol": symbol,
            "price": str(self.strategy.prices.get(symbol, "0")),
            "bucket": self.strategy.selection_bucket(symbol),
            "has_position": symbol in held_symbols,
            **self.strategy.metrics.get(symbol, {}),
        }
        for symbol in sorted(self.user_watchlist)
    ]
    agent_summary = None
    if self.market_agent:
        agent_summary = {
            "enabled": True,
            "market_pulse": self.market_pulse_cache,
            "popular_symbols": sorted(self.agent_popular_symbols),
            "strategy_review": self.market_agent.strategy_review(),
        }
    day_pnl_total = sum(
        (Decimal(row["day_pnl"]) for row in position_rows),
        Decimal("0"),
    )
    now = time.monotonic()
    if now - self.last_balance_history_write >= 20:
        self.last_balance_history_write = now
        # Signed quantity (negative for shorts) makes this the same
        # cash + market-value equity formula for both directions - a
        # short's proceeds already sit in buying_power, and its
        # negative position value nets out the buy-back liability.
        # cached_raw_buying_power (not the buying_power parameter,
        # which is reserved-down for trading sizing) so this doesn't
        # understate real equity by MIN_CASH_RESERVE_DOLLARS.
        total_equity = self.cached_raw_buying_power + sum(
            (
                Decimal(row["quantity"]) * Decimal(row["last_price"])
                for row in position_rows
            ),
            Decimal("0"),
        )
        self.status.record_balance(total_equity)
    with _working_orders_lock(self):
        working_orders_snapshot = list(self.working_orders.items())
    pending_order_rows = [
        {
            "order_id": order_id,
            "instrument_type": order.get("key", "?:?").split(":", 1)[0],
            "symbol": order.get("key", "?:?").split(":", 1)[-1],
            "action": order.get("action"),
            "quantity": (
                str(order["quantity"]) if order.get("quantity") is not None else None
            ),
            "limit_price": (
                str(order["limit_price"])
                if order.get("limit_price") is not None
                else None
            ),
            "age_seconds": round(now - float(order.get("submitted_at", now))),
            "cancel_requested": order.get("cancel_requested_at") is not None,
        }
        for order_id, order in working_orders_snapshot
    ]
    self.status.write(
        mode=self.config.mode,
        # Real, un-reserved buying power (what Webull's own app
        # shows), not the buying_power parameter this function also
        # received - that one is reserved down by
        # MIN_CASH_RESERVE_DOLLARS for trading sizing and showing it
        # here reads as a silent gap against the account's real cash.
        buying_power=self.cached_raw_buying_power,
        positions=position_rows,
        watchlist=watchlist_rows,
        agent_summary=agent_summary,
        paused=paused,
        stock_count=len(self.stock_symbols),
        option_count=len(self.option_contracts),
        realized_pnl_today=self.daily_realized_pnl,
        open_pnl_total=day_pnl_total,
        account_day_pnl_total=self.cached_account_day_pnl,
        account_value=self.cached_account_value,
        user_watchlist=sorted(self.user_watchlist),
        pending_orders=pending_order_rows,
    )
