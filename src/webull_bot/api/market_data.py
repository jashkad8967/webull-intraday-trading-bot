import logging
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal


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


def _screener_number(item: dict, field: str) -> float:
    try:
        value = float(item.get(field, ""))
    except (TypeError, ValueError):
        return 0.0
    return value if value == value and value not in (float("inf"), float("-inf")) else 0.0


def _page_screener(self, fetch_page, total_limit: int, page_size: int) -> dict[str, dict]:
    """Shared pagination/parsing for Webull screener endpoints.

    The SDK doesn't publish a typed response model for these endpoints, so
    the page-of-results and has-more-pages fields are read defensively
    across the field names Webull's own docs/SDK docstrings reference.
    """
    results: dict[str, dict] = {}
    page_index = 1
    while len(results) < total_limit:
        remaining = total_limit - len(results)
        requested_size = min(page_size, remaining)
        response = self._call(
            lambda page_index=page_index, requested_size=requested_size: (
                fetch_page(page_index, requested_size)
            ),
            "market",
        )
        if isinstance(response, list):
            items, has_more = response, len(response) >= requested_size
        elif isinstance(response, dict):
            items = (
                response.get("list")
                or response.get("data")
                or response.get("items")
                or []
            )
            has_more = bool(response.get("has_more", len(items) >= requested_size))
        else:
            items, has_more = [], False
        if not items:
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol", "")).upper()
            if not symbol:
                continue
            results[symbol] = {
                "market_value": self._screener_number(item, "market_value"),
                "volume": self._screener_number(item, "volume"),
                "change_ratio": self._screener_number(item, "change_ratio"),
                "amplitude": self._screener_number(item, "amplitude"),
            }
        if not has_more or len(results) >= total_limit:
            break
        page_index += 1
    return results


def top_active_stocks(
    self,
    total_limit: int,
    page_size: int,
    rank_type: str = "VOLUME",
    sort_by: str = "MARKET_VALUE",
) -> dict[str, dict]:
    """Page through Webull's most-active screener for a large, market-cap-
    tagged stock universe (no ETFs - the screener's US_STOCK category is
    stocks only).
    """
    from webull.data.common.category import Category

    return self._page_screener(
        lambda page_index, requested_size: self.data.screener.get_most_active(
            category=Category.US_STOCK.name,
            rank_type=rank_type,
            sort_by=sort_by,
            direction="DESC",
            page_index=page_index,
            page_size=requested_size,
        ),
        total_limit,
        page_size,
    )


def top_gainers(
    self,
    total_limit: int,
    page_size: int,
    rank_type: str = "DAY_1",
) -> dict[str, dict]:
    """Page through Webull's gainers screener (stocks up the most today by
    price change), so the selection universe includes actual current
    uptrends/momentum names rather than only high-volume names.
    """
    from webull.data.common.category import Category

    return self._page_screener(
        lambda page_index, requested_size: self.data.screener.get_gainers_losers(
            rank_type=rank_type,
            category=Category.US_STOCK.name,
            sort_by="CHANGE_RATIO",
            direction="DESC",
            page_index=page_index,
            page_size=requested_size,
        ),
        total_limit,
        page_size,
    )


def top_losers(
    self,
    total_limit: int,
    page_size: int,
    rank_type: str = "DAY_1",
) -> dict[str, dict]:
    """Same screener as top_gainers, ranked ascending instead (today's
    biggest decliners).
    """
    from webull.data.common.category import Category

    return self._page_screener(
        lambda page_index, requested_size: self.data.screener.get_gainers_losers(
            rank_type=rank_type,
            category=Category.US_STOCK.name,
            sort_by="CHANGE_RATIO",
            direction="ASC",
            page_index=page_index,
            page_size=requested_size,
        ),
        total_limit,
        page_size,
    )


def historical_volatility(
    self,
    symbols: list[str],
    days: int,
) -> dict[str, float]:
    """Average daily amplitude percent over recent daily bars per symbol.

    Amplitude is (high - low) / close, a robust historical volatility
    proxy. Parsing is defensive across possible response shapes; symbols
    without usable data are omitted so callers can decide how to treat
    missing coverage.
    """
    from webull.data.common.category import Category
    from webull.data.common.timespan import Timespan

    unique = list(dict.fromkeys(symbol.upper() for symbol in symbols))
    count = str(max(days + 1, 6))
    results: dict[str, float] = {}
    for page in self._history_bars_chunks_concurrently(
        unique, Category.US_STOCK.name, Timespan.D.name, count
    ):
        for symbol, amplitude in self._parse_amplitudes(page, days).items():
            results[symbol] = amplitude
    return results


