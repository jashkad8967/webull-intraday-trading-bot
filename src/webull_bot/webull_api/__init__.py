import logging
import threading
import time

from webull_bot.api.errors import MarketDataPermissionError, QuoteUnavailableError
from webull_bot.api.options import (
    _option_greek_field,
    contract_from_position,
    exact_option,
    option_contracts,
    option_delta,
    option_implied_vol,
    option_position,
    option_quantity,
    option_volume,
    place_option,
    resolve_options,
    select_atm_options,
)
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
from webull_bot.api.history_bars import (
    _average_amplitude,
    _average_close,
    _extract_bars,
    _history_bars_chunks_concurrently,
    _history_bars_resilient,
    _parse_amplitudes,
    _parse_closes,
    daily_closes,
    historical_volatility,
    recent_minute_closes,
    sma_trend,
)
from webull_bot.api.analyst_data import (
    analyst_rating,
    analyst_target_price,
)
from webull_bot.api.market_data import (
    _stock_instruments_resilient,
    stock_categories,
    stock_universe,
)
from webull_bot.api.screeners import (
    _page_screener,
    _screener_number,
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

    option_contracts = option_contracts
    analyst_target_price = analyst_target_price
    analyst_rating = analyst_rating
    exact_option = exact_option
    select_atm_options = select_atm_options
    resolve_options = resolve_options

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

    option_delta = option_delta
    option_implied_vol = option_implied_vol
    option_volume = option_volume
    _option_greek_field = _option_greek_field

    quote_bid = quote_bid
    quote_ask = quote_ask
    quote_price = staticmethod(quote_price)

    place_option = place_option
    stock_quantity = staticmethod(stock_quantity)
    option_quantity = staticmethod(option_quantity)
    stock_position = staticmethod(stock_position)
    option_position = option_position
    contract_from_position = contract_from_position

    close_all_positions = close_all_positions
