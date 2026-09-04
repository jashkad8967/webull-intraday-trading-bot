import logging
from collections import defaultdict
from decimal import Decimal

log = logging.getLogger("webull-bot")


def _stall_equity_quotes(
    self,
    positions: list[dict],
    core_session_active: bool,
    stall_seconds: float,
    now: float,
) -> dict[str, dict]:
    """Batch-fetches quotes for every EQUITY position that clears the
    cheap stall-eligibility checks, instead of boost_stalled_positions
    calling api.stock_quote(symbol) one at a time inside its loop.
    stock_quote() with no category does its OWN per-symbol category
    lookup plus its own single-symbol quote fetch - two API calls
    each - so a held-position count in the teens meant dozens of
    sequential, individually rate-limited round trips blocking this
    entire single-threaded loop for minutes at a stretch, right when
    the per-symbol stall check (see boost_stalled_positions) started
    actually reaching this code instead of bailing out early.
    """
    candidates = []
    for position in positions:
        if position.get("instrument_type") != "EQUITY":
            continue
        quantity = Decimal(str(position.get("quantity", "0")))
        if quantity <= 0:
            continue
        if Decimal(str(position.get("cost_price") or "0")) <= 0:
            continue
        symbol = str(position.get("symbol", "")).upper()
        if symbol in self.pending_stock_exits:
            continue
        key = f"STOCK:{symbol}"
        if not self.cooldown_ready(key):
            continue
        if now - self.last_trade.get(key, 0.0) < stall_seconds:
            continue
        if self.is_fractional_quantity(quantity) and not core_session_active:
            continue
        candidates.append(symbol)
    if not candidates:
        return {}
    by_category: dict[str, list[str]] = defaultdict(list)
    for symbol in candidates:
        by_category[self.stock_categories.get(symbol, "US_STOCK")].append(symbol)
    quote_by_symbol: dict[str, dict] = {}
    for category, symbols in by_category.items():
        try:
            quotes, _ = self.api.stock_quotes_resilient(symbols, category)
        except Exception as exc:
            log.error(
                "STALL  | batch quote fetch failed | %s | %s",
                category,
                exc,
            )
            continue
        for quote in quotes:
            quote_by_symbol[str(quote.get("symbol", "")).upper()] = quote
    return quote_by_symbol
