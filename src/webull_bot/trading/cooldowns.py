import time

from webull_bot.trading.locks import _working_orders_lock


def cooldown_ready(self, key: str) -> bool:
    elapsed = time.monotonic() - self.last_trade.get(key, float("-inf"))
    return elapsed >= float(self.config.trade_cooldown_seconds)


def reentry_cooldown_ready(self, key: str) -> bool:
    elapsed = time.monotonic() - self.last_exit_at.get(key, float("-inf"))
    return elapsed >= float(self.config.stock_reentry_cooldown_seconds)


def rate_capped(self, key: str) -> bool:
    limit = self.config.stock_max_trades_per_hour
    if limit <= 0:
        return False
    now = time.monotonic()
    times = self.trade_times[key]
    while times and now - times[0] > 3600.0:
        times.popleft()
    return len(times) >= limit


def has_pending_buy_order(self, key: str) -> bool:
    """True while an uncancelled BUY order for this key is still
    resting in self.working_orders - independent of the account's
    own (up to ACCOUNT_REFRESH_SECONDS-stale) position snapshot,
    which still reads "flat" (quantity 0) the entire time a BUY
    order hasn't filled yet. Live incident: without this, the
    volatility-scalp fresh-entry gate's only guard against
    double-buying (self.volatility_scalp_positions) was being wiped
    every single cycle by the quantity == 0 cleanup while a resting
    order was still live, stacking repeated duplicate BUY orders
    for the same symbol (MTNB: 5 orders in ~70s, same price, no
    fill or cancel in between) with no cooldown left to stop it.
    """
    with _working_orders_lock(self):
        orders = list(self.working_orders.values())
    return any(
        order.get("key") == key
        and order.get("action") == "BUY"
        and order.get("cancel_requested_at") is None
        for order in orders
    )
