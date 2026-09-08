import logging
import re
from decimal import Decimal

from webull_bot.api.errors import MarketDataPermissionError, QuoteUnavailableError


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


def quote_bid(self, quote: dict) -> Decimal | None:
    return self._sane_bid_or_ask(quote, "bid")


def quote_ask(self, quote: dict) -> Decimal | None:
    return self._sane_bid_or_ask(quote, "ask")


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
