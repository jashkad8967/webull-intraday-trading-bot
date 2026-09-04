import logging
from collections import defaultdict

from webull_bot.webull_api import WebullAPI

log = logging.getLogger("webull-bot")


def _batched_quotes(self, symbols: list[str]) -> dict[str, dict]:
    """One (or a few, split by category) batched snapshot call for
    multiple symbols, instead of a caller looping and calling
    self.api.stock_quote(symbol) once per symbol. Each individual
    call is a full separate network round-trip - with several
    repricers/sweeps each doing this once per open position or
    working order, every single main-loop cycle, this was a real
    contributor to cycles taking far longer than poll_seconds (live
    evidence: ~40s between scan cycles despite a 0.25s poll target).

    Grouped by category (stock_quotes requires one category per
    call) and capped at WebullAPI.STOCK_SNAPSHOT_MAX_SYMBOLS per
    group. A group's failure only drops that group's symbols from
    the result - callers already treat a missing symbol as "no
    quote yet, try again next cycle," the same as any other quote
    failure.
    """
    unique_symbols = list(dict.fromkeys(symbols))
    if not unique_symbols:
        return {}
    by_category: dict[str, list[str]] = defaultdict(list)
    for symbol in unique_symbols:
        by_category[self.stock_categories.get(symbol, "US_STOCK")].append(symbol)
    quotes: dict[str, dict] = {}
    for category, group in by_category.items():
        for start in range(0, len(group), WebullAPI.STOCK_SNAPSHOT_MAX_SYMBOLS):
            chunk = group[start : start + WebullAPI.STOCK_SNAPSHOT_MAX_SYMBOLS]
            try:
                fetched, _invalid = self.api.stock_quotes_resilient(
                    chunk, category
                )
            except Exception as exc:
                log.warning(
                    "REPRICE| batched quote fetch failed | %s | %s",
                    ",".join(chunk),
                    exc,
                )
                continue
            for quote in fetched:
                symbol = str(quote.get("symbol", "")).upper()
                if symbol:
                    quotes[symbol] = quote
    return quotes