def _history_bars_chunks_concurrently(
    self, unique_symbols: list[str], category: str, timespan: str, count: str
) -> list[list]:
    """Chunks unique_symbols into 20-symbol pages and fetches them
    all CONCURRENTLY instead of one at a time - by request: "use
    more concurrent streams for other tasks as well as needed."
    Shared by historical_volatility/sma_trend/recent_minute_closes,
    all of which previously ran this exact chunk loop sequentially
    - for a large batch (sma_trend covers the WHOLE universe once
    daily, up to MAX_SYMBOLS), that was 50-250+ sequential network
    round-trips. _history_bars_resilient already fully catches its
    own exceptions per chunk (never raises - falls back to []), so
    firing chunks concurrently is safe: one chunk's failure only
    drops that chunk's symbols, same as running them sequentially.
    Bounded worker count (not unbounded), same real-request-volume
    reasoning as trade_stocks' own concurrent scan dispatch.
    """
    chunks = [
        unique_symbols[start : start + 20]
        for start in range(0, len(unique_symbols), 20)
    ]
    if not chunks:
        return []
    pages: list[list] = [[] for _ in chunks]

    def _fetch_chunk(index: int) -> None:
        pages[index] = self._history_bars_resilient(
            chunks[index], category, timespan, count
        )

    with ThreadPoolExecutor(max_workers=min(len(chunks), 8)) as pool:
        list(pool.map(_fetch_chunk, range(len(chunks))))
    return pages


def _history_bars_resilient(
    self,
    symbols: list[str],
    category: str,
    timespan: str,
    count: str,
) -> list:
    if not symbols:
        return []
    try:
        response = self._call(
            lambda: self.data.market_data.get_batch_history_bar(
                symbols,
                category,
                timespan,
                count,
            ),
            "market",
        )
        # Live response shape is {"result": [{"symbol": ..., "result":
        # [...bars]}]} - one level deeper than every caller here
        # (_parse_amplitudes/_parse_closes/recent_minute_closes)
        # assumed. Unwrapping only the outer layer here (the inner
        # per-symbol "result" key is handled by _extract_bars) kept
        # this call always silently returning zero usable rows -
        # VOLFILT's daily volatility pre-filter and the SMA trend
        # filter have both been getting 0/N coverage and falling
        # back to "no filtering" this whole time, and the new M1
        # bar-seeding got nothing to seed with either.
        if isinstance(response, dict):
            return response.get("result") or []
        return response
    except Exception as exc:
        message = str(exc)
        invalid_symbol = (
            "INVALID_SYMBOL" in message
            or "does not exist in the category" in message
        )
        if not invalid_symbol:
            logging.getLogger("webull-bot").warning(
                "VOLFILT | batch history failed | %s", exc
            )
            return []
        reported = self._invalid_symbols(message, symbols)
        if reported:
            remaining = [s for s in symbols if s not in reported]
            return self._history_bars_resilient(
                remaining, category, timespan, count
            )
        if len(symbols) == 1:
            return []
        middle = len(symbols) // 2
        return (
            self._history_bars_resilient(
                symbols[:middle], category, timespan, count
            )
            + self._history_bars_resilient(
                symbols[middle:], category, timespan, count
            )
        )


def _extract_bars(entry: dict) -> list | None:
    """A per-symbol history-bar entry's actual bar list can arrive
    under any of these keys depending on which shape the SDK/API
    happens to hand back - "result" is the one the live batch-history
    endpoint actually uses (see _history_bars_resilient), "bars"/
    "candles" kept as defensive fallbacks for any other shape.
    """
    for key in ("bars", "candles", "result"):
        value = entry.get(key)
        if isinstance(value, list):
            return value
    return None


def _parse_amplitudes(cls, page, days: int) -> dict[str, float]:
    amplitudes: dict[str, float] = {}
    for entry in page or []:
        if not isinstance(entry, dict):
            continue
        symbol = str(entry.get("symbol", "")).upper()
        if not symbol:
            continue
        bars = cls._extract_bars(entry)
        if bars is None:
            continue
        amplitude = cls._average_amplitude(bars, days)
        if amplitude is not None:
            amplitudes[symbol] = amplitude
    return amplitudes


def sma_trend(self, symbols: list[str], days: int) -> dict[str, float]:
    """Simple moving average of the last `days` daily closes per
    symbol - a real higher-timeframe trend reference built from actual
    daily bars, independent of the bot's own fast EMA(3/8) scalp
    signal (built from a handful of quarter-second tick polls, which
    isn't a meaningful multi-day trend read on its own). Same
    resilient batching/parsing structure as historical_volatility.
    """
    from webull.data.common.category import Category
    from webull.data.common.timespan import Timespan

    results: dict[str, float] = {}
    unique = list(dict.fromkeys(symbol.upper() for symbol in symbols))
    count = str(max(days, 6))
    for page in self._history_bars_chunks_concurrently(
        unique, Category.US_STOCK.name, Timespan.D.name, count
    ):
        for symbol, sma in self._parse_closes(page, days).items():
            results[symbol] = sma
    return results


