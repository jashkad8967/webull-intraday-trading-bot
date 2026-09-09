import logging


def stock_categories(self, symbols: list[str]) -> dict[str, str]:
    from webull.data.common.category import Category

    requested = list(dict.fromkeys(symbol.upper() for symbol in symbols))
    categories: dict[str, str] = {}
    for category in (Category.US_STOCK.name, Category.US_ETF.name):
        for start in range(0, len(requested), 100):
            batch = requested[start : start + 100]
            page = self._stock_instruments_resilient(batch, category)
            for item in page or []:
                symbol = str(item.get("symbol", "")).upper()
                if (
                    symbol in batch
                    and item.get("tradable_status", "OC") == "OC"
                ):
                    categories[symbol] = category
    return categories


def _stock_instruments_resilient(
    self,
    symbols: list[str],
    category: str,
) -> list[dict]:
    if not symbols:
        return []
    try:
        return self._call(
            lambda: self.data.instrument.get_instrument(
                symbols=symbols,
                category=category,
                page_size=len(symbols),
            ),
            "stock_instrument",
        )
    except Exception as exc:
        message = str(exc)
        if (
            "INVALID_SYMBOL" not in message
            and "does not exist in the category" not in message
        ):
            raise
        invalid = self._invalid_symbols(message, symbols)
        if invalid:
            return self._stock_instruments_resilient(
                [symbol for symbol in symbols if symbol not in invalid],
                category,
            )
        if len(symbols) == 1:
            return []
        middle = len(symbols) // 2
        return (
            self._stock_instruments_resilient(symbols[:middle], category)
            + self._stock_instruments_resilient(symbols[middle:], category)
        )


def stock_universe(
    self,
    progress=None,
    limit: int | None = None,
) -> dict[str, str]:
    from webull.data.common.category import Category

    categories: dict[str, str] = {}
    limit = self.config.stock_universe_limit() if limit is None else limit
    cursor = None
    safe_page_size = self.config.stock_universe_page_size
    while limit == 0 or len(categories) < limit:
        page_size = (
            safe_page_size
            if limit == 0
            else min(safe_page_size, limit - len(categories))
        )
        try:
            page = self._call(
                lambda cursor=cursor, page_size=page_size: (
                    self.data.instrument.get_instrument(
                        category=Category.US_STOCK.name,
                        last_instrument_id=cursor,
                        page_size=page_size,
                    )
                ),
                "stock_instrument",
            )
        except Exception as exc:
            if self._payload_too_large(str(exc)) and page_size > 25:
                safe_page_size = max(25, page_size // 2)
                logging.getLogger("webull-bot").warning(
                    "LOAD   | payload too large | reducing directory page=%s",
                    safe_page_size,
                )
                continue
            raise
        if not page:
            break
        for item in page:
            symbol = str(item.get("symbol", "")).upper()
            if symbol and item.get("tradable_status", "OC") == "OC":
                categories[symbol] = Category.US_STOCK.name
                if limit and len(categories) >= limit:
                    break
        if progress:
            progress("US_LISTED", len(categories), limit)
        next_cursor = page[-1].get("instrument_id")
        if (
            (limit and len(categories) >= limit)
            or len(page) < page_size
            or not next_cursor
            or str(next_cursor) == cursor
        ):
            break
        cursor = str(next_cursor)
    return categories
