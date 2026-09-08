import logging
from concurrent.futures import ThreadPoolExecutor

from webull_bot.webull_api import MarketDataPermissionError, WebullAPI

log = logging.getLogger("webull-bot")


def _fetch_stock_scan_quotes(self, batch: list[str]) -> dict | None:
    """Fetches quotes for this cycle's scan batch, chunked concurrently
    per-category and merged back into one quote_by_symbol dict - the
    quote-fetch sub-phase of _prepare_stock_scan_batch, split out to
    keep that function's length down. Returns None on total failure
    (every chunk failed) - callers should treat that exactly like
    _prepare_stock_scan_batch's own "give up this cycle" None return.
    A MarketDataPermissionError still propagates out of this function
    exactly as it did inline before this extraction - a systemic,
    account-level problem, not a per-chunk one.
    """
    quotes: list[dict] = []
    invalid: set[str] = set()
    grouped: dict[str, list[str]] = {"US_STOCK": [], "US_ETF": []}
    for symbol in batch:
        grouped[self.stock_categories.get(symbol, "US_STOCK")].append(symbol)
    # By request: "scan through all [the universe]... split it up
    # in parallel streams" - a large batch (now up to
    # concurrent_batches * STOCK_SNAPSHOT_MAX_SYMBOLS symbols, see
    # above) is chunked back down to Webull's own per-call cap and
    # every chunk's quote fetch fires CONCURRENTLY, instead of one
    # chunk waiting out the previous chunk's full round-trip first.
    # A single chunk's own failure only drops that chunk's symbols
    # (same "one group's failure shouldn't cost every other
    # group's data" convention _batched_quotes already uses) -
    # except MarketDataPermissionError, which is a systemic
    # account-level problem, not a per-chunk one, and must still
    # propagate/stop the bot exactly like before this change.
    chunks: list[tuple[str, list[str]]] = []
    for category, category_symbols in grouped.items():
        for start in range(0, len(category_symbols), WebullAPI.STOCK_SNAPSHOT_MAX_SYMBOLS):
            chunk_symbols = category_symbols[
                start : start + WebullAPI.STOCK_SNAPSHOT_MAX_SYMBOLS
            ]
            if chunk_symbols:
                chunks.append((category, chunk_symbols))
    chunk_results: list[tuple[list[dict], set[str]] | None] = [None] * len(chunks)
    chunk_errors: list[Exception | None] = [None] * len(chunks)

    def _fetch_chunk(index: int) -> None:
        category, chunk_symbols = chunks[index]
        try:
            chunk_results[index] = self.api.stock_quotes_resilient(
                chunk_symbols, category
            )
        except Exception as exc:
            chunk_errors[index] = exc

    if chunks:
        with ThreadPoolExecutor(
            max_workers=min(len(chunks), self.config.stock_scan_max_concurrent_batches)
        ) as pool:
            list(pool.map(_fetch_chunk, range(len(chunks))))
    permission_error = next(
        (exc for exc in chunk_errors if isinstance(exc, MarketDataPermissionError)),
        None,
    )
    if permission_error is not None:
        raise permission_error
    if chunks and all(err is not None for err in chunk_errors):
        # Every single chunk failed (not just one) - same "give up
        # this cycle" behavior the old single-call version had on
        # any failure, since there's no usable data at all.
        log.error(
            "STOCKS | quote batch failed | %s", chunk_errors[0]
        )
        return None
    for index, (category, _chunk_symbols) in enumerate(chunks):
        if chunk_errors[index] is not None:
            log.warning(
                "STOCKS | quote chunk failed | %s | %s",
                category,
                chunk_errors[index],
            )
            continue
        category_quotes, category_invalid = chunk_results[index]
        quotes.extend(category_quotes)
        if category_invalid:
            if self.config.exclude_etfs and category == "US_STOCK":
                invalid.update(category_invalid)
                continue
            alternate = "US_ETF" if category == "US_STOCK" else "US_STOCK"
            try:
                alternate_quotes, alternate_invalid = (
                    self.api.stock_quotes_resilient(
                        sorted(category_invalid),
                        alternate,
                    )
                )
            except Exception as exc:
                if isinstance(exc, MarketDataPermissionError):
                    raise
                log.warning(
                    "STOCKS | alternate-category quote fetch failed | %s | %s",
                    alternate,
                    exc,
                )
                invalid.update(category_invalid)
                continue
            quotes.extend(alternate_quotes)
            corrected = category_invalid - alternate_invalid
            for symbol in corrected:
                self.stock_categories[symbol] = alternate
            invalid.update(alternate_invalid)
    if invalid:
        self.invalid_stock_symbols.update(invalid)
        self.invalid_symbols.add(invalid)
        self.stock_symbols = [
            symbol for symbol in self.stock_symbols if symbol not in invalid
        ]
        replacements = self.backfill_stock_symbols(len(invalid))
        self.stock_cursor %= max(1, len(self.stock_symbols))
        log.warning(
            "SKIP   | invalid=%s | %s | backfilled=%s",
            len(invalid),
            ",".join(sorted(invalid)),
            replacements,
        )
    return {str(quote.get("symbol", "")).upper(): quote for quote in quotes}
