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
    option_batch_size: int = Field(default=20, ge=1, le=20)
    option_discovery_per_cycle: int = Field(default=1, ge=1, le=10)

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

    trading_timezone: str = "America/New_York"
    market_open_time: str = "09:30"
    eod_close_time: str = "15:50"
    market_close_time: str = "16:00"
    eod_retry_seconds: int = Field(default=10, ge=2, le=120)
    market_holidays: str = ""
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
        if not self.live_trading_enabled:
            raise ValueError("Production mode requires LIVE_TRADING_ENABLED=true")

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