def daily_closes(self, symbols: list[str], days: int) -> dict[str, list[float]]:
    """Real daily-bar closes per symbol, newest-first (same
    convention as _average_close's own bars[:days] slicing) - by
    request: "include not only short term patterns like 5-10 mins,
    but also 1 day and 5 day and month." sma_trend above only ever
    returns the averaged value; this returns the raw close series
    itself so a caller can compute point-to-point returns (1-day,
    5-day, ~20-trading-day/month) instead of just a moving average.
    Same resilient batching/parsing structure as sma_trend/
    historical_volatility.
    """
    from webull.data.common.category import Category
    from webull.data.common.timespan import Timespan

    results: dict[str, list[float]] = {}
    unique = list(dict.fromkeys(symbol.upper() for symbol in symbols))
    count = str(max(days, 6))
    for page in self._history_bars_chunks_concurrently(
        unique, Category.US_STOCK.name, Timespan.D.name, count
    ):
        for entry in page or []:
            if not isinstance(entry, dict):
                continue
            symbol = str(entry.get("symbol", "")).upper()
            if not symbol:
                continue
            bars = self._extract_bars(entry)
            if not bars:
                continue
            closes: list[float] = []
            for bar in bars[:days]:
                if not isinstance(bar, dict):
                    continue
                try:
                    close = float(bar.get("close"))
                except (TypeError, ValueError):
                    continue
                if close > 0:
                    closes.append(close)
            if closes:
                results[symbol] = closes
    return results


def _parse_closes(cls, page, days: int) -> dict[str, float]:
    closes: dict[str, float] = {}
    for entry in page or []:
        if not isinstance(entry, dict):
            continue
        symbol = str(entry.get("symbol", "")).upper()
        if not symbol:
            continue
        bars = cls._extract_bars(entry)
        if bars is None:
            continue
        sma = cls._average_close(bars, days)
        if sma is not None:
            closes[symbol] = sma
    return closes


def recent_minute_closes(
    self,
    symbols: list[str],
    category: str,
    count: int = 30,
) -> dict[str, list[float]]:
    """Real M1 bar closes per symbol, oldest-first - used to seed a
    symbol's volatility-scalp price window from actual intraday
    history the moment it's first scanned, instead of that window
    only building up one live snapshot poll at a time (several scan
    cycles just to accumulate enough samples to become eligible).
    Same batching/parsing structure as sma_trend/historical_volatility,
    just M1 instead of daily bars.
    """
    from webull.data.common.timespan import Timespan

    results: dict[str, list[float]] = {}
    unique = list(dict.fromkeys(symbol.upper() for symbol in symbols))
    pages = self._history_bars_chunks_concurrently(
        unique, category, Timespan.M1.name, str(count)
    )
    for page in pages:
        for entry in page or []:
            if not isinstance(entry, dict):
                continue
            symbol = str(entry.get("symbol", "")).upper()
            if not symbol:
                continue
            bars = self._extract_bars(entry)
            if not bars:
                continue
            closes = []
            for bar in bars[:count]:
                if not isinstance(bar, dict):
                    continue
                try:
                    close = float(bar.get("close"))
                except (TypeError, ValueError):
                    continue
                if close > 0:
                    closes.append(close)
            if closes:
                # bars arrive newest-first (same convention as the
                # daily-bar parsing above) - reverse to oldest-first
                # so appending live polls afterward stays chronological.
                results[symbol] = list(reversed(closes))
    return results


def _average_close(bars: list, days: int) -> float | None:
    samples: list[float] = []
    for bar in bars[:days]:
        if not isinstance(bar, dict):
            continue
        try:
            close = float(bar.get("close"))
        except (TypeError, ValueError):
            continue
        if close > 0:
            samples.append(close)
    if not samples:
        return None
    return sum(samples) / len(samples)


def _average_amplitude(bars: list, days: int) -> float | None:
    samples: list[float] = []
    for bar in bars[:days]:
        if not isinstance(bar, dict):
            continue
        try:
            high = float(bar.get("high"))
            low = float(bar.get("low"))
            close = float(bar.get("close"))
        except (TypeError, ValueError):
            continue
        reference = close if close > 0 else (high + low) / 2
        if reference <= 0 or high < low:
            continue
        samples.append((high - low) / reference * 100.0)
    if not samples:
        return None
    return sum(samples) / len(samples)


def analyst_target_price(self, symbol: str) -> Decimal | None:
    """Analyst mean target price, or None on no coverage (common for
    penny/micro-cap names) or any other failure - callers treat that
    the same as "no signal" rather than an error.
    """
    response = self._call(
        lambda: self.data.instrument.get_analyst_target_price(symbol),
        "stock_instrument",
    )
    mean = response.get("mean") if response else None
    if mean in (None, ""):
        return None
    try:
        return Decimal(str(mean))
    except Exception:
        return None


def analyst_rating(self, symbol: str) -> dict[str, int] | None:
    """Analyst rating consensus counts, or None on no coverage/failure -
    see analyst_target_price.
    """
    response = self._call(
        lambda: self.data.instrument.get_analyst_rating(symbol),
        "stock_instrument",
    )
    if not response:
        return None
    try:
        return {
            field: int(response.get(field) or 0)
            for field in (
                "strong_buy",
                "buy",
                "hold",
                "sell",
                "under_perform",
            )
        }
    except Exception:
        return None
