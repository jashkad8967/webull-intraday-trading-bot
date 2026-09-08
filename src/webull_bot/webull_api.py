import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from uuid import uuid4

from webull_bot.api.errors import MarketDataPermissionError, QuoteUnavailableError
from webull_bot.api.pricing import (
    _bid_ask_last_midpoint,
    _quantize_to_option_tick,
    option_limit_price,
    option_price_tick_size,
    price_tick_size,
    stock_limit_price,
    stock_stop_exit_price,
)
from webull_bot.api.orders import (
    cancel,
    cancel_all_orders,
    close_all_positions,
    open_order_ids,
    open_orders,
    order_detail,
    order_history,
    order_status,
    place_stock,
)
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

    open_orders = open_orders
    open_order_ids = staticmethod(open_order_ids)
    cancel = cancel
    order_detail = order_detail
    order_status = staticmethod(order_status)
    cancel_all_orders = cancel_all_orders
    place_stock = place_stock
    order_history = order_history

    _bid_ask_last_midpoint = _bid_ask_last_midpoint
    price_tick_size = staticmethod(price_tick_size)
    stock_limit_price = stock_limit_price
    stock_stop_exit_price = stock_stop_exit_price
    option_price_tick_size = staticmethod(option_price_tick_size)
    _quantize_to_option_tick = classmethod(_quantize_to_option_tick)
    option_limit_price = option_limit_price

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

    close_all_positions = close_all_positions
