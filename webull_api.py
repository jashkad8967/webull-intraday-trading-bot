import logging
import re
import threading
import time
from datetime import date, timedelta
from decimal import Decimal, ROUND_UP
from uuid import uuid4

from config import Settings


class WebullAPI:
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
        balance = self.balance()
        usd = next(
            (
                item
                for item in balance.get("account_currency_assets", [])
                if item.get("currency") == "USD"
            ),
            {},
        )
        for field in (
            "buying_power",
            "overnight_buying_power",
            "day_buying_power",
            "cash_balance",
        ):
            if usd.get(field) not in (None, ""):
                return Decimal(str(usd[field]))
        return Decimal("0")

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
        if len(symbols) > 100:
            raise ValueError("Webull stock snapshots accept at most 100 symbols")
        if category not in (Category.US_STOCK.name, Category.US_ETF.name):
            raise ValueError(f"Unsupported stock snapshot category: {category}")
        return self._call(
            lambda: self.data.market_data.get_snapshot(
                symbols,
                category,
                False,
                False,
            ),
            "market",
        )

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
            invalid_symbol = (
                "INVALID_SYMBOL" in message
                or "does not exist in the category" in message
            )
            if not invalid_symbol:
                raise
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

    def option_quotes(self, option_symbols: list[str]) -> list[dict]:
        from webull.data.common.category import Category

        if not option_symbols:
            return []
        if len(option_symbols) > 20:
            raise ValueError("Webull option snapshots accept at most 20 symbols")
        return self._call(
            lambda: self.data.option_market_data.get_option_snapshot(
                option_symbols,
                Category.US_OPTION.name,
            ),
            "market",
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
    ) -> dict[str, str]:
        from webull.data.common.category import Category

        categories: dict[str, str] = {}
        instrument_categories = (Category.US_STOCK.name, Category.US_ETF.name)
        if self.config.max_symbols:
            stock_limit = (self.config.max_symbols + 1) // 2
            category_limits = {
                Category.US_STOCK.name: stock_limit,
                Category.US_ETF.name: self.config.max_symbols - stock_limit,
            }
        else:
            category_limits = {category: 0 for category in instrument_categories}

        for category in instrument_categories:
            category_count = 0
            category_limit = category_limits[category]
            cursor = None
            while True:
                page_size = (
                    1000
                    if category_limit == 0
                    else min(1000, category_limit - category_count)
                )
                if page_size <= 0:
                    break
                page = self._call(
                    lambda category=category, cursor=cursor, page_size=page_size: (
                        self.data.instrument.get_instrument(
                            category=category,
                            last_instrument_id=cursor,
                            page_size=page_size,
                        )
                    ),
                    "stock_instrument",
                )
                if not page:
                    break
                for item in page:
                    symbol = str(item.get("symbol", "")).upper()
                    if symbol and item.get("tradable_status", "OC") == "OC":
                        # ETF is fetched second and intentionally wins if the
                        # instrument endpoint returns it in both categories.
                        categories[symbol] = category
                        category_count += 1
                        if category_limit and category_count >= category_limit:
                            break
                if progress:
                    progress(category, category_count, category_limit)
                next_cursor = page[-1].get("instrument_id")
                if (
                    (category_limit and category_count >= category_limit)
                    or len(page) < page_size
                    or not next_cursor
                    or str(next_cursor) == cursor
                ):
                    break
                cursor = str(next_cursor)
        return categories

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
    ) -> list[dict]:
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
            if (
                item.get("option_type") in candidates
                and item.get("tradable_status") == "OC"
                and minimum <= expiration <= maximum
            ):
                candidates[item["option_type"]].append(item)
        selected = [
            min(
                candidates[kind],
                key=lambda item: (
                    date.fromisoformat(item["expiration_date"]),
                    abs(Decimal(str(item["strike_price"])) - stock_price),
                ),
            )
            for kind in option_types
            if candidates[kind]
        ]
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

    def cancel(self, client_order_id: str) -> None:
        self._call(
            lambda: self.trade.order_v3.cancel_order(
                self.config.account_id,
                client_order_id,
            ),
            "order",
        )

    def cancel_all_orders(self) -> list[str]:
        order_ids: list[str] = []
        for group in self.open_orders():
            if group.get("client_order_id"):
                order_ids.append(str(group["client_order_id"]))
            else:
                order_ids.extend(
                    str(order["client_order_id"])
                    for order in group.get("orders", [])
                    if order.get("client_order_id")
                )
        unique = list(dict.fromkeys(order_ids))
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

    def place_stock(self, symbol: str, side: str, quantity: int) -> str:
        client_order_id = uuid4().hex
        order = {
            "combo_type": "NORMAL",
            "client_order_id": client_order_id,
            "symbol": symbol,
            "instrument_type": "EQUITY",
            "market": "US",
            "order_type": "MARKET",
            "quantity": str(quantity),
            "support_trading_session": "CORE",
            "side": side,
            "time_in_force": "DAY",
            "entrust_type": "QTY",
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

    def option_limit_price(self, quote: dict, side: str) -> Decimal:
        offset = self.config.option_limit_offset
        if side == "BUY":
            base = Decimal(
                str(quote.get("ask") or quote.get("price") or quote.get("bid"))
            )
            price = base * (Decimal("1") + offset)
        else:
            base = Decimal(
                str(quote.get("bid") or quote.get("price") or quote.get("ask"))
            )
            price = base * (Decimal("1") - offset)
        return max(Decimal("0.01"), price).quantize(Decimal("0.01"), rounding=ROUND_UP)

    @staticmethod
    def quote_price(quote: dict) -> Decimal:
        value = quote.get("price") or quote.get("ask") or quote.get("bid")
        if value in (None, ""):
            raise RuntimeError("Quote did not contain a usable price")
        return Decimal(str(value))

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
    def stock_position(symbol: str, positions: list[dict]) -> tuple[int, Decimal]:
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
            return 0, Decimal("0")
        return (
            int(Decimal(str(match.get("quantity", "0")))),
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

    def close_all_positions(self) -> list[str]:
        self.cancel_all_orders()
        submitted: list[str] = []
        for position in self.positions():
            quantity = int(Decimal(str(position.get("quantity", "0"))))
            if not quantity:
                continue
            if position.get("instrument_type") == "EQUITY":
                side = "SELL" if quantity > 0 else "BUY"
                submitted.append(
                    self.place_stock(position["symbol"], side, abs(quantity))
                )
            elif position.get("instrument_type") == "OPTION":
                contract = self.contract_from_position(position)
                if not contract:
                    logging.getLogger("webull-bot").error(
                        "CLOSE  | unresolved option=%s",
                        position.get("symbol", "UNKNOWN"),
                    )
                    continue
                side = "SELL" if quantity > 0 else "BUY"
                intent = "SELL_TO_CLOSE" if quantity > 0 else "BUY_TO_CLOSE"
                quote = self.option_quote(contract["symbol"])
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
        return submitted
