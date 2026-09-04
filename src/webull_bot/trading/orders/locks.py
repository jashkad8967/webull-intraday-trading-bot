from contextlib import nullcontext


def _working_orders_lock(bot) -> object:
    """getattr fallback (module-level, not a self-method - see below)
    so working_orders touches can be locked without breaking the many
    existing unit tests that bind an AutoTrader method directly onto a
    bare SimpleNamespace fixture (AutoTrader.foo.__get__(fake_bot)).
    A self-method here would itself need to be looked up as
    self._working_orders_lock, which fails on a fixture that never
    bound it - a plain module-level function taking bot as an argument
    has no such requirement. Falls back to a no-op context manager
    when bot has no working_orders_lock attribute at all (every
    existing test fixture), so those tests run unchanged, single-
    threaded, with no behavior change - only the real AutoTrader
    (which sets a real threading.Lock in __init__) actually
    serializes against the position-protection thread (see
    AutoTrader._position_protection_loop).
    """
    lock = getattr(bot, "working_orders_lock", None)
    return lock if lock is not None else nullcontext()


def _rekey_working_order(bot, old_order_id: str, new_order_id: str, entry: dict) -> None:
    """Swaps a cancel-and-replace repricer's working_orders entry
    atomically under the lock - every repricer (reprice_resting_
    exits/entries, reprice_volatility_scalp_exits/entries) does this
    exact pop-old/set-new pair, and each one needs it locked now that
    the position-protection thread runs concurrently with record_trade
    on the main thread. Module-level for the same test-fixture-
    compatibility reason as _working_orders_lock above.
    """
    with _working_orders_lock(bot):
        bot.working_orders.pop(old_order_id, None)
        bot.working_orders[new_order_id] = entry
