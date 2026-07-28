from datetime import time
from decimal import Decimal
from functools import lru_cache
from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    mode: str = Field(default="LIVE", pattern="^LIVE$")
    webull_app_key: str = ""
    webull_app_secret: str = ""
    account_id: str = ""
    webull_api_endpoint: str = "api.webull.com"
    webull_environment: str = Field(default="prod", pattern="^prod$")
    webull_region_id: str = Field(default="us", pattern="^us$")
    live_trading_enabled: bool = True

    stock_symbols: str = "ALL"
    option_contracts: str = ""
    option_underlyings: str = "ALL"
    option_type: str = Field(default="BOTH", pattern="^(CALL|PUT|BOTH)$")
    option_min_dte: int = Field(default=7, ge=0, le=730)
    option_max_dte: int = Field(default=45, ge=0, le=730)
    max_symbols: int = Field(default=500, ge=0, le=50000)
    stock_universe_reserve: int = Field(default=250, ge=0, le=50000)
    stock_universe_page_size: int = Field(default=200, ge=25, le=1000)
    stock_batch_size: int = Field(default=100, ge=1, le=100)
    stock_priority_fraction: float = Field(default=0.70, ge=0, le=0.90)
    stock_penny_fraction: float = Field(default=0.10, ge=0, le=0.50)
    penny_stock_max_price: Decimal = Field(default=Decimal("5"), gt=0)
    exclude_etfs: bool = True
    historical_volatility_filter_enabled: bool = True
    historical_volatility_days: int = Field(default=20, ge=5, le=120)
    min_historical_volatility_percent: Decimal = Field(
        default=Decimal("3"),
        ge=0,
        le=100,
    )
    popular_stock_symbols: str = (
        "NVDA,TSLA,AMD,AAPL,AMZN,META,MSFT,GOOGL,NFLX,AVGO,"
        "COIN,PLTR,MSTR,HOOD,SOFI,RIVN,GME,AMC,NIO,BABA,F,SNAP,UBER,"
        "MARA,IONQ,RGTI,QBTS,QUBT,FCX"
    )
    popular_stock_min_volume: int = Field(default=1_000_000, ge=0)
    popular_stock_max_spread_percent: Decimal = Field(
        default=Decimal("0.50"),
        ge=0,
        le=Decimal("10"),
    )
    stock_popular_capital_fraction: Decimal = Field(
        default=Decimal("0.70"),
        ge=0,
        le=1,
    )
    stock_penny_capital_fraction: Decimal = Field(
        default=Decimal("0.10"),
        ge=0,
        le=1,
    )
    stock_discovery_capital_fraction: Decimal = Field(
        default=Decimal("0.20"),
        ge=0,
        le=1,
    )
    option_batch_size: int = Field(default=20, ge=1, le=20)
    option_discovery_per_cycle: int = Field(default=1, ge=1, le=10)
    option_discovery_seconds: Decimal = Field(default=Decimal("15"), ge=1, le=3600)

    stock_quantity: int = Field(default=1, ge=1)
    option_quantity: int = Field(default=1, ge=1)
    max_open_positions: int = Field(default=5, ge=1)
    max_order_notional: Decimal = Field(default=Decimal("1000"), gt=0)

    poll_seconds: Decimal = Field(default=Decimal("1"), ge=Decimal("1"), le=Decimal("3600"))
    trade_cooldown_seconds: Decimal = Field(default=Decimal("30"), ge=0, le=Decimal("21600"))
    stock_max_trades_per_hour: int = Field(default=8, ge=0, le=1000)
    ema_fast_period: int = Field(default=3, ge=2, le=500)
    ema_slow_period: int = Field(default=8, ge=3, le=1000)
    reenter_on_trend: bool = True
    reenter_confirmation_polls: int = Field(default=2, ge=1, le=20)
    vwap_entry_band_percent: Decimal = Field(
        default=Decimal("0.001"),
        ge=0,
        le=Decimal("0.05"),
    )
    stock_min_net_profit_percent: Decimal = Field(
        default=Decimal("0.0015"),
        ge=0,
        le=1,
    )
    stock_estimated_round_trip_cost_percent: Decimal = Field(
        default=Decimal("0.002"),
        ge=0,
        le=Decimal("0.10"),
    )
    stock_stop_loss_min_percent: Decimal = Field(default=Decimal("0.0015"), gt=0, le=1)
    stock_stop_loss_max_percent: Decimal = Field(default=Decimal("0.006"), gt=0, le=1)
    stock_stop_loss_range_multiplier: Decimal = Field(
        default=Decimal("0.35"),
        ge=0,
        le=Decimal("5"),
    )
    stock_target_stop_multiple: Decimal = Field(
        default=Decimal("1.2"),
        ge=Decimal("0.5"),
        le=Decimal("5"),
    )
    stock_entry_max_spread_percent: Decimal = Field(
        default=Decimal("0.15"),
        gt=0,
        le=Decimal("5"),
    )
    option_take_profit_price: Decimal = Field(default=Decimal("0.01"), ge=0)
    market_requests_per_minute: int = Field(default=240, ge=1, le=300)
    option_instrument_requests_per_minute: int = Field(default=45, ge=1, le=60)
    stock_instrument_requests_per_30_seconds: int = Field(default=9, ge=1, le=10)
    account_requests_per_second: Decimal = Field(
        default=Decimal("0.8"),
        gt=0,
        le=Decimal("1"),
    )
    order_requests_per_minute: int = Field(default=480, ge=1, le=600)
    account_refresh_seconds: Decimal = Field(default=Decimal("5"), ge=1, le=60)
    order_timeout_seconds: int = Field(default=120, ge=15, le=3600)
    order_monitor_seconds: Decimal = Field(default=Decimal("5"), ge=1, le=60)
    stall_breaker_enabled: bool = True
    stall_breaker_seconds: int = Field(default=120, ge=15, le=3600)
    stall_breaker_min_profit: Decimal = Field(
        default=Decimal("0.01"),
        gt=0,
        le=Decimal("10"),
    )

    agent_enabled: bool = False
    groq_api_key: str = ""
    groq_model: str = "groq/compound-mini"
    agent_core_research_seconds: int = Field(default=120, ge=15, le=3600)
    agent_extended_research_seconds: int = Field(default=622, ge=15, le=3600)
    agent_daily_request_limit: int = Field(default=250, ge=1, le=250)
    agent_max_symbols: int = Field(default=5, ge=1, le=50)
    agent_discovery_max_symbols: int = Field(default=5, ge=1, le=25)
    agent_timeout_seconds: int = Field(default=60, ge=5, le=180)
    agent_exit_influence_enabled: bool = True
    agent_exit_min_confidence: Decimal = Field(
        default=Decimal("0.60"),
        ge=0,
        le=1,
    )
    agent_runner_bias_threshold: Decimal = Field(
        default=Decimal("0.50"),
        ge=0,
        le=1,
    )
    agent_runner_profit_percent: Decimal = Field(
        default=Decimal("0.01"),
        ge=0,
        le=Decimal("0.50"),
    )
    agent_derisk_bias_threshold: Decimal = Field(
        default=Decimal("-0.50"),
        ge=-1,
        le=0,
    )
    loss_circuit_breaker_enabled: bool = False
    loss_spree_position_count: int = Field(default=3, ge=2, le=100)
    loss_spree_total_dollars: Decimal = Field(default=Decimal("1"), gt=0)
    loss_reevaluation_seconds: int = Field(default=120, ge=30, le=3600)

    trading_timezone: str = "America/New_York"
    market_open_time: str = "04:00"
    eod_close_time: str = "19:50"
    market_close_time: str = "20:00"
    option_market_open_time: str = "09:30"
    option_eod_close_time: str = "15:50"
    option_market_close_time: str = "16:00"
    eod_retry_seconds: int = Field(default=10, ge=2, le=120)
    market_holidays: str = ""
    wash_sale_block_days: int = Field(default=60, ge=31, le=365)
    wash_sale_state_file: str = "conf/wash_sale_blocks.json"
    invalid_symbol_state_file: str = "conf/invalid_symbols.json"
    stock_limit_offset: Decimal = Field(
        default=Decimal("0.005"),
        ge=0,
        le=Decimal("0.10"),
    )
    option_limit_offset: Decimal = Field(default=Decimal("0.03"), ge=0, le=Decimal("0.25"))
    log_directory: str = "logs"
    status_file: str = "status.json"

    def host(self) -> str:
        value = self.webull_api_endpoint.strip()
        parsed = urlparse(value if "://" in value else f"https://{value}")
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/"):
            raise ValueError("WEBULL_API_ENDPOINT must be an HTTPS host without a path")
        return parsed.hostname

    def validate_connection(self, require_account: bool = True) -> None:
        if not self.webull_app_key or not self.webull_app_secret:
            raise ValueError("WEBULL_APP_KEY and WEBULL_APP_SECRET are required")
        if require_account and not self.account_id:
            raise ValueError("ACCOUNT_ID is required")
        if self.host() != "api.webull.com":
            raise ValueError("Production mode requires WEBULL_API_ENDPOINT=api.webull.com")
        if self.webull_environment != "prod":
            raise ValueError("Production mode requires WEBULL_ENVIRONMENT=prod")
        if self.webull_region_id != "us":
            raise ValueError("This application requires WEBULL_REGION_ID=us")

    def validate_runtime(self) -> None:
        self.validate_connection(require_account=True)
        if self.ema_fast_period >= self.ema_slow_period:
            raise ValueError("EMA_FAST_PERIOD must be lower than EMA_SLOW_PERIOD")
        if self.stock_stop_loss_min_percent > self.stock_stop_loss_max_percent:
            raise ValueError(
                "STOCK_STOP_LOSS_MIN_PERCENT must not exceed STOCK_STOP_LOSS_MAX_PERCENT"
            )
        if self.option_min_dte > self.option_max_dte:
            raise ValueError("OPTION_MIN_DTE must not exceed OPTION_MAX_DTE")
        if self.stock_priority_fraction + self.stock_penny_fraction > 0.90:
            raise ValueError(
                "STOCK_PRIORITY_FRACTION + STOCK_PENNY_FRACTION must be <= 0.90"
            )
        capital_total = (
            self.stock_popular_capital_fraction
            + self.stock_penny_capital_fraction
            + self.stock_discovery_capital_fraction
        )
        if capital_total != Decimal("1"):
            raise ValueError(
                "Stock capital fractions must add up to exactly 1.0"
            )
        if not (
            self.session_time(self.market_open_time)
            < self.session_time(self.eod_close_time)
            < self.session_time(self.market_close_time)
        ):
            raise ValueError(
                "Stock session times must be ordered: MARKET_OPEN_TIME, "
                "EOD_CLOSE_TIME, MARKET_CLOSE_TIME"
            )
        if not (
            self.session_time(self.option_market_open_time)
            < self.session_time(self.option_eod_close_time)
            < self.session_time(self.option_market_close_time)
        ):
            raise ValueError(
                "Option session times must be ordered: OPTION_MARKET_OPEN_TIME, "
                "OPTION_EOD_CLOSE_TIME, OPTION_MARKET_CLOSE_TIME"
            )
        if not self.live_trading_enabled:
            raise ValueError("Production mode requires LIVE_TRADING_ENABLED=true")
        if self.agent_enabled and not self.groq_api_key:
            raise ValueError("GROQ_API_KEY is required when AGENT_ENABLED=true")

    def stocks(self) -> list[str]:
        return [item.strip().upper() for item in self.stock_symbols.split(",") if item.strip()]

    def stock_universe_limit(self) -> int:
        return self.max_symbols or 500

    def stock_universe_pool(self) -> int:
        return self.stock_universe_limit() + self.stock_universe_reserve

    def exact_options(self) -> list[str]:
        return [item.strip().upper() for item in self.option_contracts.split(",") if item.strip()]

    def popular_stocks(self) -> list[str]:
        return [
            item.strip().upper()
            for item in self.popular_stock_symbols.split(",")
            if item.strip()
        ]

    def stock_capital_fractions(self) -> dict[str, Decimal]:
        return {
            "POPULAR": self.stock_popular_capital_fraction,
            "PENNY": self.stock_penny_capital_fraction,
            "DISCOVERY": self.stock_discovery_capital_fraction,
        }

    def stock_bucket_slot_limits(self) -> dict[str, int]:
        fractions = self.stock_capital_fractions()
        buckets = list(fractions)
        limits = {bucket: 0 for bucket in buckets}
        remaining = self.max_open_positions
        if remaining >= len(buckets):
            for bucket in buckets:
                if fractions[bucket] > 0:
                    limits[bucket] = 1
                    remaining -= 1
        while remaining > 0:
            bucket = max(
                buckets,
                key=lambda item: (
                    float(fractions[item])
                    / max(1, limits[item]),
                    float(fractions[item]),
                ),
            )
            limits[bucket] += 1
            remaining -= 1
        return limits

    def option_roots(self) -> list[str]:
        return [item.strip().upper() for item in self.option_underlyings.split(",") if item.strip()]

    def holidays(self) -> set[str]:
        return {item.strip() for item in self.market_holidays.split(",") if item.strip()}

    def session_time(self, value: str) -> time:
        hour, minute = (int(part) for part in value.split(":"))
        return time(hour, minute)


@lru_cache
def settings() -> Settings:
    return Settings()
