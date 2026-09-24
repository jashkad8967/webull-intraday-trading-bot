import time

from webull_bot.trading.orders.locks import _working_orders_lock


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


def has_pending_sell_order(self, key: str) -> bool:
    """SELL analog of has_pending_buy_order. Live incident: VZ's
    stall-position-boost sweep (boost_stalled_positions) placed its
    own SELL_TO_CLOSE for a symbol that already had a resting
    STOP/PROFIT exit order out from the normal exit path - Webull's
    broker-side reservation against the held quantity meant the
    second close attempt would have tried to close more than the
    unreserved balance, rejected outright as reversing the position
    (OPENAPI_ORDER_NOT_SUPPORT_REVERSE_OPTION) or as a direction
    mismatch (OPENAPI_POSITION_ORDER_INTENT_MISMATCH). pending_stock_
    exits/pending_option_exits already guard against this in the
    common case, but only while this process's own in-memory flag is
    set - this checks the actual resting order state directly, the
    same way has_pending_buy_order already does for entries.
    """
    with _working_orders_lock(self):
        orders = list(self.working_orders.values())
    return any(
        order.get("key") == key
        # MANUAL_SELL belongs here too. A sell the USER placed from
        # the dashboard reserves the broker-side quantity exactly like
        # an automated one, but was omitted - so every automated exit
        # path believed nothing was resting and kept submitting into a
        # position already spoken for, logging HTTP 417
        # OPENAPI_OPTION_LONG_POSITION_MUST_BE_CLOSE_THAN_SELL_SHORT
        # on every cycle (live 2026-09-24).
        and order.get("action") in ("SELL", "STOP", "PROFIT", "MANUAL_SELL")
        and order.get("cancel_requested_at") is None
        for order in orders
    )
