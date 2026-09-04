import time


def _manual_touch_active(bot, symbol: str) -> bool:
    """By request: "when i touch a stock stop doing anything with it
    while i am there." Module-level, not a self-method, for the same
    test-fixture-compatibility reason as _working_orders_lock above -
    every repricer/escalation call site calls this on `bot`, which in
    many existing tests is a bare SimpleNamespace with only one method
    bound via .__get__, not a real AutoTrader. getattr defaults to "no
    touch recorded" (False) when the fixture never set manual_touch_at
    at all, so every existing test keeps its original behavior
    unchanged - only a real AutoTrader (which does set manual_touch_at
    in __init__ and stamps it in record_trade) actually pauses.
    """
    touched_at = getattr(bot, "manual_touch_at", {}).get(symbol)
    if touched_at is None:
        return False
    pause_seconds = float(
        getattr(bot.config, "manual_touch_pause_seconds", 300)
    )
    return time.monotonic() - touched_at < pause_seconds
