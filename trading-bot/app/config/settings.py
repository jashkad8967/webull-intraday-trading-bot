from decimal import Decimal
from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    mode: str = Field(default="PAPER", pattern="^(PAPER|LIVE)$")
    webull_app_key: str = ""
    webull_app_secret: str = ""
    account_id: str = ""
    webull_api_endpoint: str = "https://api.sandbox.webull.com"
    webull_data_endpoint: str = "data-api.webull.com"
    webull_environment: str = "us"
    database_url: str = "sqlite:///./data/trading.db"
    max_risk_per_trade: Decimal = Decimal("0.01")
    max_daily_loss: Decimal = Decimal("0.03")
    max_open_positions: int = 5
    max_buying_power_fraction: Decimal = Decimal("0.80")
    paper_starting_cash: Decimal = Decimal("100000")
    eod_flatten_time: str = "15:55"
    trading_timezone: str = "America/New_York"
    options_enabled: bool = False
    def live_ready(self) -> bool:
        return self.mode == "LIVE" and bool(self.account_id and self.webull_app_key and self.webull_app_secret)
@lru_cache
def get_settings() -> Settings:
    return Settings()
