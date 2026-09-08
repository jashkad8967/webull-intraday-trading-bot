import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from uuid import uuid4

from webull_bot.api.errors import MarketDataPermissionError, QuoteUnavailableError
from webull_bot.api.market_data import (
    _average_amplitude,
    _average_close,
    _extract_bars,
    _history_bars_chunks_concurrently,
    _history_bars_resilient,
    _page_screener,
    _parse_amplitudes,
    _parse_closes,
    _screener_number,
    _stock_instruments_resilient,
    analyst_rating,
    analyst_target_price,
    daily_closes,
    historical_volatility,
    recent_minute_closes,
    sma_trend,
    stock_categories,
    stock_universe,
    top_active_stocks,
    top_gainers,
    top_losers,
)
from webull_bot.api.quotes import (
    _invalid_symbols,
    _payload_too_large,
    _quote_decimal,
    _sane_bid_or_ask,
    _subscription_required,
    depth_imbalance,
    option_quote,
    option_quotes,
    quote_ask,
    quote_bid,
    quote_price,
    stock_depth,
    stock_quote,
    stock_quotes,
    stock_quotes_resilient,
)
from webull_bot.api.positions import (
    account_day_pnl_from_balance,
    account_value_from_balance,
    accounts,
    balance,
    buying_power,
    buying_power_from_balance,
    option_buying_power,
    option_buying_power_from_balance,
    positions,
    stock_position,
    stock_quantity,
)
from webull_bot.config import Settings


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

    accounts = accounts
    balance = balance
    buying_power = buying_power
    buying_power_from_balance = staticmethod(buying_power_from_balance)
    option_buying_power = option_buying_power
    option_buying_power_from_balance = staticmethod(
        option_buying_power_from_balance
    )
    account_day_pnl_from_balance = staticmethod(account_day_pnl_from_balance)
    account_value_from_balance = staticmethod(account_value_from_balance)
    positions = positions

    stock_quotes = stock_quotes
    stock_quotes_resilient = stock_quotes_resilient
    _payload_too_large = staticmethod(_payload_too_large)
    _invalid_symbols = staticmethod(_invalid_symbols)
    stock_quote = stock_quote
    stock_depth = stock_depth
    depth_imbalance = staticmethod(depth_imbalance)
    option_quotes = option_quotes
    _subscription_required = staticmethod(_subscription_required)
    option_quote = option_quote

    stock_categories = stock_categories
    _stock_instruments_resilient = _stock_instruments_resilient
    stock_universe = stock_universe
    _screener_number = staticmethod(_screener_number)
    _page_screener = _page_screener
    top_active_stocks = top_active_stocks
    top_gainers = top_gainers
    top_losers = top_losers
    historical_volatility = historical_volatility
    _history_bars_chunks_concurrently = _history_bars_chunks_concurrently
    _history_bars_resilient = _history_bars_resilient
    _extract_bars = staticmethod(_extract_bars)
    _parse_amplitudes = classmethod(_parse_amplitudes)
    sma_trend = sma_trend
    daily_closes = daily_closes
    _parse_closes = classmethod(_parse_closes)
    recent_minute_closes = recent_minute_closes
    _average_close = staticmethod(_average_close)
    _average_amplitude = staticmethod(_average_amplitude)

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

    analyst_target_price = analyst_target_price
    analyst_rating = analyst_rating

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
            # By request: "look at contracts in those stocks with high
            # volume movement and volatility" - among the AFFORDABLE
            # candidates, prefer the one with the most contract volume
            # (liquidity/activity), breaking ties by shortlist order
            # (nearest-to-ATM first, since shortlist is already sorted
            # that way). Affordability stays the hard filter; volume
            # only re-ranks within it, so this never picks a contract
            # that doesn't fit max_contract_cost.
            priced: list[tuple[dict, Decimal, Decimal, int]] = []
            for index, item in enumerate(shortlist):
                quote = quote_by_symbol.get(item["symbol"])
                if not quote:
                    continue
                try:
                    premium = self.quote_price(quote)
                except Exception:
                    continue
                volume = self.option_volume(quote) or Decimal("0")
                priced.append((item, premium * 100, volume, index))
            affordable = [pair for pair in priced if pair[1] <= max_contract_cost]
            # By request: "these are definitely not the most volatile
            # contracts, I know TSLA contracts move like crazy" - a
            # high-priced underlying's near-ATM strikes can ALL be
            # unaffordable on a small account (the old fallback below
            # then just picked the cheapest of that same unaffordable
            # cluster, which still doesn't fit - buy_quantity ends up
            # 0 downstream and the underlying silently never trades
            # despite being "selected" every cycle). Rather than give
            # up at the near-ATM shortlist, walk further OUT into the
            # same expiration/type pool (further OTM = cheaper premium,
            # still a real, tradable contract on that same volatile
            # underlying) until an affordable one turns up. Bounded to
            # a few extra quote batches (option_quotes caps at 20/call)
            # so a single expensive underlying can't blow up this
            # cycle's request budget.
            if not affordable:
                already_quoted = {item["symbol"] for item in shortlist}
                remaining = [
                    item for item in pool_sorted if item["symbol"] not in already_quoted
                ]
                for start in range(0, min(len(remaining), 60), 20):
                    chunk = remaining[start : start + 20]
                    if not chunk:
                        break
                    try:
                        extra_quotes = self.option_quotes(
                            [item["symbol"] for item in chunk]
                        )
                    except Exception:
                        break
                    extra_by_symbol = {
                        str(q.get("symbol")): q
                        for q in extra_quotes
                        if isinstance(q, dict)
                    }
                    found = False
                    for item in chunk:
                        quote = extra_by_symbol.get(item["symbol"])
                        if not quote:
                            continue
                        try:
                            premium = self.quote_price(quote)
                        except Exception:
                            continue
                        cost = premium * 100
                        if cost <= max_contract_cost:
                            affordable.append(
                                (item, cost, self.option_volume(quote) or Decimal("0"), -1)
                            )
                            found = True
                    if found:
                        break
            if affordable:
                best = max(affordable, key=lambda pair: (pair[2], -pair[3]))
                selected.append(best[0])
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

    @staticmethod
    def option_price_tick_size(premium: Decimal) -> Decimal:
        """The real increment an option premium is allowed to be quoted
        in - live incident: a real order attempt at $7.47 (AAPL, a
        premium >= $3) was rejected outright with OPENAPI_OPTION_
        PRICE_STEP_GTE ("Orders placed with a premium of $3 or more
        must be in increments of 0.05"). option_limit_price used to
        always round to a flat $0.01 regardless of premium level - this
        would have blocked every real option order whose premium ever
        cleared $3, silently until the first live attempt actually hit
        it (never verified live before this, since nothing had reached
        order placement yet).

        Live incident (this fix): a second, distinct rejection -
        OPENAPI_OPTION_PRICE_STEP_LT ("Orders placed with a premium of
        less than $3 must be in increments of 0.05") - hit repeatedly
        on a different underlying (CD), directly contradicting the
        $0.01-below-$3 assumption this used to make. Standard OCC tick
        rules only allow $0.01 increments below $3 for "Penny Pilot"
        underlyings; most names aren't enrolled, and Webull's API gives
        no cheap way to tell which is which ahead of a real order
        attempt. $0.05 is always a valid point on the $0.01 grid too,
        so quoting every premium at $0.05 (not just >= $3) satisfies
        both rule variants unconditionally, at the cost of the finer
        penny-level granularity Penny Pilot names could otherwise use.
        """
        return Decimal("0.05")

    @classmethod
    def _quantize_to_option_tick(
        cls, price: Decimal, rounding: str
    ) -> Decimal:
        tick = cls.option_price_tick_size(price)
        steps = (price / tick).quantize(Decimal("1"), rounding=rounding)
        return (steps * tick).quantize(Decimal("0.01"))

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
            return self._quantize_to_option_tick(
                max(Decimal("0.01"), price), ROUND_DOWN
            )
        else:
            base = (
                self._sane_bid_or_ask(quote, "bid")
                or self._quote_decimal(quote, "price")
                or self._sane_bid_or_ask(quote, "ask")
            )
            price = base * (Decimal("1") - offset)
        return self._quantize_to_option_tick(
            max(Decimal("0.01"), price), ROUND_UP
        )

    _quote_decimal = staticmethod(_quote_decimal)
    _sane_bid_or_ask = _sane_bid_or_ask

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

    def option_volume(self, quote: dict) -> Decimal | None:
        """Best-effort contract volume extraction, same unconfirmed-field-
        name situation as option_delta/option_implied_vol above - tries the
        plausible key names rather than guessing one. By request: "look for
        contracts in those stocks with high volume movement and volatility"
        - contract selection previously only weighed DTE/ATM-proximity/
        affordability, never the contract's own trading activity.
        """
        return self._option_greek_field(
            quote, ("volume", "tradeVolume", "dealNum", "totalShares")
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

    quote_bid = quote_bid
    quote_ask = quote_ask
    quote_price = staticmethod(quote_price)

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

    stock_quantity = staticmethod(stock_quantity)

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

    stock_position = staticmethod(stock_position)

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
