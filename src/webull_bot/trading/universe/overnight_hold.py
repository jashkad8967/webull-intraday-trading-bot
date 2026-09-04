# Symbols carry a position past EOD_CLOSE_TIME instead of always
# flattening, unless their bucket is in ALWAYS_FLATTEN_BUCKETS (pairs
# legs - that strategy is intraday-only by design).
OVERNIGHT_HOLD_ENABLED = True
ALWAYS_FLATTEN_BUCKETS = frozenset({"PAIRS_LONG", "PAIRS_SHORT"})


def overnight_hold_symbols(self) -> set[str]:
    """Symbols whose bucket is eligible to carry a position past
    EOD_CLOSE_TIME instead of always flattening. Pairs positions are
    excluded - that strategy is intraday-only by design - so only the
    core EMA/OBI stock strategy's own positions (plus manual buys)
    ever ride overnight.
    """
    if not OVERNIGHT_HOLD_ENABLED:
        return set()
    return {
        symbol
        for symbol, bucket in self.position_buckets.items()
        if bucket not in ALWAYS_FLATTEN_BUCKETS
        and symbol not in self.short_symbols
    }
