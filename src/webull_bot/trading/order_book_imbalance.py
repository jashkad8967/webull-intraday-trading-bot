from decimal import Decimal

from webull_bot.strategy import OBI_DEPTH_LEVELS


def _quote_size(quote: dict, *fields: str) -> Decimal | None:
    for field in fields:
        value = quote.get(field)
        if value in (None, ""):
            continue
        try:
            size = Decimal(str(value))
        except Exception:
            continue
        if size.is_finite() and size >= 0:
            return size
    return None


def obi_score_for(self, symbol: str, category: str, quote: dict) -> Decimal | None:
    """Order-book-imbalance score for a symbol that's otherwise about
    to fire a BUY. Only ever called for that one symbol right before
    order placement - fetching L2 depth for every scanned symbol every
    cycle would badly overrun the "market" request-rate budget (a
    single depth call per symbol vs. today's one snapshot call per
    whole batch), so this stays a final, on-demand gate rather than a
    per-cycle metric like everything else in strategy.metrics.
    """
    depth = self.api.stock_depth(symbol, category)
    score = self.api.depth_imbalance(depth, OBI_DEPTH_LEVELS)
    if score is not None:
        return score
    bid_size = self._quote_size(quote, "bid_size", "bidSize", "bid_volume")
    ask_size = self._quote_size(quote, "ask_size", "askSize", "ask_volume")
    if bid_size is None or ask_size is None:
        return None
    total = bid_size + ask_size
    return bid_size / total if total > 0 else None
