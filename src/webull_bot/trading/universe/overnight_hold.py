# By request: "before eod sell all and make sure for tuesday there is
# enough option bp, keep it separate, in fact all the bp should be for
# option." Every stock position (not just pairs legs) now always
# flattens at EOD_CLOSE_TIME instead of certain buckets riding
# overnight - closing everything out maximizes the account's free
# equity (and therefore next session's real option buying power,
# which is net equity minus margin held for open positions - see
# account_state) going into the next trading day.
OVERNIGHT_HOLD_ENABLED = False
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
