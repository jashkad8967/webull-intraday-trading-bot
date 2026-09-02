import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from uuid import uuid4

from webull_bot.config import Settings


class MarketDataPermissionError(RuntimeError):
    pass


class QuoteUnavailableError(RuntimeError):
    pass


class WebullAPI:
    # Webull's own hard cap on a single stock-snapshot request - callers
    # batching symbols across multiple cycles (see AutoTrader.
    # trade_stocks' final batch-size cap) need this exposed, not just
    # enforced as a private magic number inside stock_quotes below.
    STOCK_SNAPSHOT_MAX_SYMBOLS = 100

    def __init__(self, config: Settings):
        config.validate_connection(require_account=False)
        try:
            from webull.core.client import ApiClient
            from webull.data.data_client import DataClient
            from webull.trade.trade_client import TradeClient
        except ImportError as exc:
            raise RuntimeError("Run setup.ps1 to install the Webull SDK") from exc

        client = ApiClient(
            config.webull_app_key,
            config.webull_app_secret,
            config.webull_region_id,
        )
        client.add_endpoint(config.webull_region_id, config.host())
        # DataClient otherwise enables verbose SDK logging that can include
        # authentication headers in an error response.
        client._stream_logger_set = True
        client._file_logger_set = True
        for logger_name in ("webull", "webull.core", "webull.core.client"):
            sdk_logger = logging.getLogger(logger_name)
            sdk_logger.setLevel(logging.CRITICAL)
            sdk_logger.propagate = False
            sdk_logger.handlers.clear()
            sdk_logger.addHandler(logging.NullHandler())
        self.trade = TradeClient(client)
        self.data = DataClient(client)
        self.config = config
        self._lock = threading.Lock()
        self._last_request: dict[str, float] = {}

    def _request_interval(self, group: str) -> float:
        intervals = {
            "market": 60.0 / self.config.market_requests_per_minute,
            "option_instrument": (
                60.0 / self.config.option_instrument_requests_per_minute
            ),
            "stock_instrument": (
                30.0 / self.config.stock_instrument_requests_per_30_seconds
            ),
            "account": 1.0 / float(self.config.account_requests_per_second),
            "order": 60.0 / self.config.order_requests_per_minute,
        }
        return intervals[group]

    def _throttle(self, group: str) -> None:
        with self._lock:
            spacing = self._request_interval(group)
            wait = spacing - (
                time.monotonic() - self._last_request.get(group, float("-inf"))
            )
            if wait > 0:
                time.sleep(wait)
            self._last_request[group] = time.monotonic()

    def _call(self, callback, group: str, retry: bool = True):
        attempts = 4 if retry else 1
        for attempt in range(attempts):
            self._throttle(group)
            response = callback()
            if 200 <= response.status_code < 300:
                return response.json()
            if response.status_code not in (429, 500, 502, 503, 504):
                break
            if attempt + 1 < attempts:
                retry_after = response.headers.get("Retry-After", "")
                delay = float(retry_after) if retry_after.isdigit() else 2 ** attempt
                time.sleep(min(30.0, max(1.0, delay)))
        raise RuntimeError(f"Webull API error {response.status_code}: {response.text}")

    def accounts(self) -> list[dict]:
        return self._call(self.trade.account_v2.get_account_list, "account")

    def balance(self) -> dict:
        return self._call(
            lambda: self.trade.account_v2.get_account_balance(self.config.account_id),
            "account",
        )

    def buying_power(self) -> Decimal:
        return self.buying_power_from_balance(self.balance())

    @staticmethod
    def buying_power_from_balance(balance: dict) -> Decimal:
        usd = next(
            (
                item
                for item in balance.get("account_currency_assets", [])
                if item.get("currency") == "USD"
            ),
            {},
        )
        buying_power_values: list[Decimal] = []
        for field in (
            "day_buying_power",
            "buying_power",
            "overnight_buying_power",
        ):
            if usd.get(field) not in (None, ""):
                buying_power_values.append(Decimal(str(usd[field])))
        if buying_power_values:
            return max(Decimal("0"), *buying_power_values)
        cash = usd.get("cash_balance")
        return Decimal(str(cash)) if cash not in (None, "") else Decimal("0")

    @staticmethod
    def account_day_pnl_from_balance(balance: dict) -> Decimal | None:
        """Webull's own account-level today's total P&L (realized +
        unrealized since the prior session's close) - see
        total_day_profit_loss. Ground truth for the dashboard's headline
        P&L Today total, sourced from the same balance() call
        account_state already makes every cycle for buying_power (no
        extra network call). None if unreported.
        """
        reported = balance.get("total_day_profit_loss")
        if reported in (None, ""):
            return None
        try:
            return Decimal(str(reported))
        except Exception:
            return None

    @staticmethod
    def account_value_from_balance(balance: dict) -> Decimal | None:
        """Total net liquidation value (cash + market value of every
        held position) - the account's actual full worth, distinct from
        buying_power (spendable cash only). Sourced from the same
        balance() call account_state already makes every cycle.
        """
        reported = balance.get("total_net_liquidation_value")
        if reported in (None, ""):
            return None
        try:
            return Decimal(str(reported))
        except Exception:
            return None

    def positions(self) -> list[dict]:
        return self._call(
            lambda: self.trade.account_v2.get_account_position(self.config.account_id),
            "account",
        )

    def stock_quotes(
        self,
        symbols: list[str],
        category: str = "US_STOCK",
    ) -> list[dict]:
        from webull.data.common.category import Category

        if not symbols:
            return []
        if len(symbols) > self.STOCK_SNAPSHOT_MAX_SYMBOLS:
            raise ValueError(
                f"Webull stock snapshots accept at most "
                f"{self.STOCK_SNAPSHOT_MAX_SYMBOLS} symbols"
            )
        if category not in (Category.US_STOCK.name, Category.US_ETF.name):
            raise ValueError(f"Unsupported stock snapshot category: {category}")
        try:
            return self._call(
                lambda: self.data.market_data.get_snapshot(
                    symbols,
                    category,
                    True,
                    False,
                ),
                "market",
            )
        except Exception as exc:
            if self._subscription_required(exc):
                raise MarketDataPermissionError(
                    "OpenAPI stock quotes are not subscribed. "
                    "Enable Nasdaq Basic Non-Display in Webull's "
                    "OpenAPI Advanced Quotes center, then restart."
                ) from None
            raise

    def stock_quotes_resilient(
        self,
        symbols: list[str],
        category: str,
    ) -> tuple[list[dict], set[str]]:
        if not symbols:
            return [], set()
        try:
            return self.stock_quotes(symbols, category), set()
        except Exception as exc:
            message = str(exc)
            oversized = self._payload_too_large(message)
            invalid_symbol = (
                "INVALID_SYMBOL" in message
                or "does not exist in the category" in message
            )
            if not invalid_symbol and not oversized:
                raise
            if oversized:
                if len(symbols) == 1:
                    raise
                middle = len(symbols) // 2
                left_quotes, left_invalid = self.stock_quotes_resilient(
                    symbols[:middle],
                    category,
                )
                right_quotes, right_invalid = self.stock_quotes_resilient(
                    symbols[middle:],
                    category,
                )
                return left_quotes + right_quotes, left_invalid | right_invalid
            reported = self._invalid_symbols(message, symbols)
            if reported:
                valid = [symbol for symbol in symbols if symbol not in reported]
                if not valid:
                    return [], reported
                quotes, additional = self.stock_quotes_resilient(valid, category)
                return quotes, reported | additional
            if len(symbols) == 1:
                return [], {symbols[0]}
            middle = len(symbols) // 2
            left_quotes, left_invalid = self.stock_quotes_resilient(
                symbols[:middle],
                category,
            )
            right_quotes, right_invalid = self.stock_quotes_resilient(
                symbols[middle:],
                category,
            )
            return left_quotes + right_quotes, left_invalid | right_invalid

    @staticmethod
    def _payload_too_large(message: str) -> bool:
        # Live incident: once options discovery grew past 100
        # underlyings, trade_options' direction-signal quote fetch
        # started failing every single cycle with "Webull stock
        # snapshots accept at most 100 symbols" - stock_quotes' own
        # local pre-check (STOCK_SNAPSHOT_MAX_SYMBOLS), raised BEFORE
        # ever reaching the real API, not a server-reported 413. This
        # wasn't recognized as "oversized" here, so stock_quotes_
        # resilient's existing recursive-split path (already built and
        # working for genuine server-side oversized-payload errors)
        # never engaged, and the whole batch was simply lost every
        # cycle instead of being split - direction signals for every
        # underlying past the first ~100 just silently stopped updating.
        lowered = message.lower()
        return (
            "payload too large" in lowered
            or "request entity too large" in lowered
            or "content too large" in lowered
            or re.search(r"\b413\b", lowered) is not None
            or "accept at most" in lowered
        )

    @staticmethod
    def _invalid_symbols(message: str, requested: list[str]) -> set[str]:
        match = re.search(r"\[([^\]]+)\]", message)
        if not match:
            return set()
        allowed = set(requested)
        return {
            value.strip().upper()
            for value in match.group(1).split(",")
            if value.strip().upper() in allowed
        }

    def stock_quote(self, symbol: str, category: str | None = None) -> dict:
        if category is None:
            category = self.stock_categories([symbol]).get(symbol.upper(), "US_STOCK")
        data = self.stock_quotes([symbol], category)
        if not data:
            raise RuntimeError(f"No stock snapshot returned for {symbol}")
        return data[0]

    def stock_depth(self, symbol: str, category: str) -> dict | None:
        """Level-2 order-book depth for order-book-imbalance scoring.

        OBI is a secondary, best-effort signal - it must never be able to
        abort an otherwise-qualifying entry. Depth quotes need a separate
        market-data entitlement beyond the plain snapshot subscription, and
        in practice this account gets back a generic 500 INTERNAL_ERROR
        rather than a clean permission-denied response for it, so this
        catches *any* failure (not just the subscription-shaped one) on
        the first call, latches self._depth_unsupported, and every call
        after that short-circuits to None without hitting the API again -
        both to stop burning "market" rate-limit budget on a call that
        keeps failing, and because a raised exception here was otherwise
        propagating straight out of the BUY gate in trade_stocks() and
        aborting that symbol's entry for the cycle entirely.
        """
        if getattr(self, "_depth_unsupported", False):
            return None
        try:
            response = self._call(
                lambda: self.data.market_data.get_quotes(
                    symbol,
                    category,
                    depth="L2",
                ),
                "market",
            )
        except Exception as exc:
            self._depth_unsupported = True
            logging.getLogger("webull-bot").warning(
                "DEPTH  | L2 depth unavailable on this account | "
                "OBI falling back to top-of-book size for the rest of this "
                "run | %s",
                exc,
            )
            return None
        if not getattr(self, "_depth_logged", False):
            self._depth_logged = True
            logging.getLogger("webull-bot").debug(
                "DEPTH  | first non-empty depth payload | %s", response
            )
        return response

    @staticmethod
    def depth_imbalance(depth: dict | None, levels: int) -> Decimal | None:
        """bid_volume / (bid_volume + ask_volume) across the first `levels`
        price levels. Webull's exact depth JSON isn't documented in the
        bundled SDK (REST responses are plain JSON, no typed model), so
        this tries a few plausible shapes defensively rather than assuming
        one - confirm/adjust against the DEBUG-logged raw payload in
        stock_depth() once this runs against a live, entitled account.
        """
        if not depth:
            return None
        for bid_key, ask_key in (
            ("bids", "asks"),
            ("bidList", "askList"),
            ("bid", "ask"),
        ):
            bids = depth.get(bid_key)
            asks = depth.get(ask_key)
            if not isinstance(bids, list) or not isinstance(asks, list):
                continue
            if not bids or not asks:
                continue
            try:
                bid_volume = sum(
                    Decimal(str(level.get("volume", level.get("size", 0))))
                    for level in bids[:levels]
                )
                ask_volume = sum(
                    Decimal(str(level.get("volume", level.get("size", 0))))
                    for level in asks[:levels]
                )
            except Exception:
                continue
            total = bid_volume + ask_volume
            if total > 0:
                return bid_volume / total
        return None

    def option_quotes(self, option_symbols: list[str]) -> list[dict]:
        from webull.data.common.category import Category

        if not option_symbols:
            return []
        if len(option_symbols) > 20:
            raise ValueError("Webull option snapshots accept at most 20 symbols")
        try:
            return self._call(
                lambda: self.data.option_market_data.get_option_snapshot(
                    option_symbols,
                    Category.US_OPTION.name,
                ),
                "market",
            )
        except Exception as exc:
            if self._subscription_required(exc):
                raise MarketDataPermissionError(
                    "OpenAPI option quotes are not subscribed. "
                    "Enable OPRA Real-Time Non-Display in Webull's "
                    "OpenAPI Advanced Quotes center, then restart."
                ) from None
            raise

    @staticmethod
    def _subscription_required(exc: Exception) -> bool:
        message = str(exc).lower()
        return (
            ("unauthorized" in message or "insufficient permission" in message)
            and ("subscribe" in message or "permission" in message)
        )

    def option_quote(self, option_symbol: str) -> dict:
        data = self.option_quotes([option_symbol])
        if not data:
            raise RuntimeError(f"No option snapshot returned for {option_symbol}")
        return data[0]

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

    @staticmethod
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

    @staticmethod
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

    @classmethod
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

    @classmethod
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

    @staticmethod
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

    @staticmethod
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

    def option_contracts(
        self,
        underlying: str | None = None,
        option_symbol: str | None = None,
    ) -> list[dict]:
        from webull.data.common.category import Category

        contracts: list[dict] = []
        cursor = None
        while len(contracts) < 5000:
            page = self._call(
                lambda: self.data.instrument.get_option_contracts(
                    category=Category.US_OPTION.name,
                    underlying_symbols=underlying,
                    option_symbol=option_symbol,
                    status="LISTING",
                    page_size=1000,
                    last_instrument_id=cursor,
                ),
                "option_instrument",
            )
            if not page:
                break
            contracts.extend(page)
            next_cursor = page[-1].get("instrument_id")
            if len(page) < 1000 or not next_cursor or next_cursor == cursor:
                break
            cursor = str(next_cursor)
        return contracts

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

    def exact_option(self, option_symbol: str) -> dict:
        contracts = self.option_contracts(option_symbol=option_symbol)
        match = next(
            (
                item
                for item in contracts
                if item.get("symbol") == option_symbol
                and item.get("tradable_status", "OC") == "OC"
            ),
            None,
        )
        if not match:
            raise RuntimeError(f"Tradable option contract not found: {option_symbol}")
        return match

    def select_atm_options(
        self,
        underlying: str,
        stock_price: Decimal | None = None,
        max_contract_cost: Decimal | None = None,
    ) -> list[dict]:
        """By request: "look for cheaper options to buy in to." Always
        picking the single nearest-to-the-money strike is right for
        delta, but on a small account that strike's premium is often
        outright unaffordable (option_order_quantity then silently
        rounds down to 0 contracts - a real, but real trade). When
        max_contract_cost is given, quotes the OPTION_AFFORDABILITY_
        SHORTLIST_SIZE nearest-to-ATM candidates per expiration/type
        and picks the closest-to-the-money one whose premium*100 still
        fits - or, if none of them fit, the cheapest one quoted (better
        than falling through to an unaffordable ATM pick that never
        actually trades). Falls back to the plain nearest-to-ATM
        behavior (no extra quote calls) when max_contract_cost is None
        or a quote batch fails, same "degrade to the old behavior on
        any trouble" convention as the rest of this file.
        """
        if stock_price is None:
            stock_price = self.quote_price(self.stock_quote(underlying))
        minimum = date.today() + timedelta(days=self.config.option_min_dte)
        maximum = date.today() + timedelta(days=self.config.option_max_dte)
        option_types = (
            ("CALL", "PUT")
            if self.config.option_type == "BOTH"
            else (self.config.option_type,)
        )
        candidates: dict[str, list[dict]] = {kind: [] for kind in option_types}
        for item in self.option_contracts(underlying=underlying):
            expiration = date.fromisoformat(item["expiration_date"])
            # Live incident: Webull's contract listing marks some
            # contracts tradable_status="OC" that its OWN quote
            # endpoint then rejects outright as INVALID_SYMBOL
            # (observed: "2AVGO260908C00389030", "2AMD260910C00513030"
            # - a leading digit and a non-round strike, matching the
            # real OCC convention for a non-standard/adjusted contract
            # series, not a parsing bug on this side). Worse than just
            # skipping that one contract: option_quotes fetches a whole
            # BATCH at once, and Webull rejects the entire batch call
            # if even one symbol in it is invalid - silently blocking
            # every OTHER, genuinely valid contract riding along in the
            # same batch. A standard contract's symbol always starts
            # with its own underlying ticker exactly; requiring that
            # here filters these out before they ever get selected.
            if not str(item.get("symbol", "")).startswith(underlying):
                continue
            if (
                item.get("option_type") in candidates
                and item.get("tradable_status") == "OC"
                and minimum <= expiration <= maximum
            ):
                candidates[item["option_type"]].append(item)
        def _sort_key(item: dict):
            return (
                date.fromisoformat(item["expiration_date"]),
                abs(Decimal(str(item["strike_price"])) - stock_price),
            )

        selected: list[dict] = []
        for kind in option_types:
            pool = candidates[kind]
            if not pool:
                continue
            pool_sorted = sorted(pool, key=_sort_key)
            if max_contract_cost is None or len(pool_sorted) == 1:
                selected.append(pool_sorted[0])
                continue
            shortlist = pool_sorted[
                : self.config.option_affordability_shortlist_size
            ]
            try:
                quotes = self.option_quotes(
                    [item["symbol"] for item in shortlist]
                )
            except Exception:
                selected.append(pool_sorted[0])
                continue
            quote_by_symbol = {
                str(q.get("symbol")): q for q in quotes if isinstance(q, dict)
            }
            # shortlist is already ordered nearest-to-ATM first, and
            # this loop preserves that order - so the first affordable
            # entry found is the closest-to-the-money one that fits.
            priced: list[tuple[dict, Decimal]] = []
            for item in shortlist:
                quote = quote_by_symbol.get(item["symbol"])
                if not quote:
                    continue
                try:
                    premium = self.quote_price(quote)
                except Exception:
                    continue
                priced.append((item, premium * 100))
            affordable = [pair for pair in priced if pair[1] <= max_contract_cost]
            if affordable:
                selected.append(affordable[0][0])
            elif priced:
                selected.append(min(priced, key=lambda pair: pair[1])[0])
            else:
                selected.append(pool_sorted[0])
        if not selected:
            raise RuntimeError(f"No matching options found for {underlying}")
        return selected

    def resolve_options(self) -> list[dict]:
        contracts = [self.exact_option(symbol) for symbol in self.config.exact_options()]
        for underlying in self.config.option_roots():
            if underlying != "ALL":
                contracts.extend(self.select_atm_options(underlying))
        unique = {item["symbol"]: item for item in contracts}
        return list(unique.values())

    def open_orders(self) -> list[dict]:
        return self._call(
            lambda: self.trade.order_v3.get_order_open(
                self.config.account_id,
                page_size=100,
            ),
            "account",
        )

    @staticmethod
    def open_order_ids(groups: list[dict]) -> list[str]:
        order_ids: list[str] = []
        for group in groups or []:
            if group.get("client_order_id"):
                order_ids.append(str(group["client_order_id"]))
            else:
                order_ids.extend(
                    str(order["client_order_id"])
                    for order in (group.get("orders") or [])
                    if order.get("client_order_id")
                )
        return list(dict.fromkeys(order_ids))

    def cancel(self, client_order_id: str) -> None:
        self._call(
            lambda: self.trade.order_v3.cancel_order(
                self.config.account_id,
                client_order_id,
            ),
            "order",
        )

    def order_detail(self, client_order_id: str) -> dict:
        return self._call(
            lambda: self.trade.order_v3.get_order_detail(
                self.config.account_id,
                client_order_id,
            ),
            "order",
        )

    @staticmethod
    def order_status(detail: dict) -> str | None:
        """Best-effort terminal status of a fetched order (SUBMITTED,
        CANCELLED, FAILED, FILLED, PARTIAL_FILLED per the SDK's
        OrderStatus enum).

        Confirmed live shape: get_order_detail's top level carries no
        status field at all - it's nested one level down, inside the
        first entry of an "orders" list (the same grouped shape
        open_orders() returns), e.g.
        {"client_order_id": ..., "orders": [{"status": "CANCELLED",
        "filled_quantity": "0", ...}]}. The flat top-level keys are kept
        as a fallback in case a different order type/response variant
        ever puts it there instead. An unrecognized/missing shape
        returns None - callers must fail open (never treat None as
        either filled or cancelled) rather than guess.
        """
        orders = detail.get("orders") or []
        if orders and isinstance(orders[0], dict):
            for field in ("status", "order_status", "orderStatus"):
                value = orders[0].get(field)
                if value not in (None, ""):
                    return str(value).upper()
        for field in ("status", "order_status", "orderStatus"):
            value = detail.get(field)
            if value not in (None, ""):
                return str(value).upper()
        return None

    def cancel_all_orders(self) -> list[str]:
        unique = self.open_order_ids(self.open_orders())
        for order_id in unique:
            try:
                self.cancel(order_id)
            except Exception as exc:
                logging.getLogger("webull-bot").error(
                    "CANCEL | id=%s | %s",
                    order_id,
                    exc,
                )
        return unique

    def place_stock(
        self,
        symbol: str,
        side: str,
        quantity: int | Decimal,
        limit_price: Decimal | None = None,
        fractional: bool = False,
        market: bool = False,
    ) -> str:
        """fractional=True places a fixed-quantity MARKET order for a
        quantity in (0, 1] - Webull only supports fractional share trading
        as a MARKET order during core hours, never LIMIT and never extended
        hours, so those overrides are forced regardless of limit_price.

        market=True is the same MARKET/CORE-only override for a plain
        whole-share order - for a caller that wants a guaranteed-fill exit
        (e.g. an urgent manual sell) without going through the fractional-
        quantity machinery above.
        """
        client_order_id = uuid4().hex
        order = {
            "combo_type": "NORMAL",
            "client_order_id": client_order_id,
            "symbol": symbol,
            "instrument_type": "EQUITY",
            "market": "US",
            "order_type": (
                "MARKET" if fractional or market or limit_price is None else "LIMIT"
            ),
            "quantity": str(quantity),
            "support_trading_session": "CORE" if (fractional or market) else "ALL",
            "side": side,
            "time_in_force": "DAY",
            "entrust_type": "QTY",
        }
        if limit_price is not None and not fractional and not market:
            order["limit_price"] = str(
                limit_price.quantize(
                    self.price_tick_size(limit_price), rounding=ROUND_UP
                )
            )
        self._call(
            lambda: self.trade.order_v3.place_order(
                self.config.account_id,
                [order],
            ),
            "order",
            retry=False,
        )
        return client_order_id

    def order_history(self, start_date: str, end_date: str) -> list[dict]:
        """Every combo-order entry Webull has on record for the account
        in [start_date, end_date] (each "YYYY-MM-DD") - feeds
        AutoTrader.reconcile_order_history's log-only audit. Webull
        rejects a page_size under 10 outright, so this always requests
        the max useful page and paginates via last_client_order_id.

        Deliberately throttled under the "account" group, not "order" -
        this is a read-only, once-per-ORDER_HISTORY_RECONCILE_SECONDS
        (default 30 min) audit call, not a live trading action. Sharing
        the "order" group with actual order placement/cancellation meant
        it competed for the same rate budget as real-time trading
        activity - live incident: with volatility-scalp's fast cent-by-
        cent repricing (cancel+replace every ~1s per eligible position)
        now adding real load there, this non-critical audit call was
        getting starved out with a sustained 429 across every retry
        attempt. An audit falling a cycle behind is harmless; a live
        order call losing its rate-limit slot to an audit query is not.
        """
        orders: list[dict] = []
        cursor = None
        while True:
            page = self._call(
                lambda: self.trade.order_v3.get_order_history(
                    self.config.account_id,
                    page_size=100,
                    start_date=start_date,
                    end_date=end_date,
                    last_client_order_id=cursor,
                ),
                "account",
            )
            if not page:
                break
            orders.extend(page)
            if len(page) < 100:
                break
            cursor = page[-1].get("client_order_id")
            if not cursor:
                break
        return orders

    def _bid_ask_last_midpoint(self, quote: dict, bid: Decimal, ask: Decimal) -> Decimal:
        """bid/ask midpoint, blended with the last-trade print when it
        actually falls within the current spread - a passive mid-price
        that only ever looked at the top of the book ignored where the
        market was actually trading a moment ago. Last-trade is a print
        from BEFORE this quote's bid/ask, though, so it's only trusted
        when it's still consistent with the current spread (inside
        [bid, ask]) - a stale or off-spread print pulling the price
        outside the real market would be worse than not using it at all,
        so that case falls back to the plain midpoint.
        """
        last = self._quote_decimal(quote, "price")
        if last is not None and bid <= last <= ask:
            return (bid + ask + last) / 3
        return (bid + ask) / 2

    @staticmethod
    def price_tick_size(price: Decimal) -> Decimal:
        """The real increment a stock's price actually moves in - $0.0001
        under $1 (the standard US equity sub-penny allowance for stocks
        priced under a dollar), $0.01 at or above it. By request: these
        smaller/cheaper stocks quote with real 4-decimal precision (a
        live GAUZ quote showed bid=0.4592) - blanket-rounding every
        computed price to whole cents was throwing away up to a cent of
        real value per share on exactly the stocks where a cent is a
        meaningful fraction of the price.
        """
        return Decimal("0.0001") if price < 1 else Decimal("0.01")

    def stock_limit_price(self, quote: dict, side: str) -> Decimal:
        offset = self.config.stock_limit_offset
        if side in ("BUY", "SHORT"):
            # A SHORT entry is priced the same passive way as a BUY entry
            # (mid-price) - it's a normal opening trade, not an urgent
            # exit, so it shouldn't use the aggressive crossing price the
            # `else` branch below is tuned for (that one's for buy-to-cover
            # unwinds and stop/profit exits that need a fast fill).
            bid = self._sane_bid_or_ask(quote, "bid")
            ask = self._sane_bid_or_ask(quote, "ask")
            if not bid or not ask or bid > ask:
                raise QuoteUnavailableError(
                    "stock quote has no valid bid/ask spread for a buy"
                )
            price = self._bid_ask_last_midpoint(quote, bid, ask)
            price = max(Decimal("0.01"), price)
            return price.quantize(self.price_tick_size(price), rounding=ROUND_DOWN)
        elif side == "COVER":
            # Buying back a short to close it out is economically a BUY,
            # not a SELL - the aggressive-crossing `else` branch below
            # prices toward the bid (correct for an urgent SELL, backwards
            # for a BUY that needs to guarantee a fast fill). Cross above
            # the ask instead, mirroring the same offset/urgency the SELL
            # branch uses on the other side of the book.
            base = (
                self._sane_bid_or_ask(quote, "ask")
                or self._quote_decimal(quote, "price")
                or self._sane_bid_or_ask(quote, "bid")
            )
            price = base * (Decimal("1") + offset)
            price = max(Decimal("0.01"), price)
            return price.quantize(self.price_tick_size(price), rounding=ROUND_UP)
        else:
            base = (
                self._sane_bid_or_ask(quote, "bid")
                or self._quote_decimal(quote, "price")
                or self._sane_bid_or_ask(quote, "ask")
            )
            price = base * (Decimal("1") - offset)
        price = max(Decimal("0.01"), price)
        return price.quantize(self.price_tick_size(price), rounding=ROUND_UP)

    def stock_stop_exit_price(self, quote: dict) -> Decimal:
        """Midpoint sell limit for a stop-loss exit.

        Deliberately gentler than stock_limit_price's SELL side (which
        crosses below the bid to guarantee a fill for closeouts): a
        stop-loss should cap the loss precisely rather than chase an
        immediate fill, since overshooting the bid on a fast-moving or
        thin quote is what turns a bounded stop into a much larger loss.
        """
        bid = self._sane_bid_or_ask(quote, "bid")
        ask = self._sane_bid_or_ask(quote, "ask")
        if not bid or not ask or bid > ask:
            raise QuoteUnavailableError(
                "stock quote has no valid bid/ask spread for a stop exit"
            )
        price = self._bid_ask_last_midpoint(quote, bid, ask)
        price = max(Decimal("0.01"), price)
        return price.quantize(self.price_tick_size(price), rounding=ROUND_DOWN)

    def option_limit_price(self, quote: dict, side: str) -> Decimal:
        offset = self.config.option_limit_offset
        if side == "BUY":
            bid = self._sane_bid_or_ask(quote, "bid")
            ask = self._sane_bid_or_ask(quote, "ask")
            if not bid or not ask or bid > ask:
                raise QuoteUnavailableError(
                    "option quote has no valid bid/ask spread for a buy"
                )
            price = (bid + ask) / 2
            return max(Decimal("0.01"), price).quantize(
                Decimal("0.01"),
                rounding=ROUND_DOWN,
            )
        else:
            base = (
                self._sane_bid_or_ask(quote, "bid")
                or self._quote_decimal(quote, "price")
                or self._sane_bid_or_ask(quote, "ask")
            )
            price = base * (Decimal("1") - offset)
        return max(Decimal("0.01"), price).quantize(Decimal("0.01"), rounding=ROUND_UP)

    @staticmethod
    def _quote_decimal(quote: dict, field: str) -> Decimal | None:
        value = quote.get(field)
        if value in (None, ""):
            return None
        try:
            number = Decimal(str(value))
        except Exception:
            return None
        return number if number.is_finite() and number > 0 else None

    def _sane_bid_or_ask(self, quote: dict, field: str) -> Decimal | None:
        """bid/ask extraction with a sanity check against the same quote's
        own last-trade price.

        Live incident: FPE's ask sat at $20.08 (once even $28.49) while
        every other field on the same quote snapshot - last-trade price,
        open/high/low, the broker's own cost basis - was consistently
        ~$17.7-17.8. Every bid/ask consumer in this file went through
        _quote_decimal directly or a raw quote["bid"]/quote["ask"] read,
        so that one broken field fed a limit price no real order could
        ever fill, repeatedly, for hours. Routing every bid/ask read
        through here instead means a broken quote is treated as
        unavailable (falls through to the price-based fallback already in
        each caller) everywhere at once, rather than needing to be caught
        at each call site individually.
        """
        value = self._quote_decimal(quote, field)
        if value is None:
            return None
        reference = self._quote_decimal(quote, "price")
        if reference is None:
            return value
        if abs(value - reference) / reference > self.config.quote_price_sanity_percent:
            return None
        return value

    def option_delta(self, quote: dict) -> Decimal | None:
        """Best-effort delta extraction. Webull's option snapshot response
        isn't a typed model in the bundled SDK (plain passthrough dict), so
        whether/how it exposes delta on this account is unconfirmed - tries
        a few plausible key names and returns None (quality gate passes
        through) rather than guessing wrong. Logs the raw quote once at
        DEBUG the first time a real value shows up so the field name can be
        confirmed/adjusted from a live log, same precedent as stock_depth.
        """
        return self._option_greek_field(quote, ("delta", "Delta"))

    def option_implied_vol(self, quote: dict) -> Decimal | None:
        return self._option_greek_field(
            quote, ("impliedVol", "implied_vol", "iv", "impliedVolatility")
        )

    def _option_greek_field(self, quote: dict, fields: tuple[str, ...]) -> Decimal | None:
        for field in fields:
            value = quote.get(field)
            if value in (None, ""):
                continue
            try:
                number = Decimal(str(value))
            except Exception:
                continue
            if not number.is_finite():
                continue
            if not getattr(self, "_option_greeks_logged", False):
                self._option_greeks_logged = True
                logging.getLogger("webull-bot").debug(
                    "OPTION | first option snapshot with a greek field | %s",
                    quote,
                )
            return number
        return None

    def quote_bid(self, quote: dict) -> Decimal | None:
        return self._sane_bid_or_ask(quote, "bid")

    def quote_ask(self, quote: dict) -> Decimal | None:
        return self._sane_bid_or_ask(quote, "ask")

    @staticmethod
    def quote_price(quote: dict) -> Decimal:
        regular_time = int(quote.get("last_trade_time") or 0)
        extended_time = int(quote.get("extend_hour_last_trade_time") or 0)
        fields = (
            ("extend_hour_last_price", "price", "ask", "bid")
            if extended_time >= regular_time and extended_time > 0
            else ("price", "ask", "bid", "extend_hour_last_price")
        )
        for field in fields:
            value = quote.get(field)
            if value in (None, ""):
                continue
            try:
                price = Decimal(str(value))
            except Exception:
                continue
            if price.is_finite() and price > 0:
                return price
        raise QuoteUnavailableError("quote has no numeric positive price")

    def place_option(
        self,
        contract: dict,
        side: str,
        quantity: int,
        limit_price: Decimal,
        position_intent: str,
    ) -> str:
        client_order_id = uuid4().hex
        underlying = contract["underlying_symbol"]
        order = {
            "client_order_id": client_order_id,
            "combo_type": "NORMAL",
            "option_strategy": "SINGLE",
            "order_type": "LIMIT",
            "limit_price": str(limit_price),
            "quantity": str(quantity),
            "side": side,
            "position_intent": position_intent,
            "time_in_force": "DAY",
            "entrust_type": "QTY",
            "instrument_type": "OPTION",
            "market": "US",
            "symbol": underlying,
            "legs": [
                {
                    "side": side,
                    "quantity": str(quantity),
                    "symbol": underlying,
                    "strike_price": str(contract["strike_price"]),
                    "option_expire_date": contract["expiration_date"],
                    "instrument_type": "OPTION",
                    "option_type": contract["option_type"],
                    "market": "US",
                }
            ],
        }
        self._call(
            lambda: self.trade.order_v3.place_order(
                self.config.account_id,
                [order],
            ),
            "order",
            retry=False,
        )
        return client_order_id

    @staticmethod
    def stock_quantity(symbol: str, positions: list[dict]) -> int:
        return sum(
            int(Decimal(str(item.get("quantity", "0"))))
            for item in positions
            if item.get("instrument_type") == "EQUITY" and item.get("symbol") == symbol
        )

    @staticmethod
    def option_quantity(contract: dict, positions: list[dict]) -> int:
        total = 0
        for item in positions:
            if item.get("instrument_type") != "OPTION":
                continue
            if item.get("symbol") == contract["symbol"]:
                total += int(Decimal(str(item.get("quantity", "0"))))
                continue
            for leg in item.get("legs", []):
                if (
                    leg.get("symbol") == contract["underlying_symbol"]
                    and leg.get("option_type") == contract["option_type"]
                    and leg.get("option_expire_date") == contract["expiration_date"]
                    and Decimal(str(leg.get("option_exercise_price", "0")))
                    == Decimal(str(contract["strike_price"]))
                ):
                    total += int(Decimal(str(item.get("quantity", leg.get("quantity", "0")))))
        return total

    @staticmethod
    def stock_position(symbol: str, positions: list[dict]) -> tuple[Decimal, Decimal]:
        """Quantity is a Decimal, not an int - a fractional-share position
        (e.g. 0.5) would otherwise truncate to 0 here and become invisible
        to every decision/exit/closeout path that checks this quantity.
        """
        match = next(
            (
                item
                for item in positions
                if item.get("instrument_type") == "EQUITY"
                and item.get("symbol") == symbol
            ),
            None,
        )
        if not match:
            return Decimal("0"), Decimal("0")
        return (
            Decimal(str(match.get("quantity", "0"))),
            Decimal(str(match.get("cost_price", "0"))),
        )

    def option_position(
        self,
        contract: dict,
        positions: list[dict],
    ) -> tuple[int, Decimal]:
        quantity = self.option_quantity(contract, positions)
        if not quantity:
            return 0, Decimal("0")
        for item in positions:
            if item.get("instrument_type") != "OPTION":
                continue
            if item.get("symbol") == contract["symbol"]:
                return quantity, Decimal(str(item.get("cost_price", "0")))
            for leg in item.get("legs", []):
                if (
                    leg.get("symbol") == contract["underlying_symbol"]
                    and leg.get("option_type") == contract["option_type"]
                    and leg.get("option_expire_date") == contract["expiration_date"]
                    and Decimal(str(leg.get("option_exercise_price", "0")))
                    == Decimal(str(contract["strike_price"]))
                ):
                    return quantity, Decimal(str(item.get("cost_price", "0")))
        return quantity, Decimal("0")

    def contract_from_position(self, position: dict) -> dict | None:
        if position.get("instrument_type") != "OPTION":
            return None
        symbol = str(position.get("symbol", ""))
        if len(symbol) > 10:
            try:
                return self.exact_option(symbol)
            except Exception:
                pass
        legs = position.get("legs", [])
        if not legs:
            return None
        leg = legs[0]
        underlying = leg.get("symbol") or symbol
        for contract in self.option_contracts(underlying=underlying):
            if (
                contract.get("option_type") == leg.get("option_type")
                and contract.get("expiration_date") == leg.get("option_expire_date")
                and Decimal(str(contract.get("strike_price", "0")))
                == Decimal(str(leg.get("option_exercise_price", "0")))
            ):
                return contract
        return None

    def close_all_positions(
        self,
        instrument_types: set[str] | None = None,
        loss_callback=None,
        exclude_symbols: set[str] | None = None,
    ) -> list[str]:
        positions = [
            position
            for position in self.positions()
            if (
                instrument_types is None
                or position.get("instrument_type") in instrument_types
            )
            and (
                not exclude_symbols
                or str(position.get("symbol", "")).upper() not in exclude_symbols
            )
        ]
        if not positions:
            return []
        self.cancel_all_orders()
        submitted: list[str] = []

        def close_one(position: dict, quantity: Decimal) -> None:
            if position.get("instrument_type") == "EQUITY":
                side = "SELL" if quantity > 0 else "BUY"
                # Pricing side is distinct from the broker order side above:
                # covering a short still submits as a plain "BUY" (Webull's
                # order API has no fourth side value), but it needs the
                # urgent cross-the-ask "COVER" pricing branch, not the
                # passive mid-price one plain "BUY" entries use.
                pricing_side = "SELL" if quantity > 0 else "COVER"
                quote = self.stock_quote(position["symbol"])
                market_price = self.quote_price(quote)
                average_cost = Decimal(str(position.get("cost_price") or "0"))
                loss_exit = (
                    quantity > 0
                    and average_cost > 0
                    and market_price < average_cost
                ) or (
                    quantity < 0
                    and average_cost > 0
                    and market_price > average_cost
                )
                limit_price = self.stock_limit_price(quote, pricing_side)
                fractional = abs(quantity) != abs(quantity).to_integral_value()
                submitted.append(
                    self.place_stock(
                        position["symbol"],
                        side,
                        abs(quantity),
                        limit_price,
                        fractional=fractional,
                    )
                )
                if loss_exit and loss_callback:
                    loss_callback(position["symbol"], "loss closeout submitted")
            elif position.get("instrument_type") == "OPTION":
                contract = self.contract_from_position(position)
                if not contract:
                    logging.getLogger("webull-bot").error(
                        "CLOSE  | unresolved option=%s",
                        position.get("symbol", "UNKNOWN"),
                    )
                    return
                side = "SELL" if quantity > 0 else "BUY"
                intent = "SELL_TO_CLOSE" if quantity > 0 else "BUY_TO_CLOSE"
                quote = self.option_quote(contract["symbol"])
                average_cost = Decimal(str(position.get("cost_price") or "0"))
                market_price = self.quote_price(quote)
                loss_exit = (
                    quantity > 0
                    and average_cost > 0
                    and market_price < average_cost
                ) or (
                    quantity < 0
                    and average_cost > 0
                    and market_price > average_cost
                )
                limit_price = self.option_limit_price(quote, side)
                submitted.append(
                    self.place_option(
                        contract,
                        side,
                        abs(quantity),
                        limit_price,
                        intent,
                    )
                )
                if loss_exit and loss_callback:
                    loss_callback(
                        contract["underlying_symbol"],
                        "option loss closeout submitted",
                    )

        for position in positions:
            quantity = Decimal(str(position.get("quantity", "0")))
            if not quantity:
                continue
            try:
                close_one(position, quantity)
            except Exception as exc:
                # One position rejected (e.g. a sub-100-share position
                # stuck in Webull's $0.10-$0.999 lot-restricted band, which
                # rejects orders on ANY size below 100 there) must never
                # abort closing every other position in this batch - this
                # loop previously had no exception handling at all, so an
                # unwrapped raise here silently skipped the rest of the EOD
                # closeout, options included, for the whole account.
                logging.getLogger("webull-bot").error(
                    "CLOSE  | %s | close order failed, continuing with "
                    "remaining positions | %s",
                    position.get("symbol", "UNKNOWN"),
                    exc,
                )
        return submitted
