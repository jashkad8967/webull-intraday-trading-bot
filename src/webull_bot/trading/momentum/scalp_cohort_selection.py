import logging
import time
from decimal import Decimal

log = logging.getLogger("webull-bot")


def select_volatility_scalp_symbols(self) -> None:
    """Re-ranks the curated volatility-scalp cohort from data already
    being collected during normal scanning (self.strategy.prices/
    volatility_price_history) - no extra API calls needed. Picks the
    top VOLATILITY_SCALP_SYMBOL_COUNT symbols, by realized short-
    window volatility, among those priced at or under
    VOLATILITY_SCALP_MAX_PRICE with enough samples to have a real
    reading. Re-run periodically (VOLATILITY_SCALP_RESELECT_SECONDS),
    so a symbol that's cooled off drops out and a newly-hot one
    (from anywhere in the scanned universe, not just today's
    starting picks) can take its place - "keep looking for volatile
    stocks to add to the group."
    """
    if not self.config.volatility_scalp_enabled:
        return
    now = time.monotonic()
    if (
        now - self.last_volatility_symbol_selection
        < float(self.config.volatility_scalp_reselect_seconds)
    ):
        return
    candidates: list[tuple[Decimal, str]] = []
    for symbol, price in self.strategy.prices.items():
        if price <= 0 or price > self.config.volatility_scalp_max_price:
            continue
        stdev = self.strategy.realized_volatility_percent(symbol)
        if stdev is None:
            continue
        candidates.append((stdev, symbol))
    if not candidates:
        # Don't stamp the throttle yet - this call ran before any
        # symbol had accumulated a real volatility reading (always
        # true for the very first call or two right after startup,
        # since self.strategy.prices is still empty then). Stamping
        # here anyway would "spend" the throttle on a result with no
        # real data behind it and leave the cohort empty for the
        # full VOLATILITY_SCALP_RESELECT_SECONDS (default 30 min)
        # before ever trying again.
        return
    self.last_volatility_symbol_selection = now
    candidates.sort(key=lambda item: item[0], reverse=True)
    selected = {
        symbol
        for _, symbol in candidates[: self.config.volatility_scalp_symbol_count]
    }
    if selected != self.volatility_scalp_symbols:
        log.info(
            "SCALP  | daily cohort | %s",
            ", ".join(sorted(selected)) if selected else "(none eligible yet)",
        )
    self.volatility_scalp_symbols = selected
