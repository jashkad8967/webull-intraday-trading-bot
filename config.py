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
    max_symbols: int = Field(default=0, ge=0, le=50000)
    stock_batch_size: int = Field(default=100, ge=1, le=100)
    stock_priority_fraction: float = Field(default=0.60, ge=0, le=0.90)
    option_batch_size: int = Field(default=20, ge=1, le=20)
    option_discovery_per_cycle: int = Field(default=1, ge=1, le=10)
    option_discovery_seconds: Decimal = Field(default=Decimal("15"), ge=1, le=3600)

    stock_quantity: int = Field(default=1, ge=1)
    option_quantity: int = Field(default=1, ge=1)
    max_open_positions: int = Field(default=5, ge=1)
    max_order_notional: Decimal = Field(default=Decimal("1000"), gt=0)

    poll_seconds: Decimal = Field(default=Decimal("1"), ge=Decimal("1"), le=Decimal("3600"))
    trade_cooldown_seconds: Decimal = Field(default=Decimal("5"), ge=0, le=Decimal("21600"))
    ema_fast_period: int = Field(default=3, ge=2, le=500)
    ema_slow_period: int = Field(default=8, ge=3, le=1000)
    reenter_on_trend: bool = True
    stock_take_profit_per_share: Decimal = Field(default=Decimal("0.01"), ge=0)
    stock_stop_loss_per_share: Decimal = Field(default=Decimal("0.05"), ge=0)
    option_take_profit_price: Decimal = Field(default=Decimal("0.01"), ge=0)
    option_stop_loss_price: Decimal = Field(default=Decimal("0.05"), ge=0)
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

    agent_enabled: bool = False
    groq_api_key: str = ""
    groq_model: str = "groq/compound-mini"
    agent_research_seconds: int = Field(default=235, ge=15, le=3600)
    agent_daily_request_limit: int = Field(default=245, ge=1, le=250)
    agent_max_symbols: int = Field(default=3, ge=1, le=50)
    agent_timeout_seconds: int = Field(default=60, ge=5, le=180)
    agent_required_for_entry: bool = False
    agent_min_confidence: float = Field(default=0.65, ge=0, le=1)
    agent_min_entry_score: float = Field(default=0.15, ge=-1, le=1)
    agent_max_downside_risk: float = Field(default=0.55, ge=0, le=1)
    agent_max_liquidity_risk: float = Field(default=0.50, ge=0, le=1)
    loss_circuit_breaker_enabled: bool = False
    loss_spree_position_count: int = Field(default=3, ge=2, le=100)
    loss_spree_total_dollars: Decimal = Field(default=Decimal("1"), gt=0)
    loss_spree_low_outlook_fraction: float = Field(default=0.6, ge=0, le=1)
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
    stock_limit_offset: Decimal = Field(
        default=Decimal("0.005"),
        ge=0,
        le=Decimal("0.10"),
    )
    option_limit_offset: Decimal = Field(default=Decimal("0.03"), ge=0, le=Decimal("0.25"))

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
        if self.option_min_dte > self.option_max_dte:
            raise ValueError("OPTION_MIN_DTE must not exceed OPTION_MAX_DTE")
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
        if self.loss_circuit_breaker_enabled and not self.agent_enabled:
            raise ValueError(
                "LOSS_CIRCUIT_BREAKER_ENABLED requires AGENT_ENABLED=true"
            )

    def stocks(self) -> list[str]:
        return [item.strip().upper() for item in self.stock_symbols.split(",") if item.strip()]

    def exact_options(self) -> list[str]:
        return [item.strip().upper() for item in self.option_contracts.split(",") if item.strip()]

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
