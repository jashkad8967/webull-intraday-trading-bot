import logging
from collections import defaultdict
from decimal import Decimal

log = logging.getLogger("webull-bot")


def seed_volatility_windows(self, symbols: list[str]) -> None:
    """Warm-starts each not-yet-seen symbol's volatility-scalp window
    from real M1 bar closes in one batched call per category, instead
    of leaving it to build up one live snapshot poll at a time. Fully
    self-limiting: TradingStrategy.seed_volatility_window is a no-op
    for any symbol whose window already has data (from a prior seed
    or from live polling), so the candidate list naturally shrinks to
    nothing as the watchlist gets covered.
    """
    unseeded = [
        symbol
        for symbol in symbols
        if not self.strategy.volatility_price_history.get(symbol)
    ]
    if not unseeded:
        return
    grouped: dict[str, list[str]] = defaultdict(list)
    for symbol in unseeded:
        grouped[self.stock_categories.get(symbol, "US_STOCK")].append(symbol)
    for category, category_symbols in grouped.items():
        try:
            closes_by_symbol = self.api.recent_minute_closes(
                category_symbols,
                category,
                self.config.volatility_scalp_lookback_samples,
            )
        except Exception as exc:
            log.warning("SCALP  | bar seed fetch failed | %s | %s", category, exc)
            continue
        for symbol, closes in closes_by_symbol.items():
            self.strategy.seed_volatility_window(symbol, closes)
            # select_volatility_scalp_symbols candidates come from
            # self.strategy.prices, which otherwise only gets
            # populated by a live quote scan (update_stock_snapshot)
            # - without this, bar-seeding the volatility window alone
            # still wouldn't make a symbol visible to cohort
            # selection until it was actually scanned. Never
            # overwrites an already-live price with a stale bar
            # close.
            if closes and symbol not in self.strategy.prices:
                self.strategy.prices[symbol] = Decimal(str(closes[-1]))
