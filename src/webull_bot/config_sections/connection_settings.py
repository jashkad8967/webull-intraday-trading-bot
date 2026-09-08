from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConnectionSettings(BaseSettings):
    """Webull API connection/account identity fields and connection validation."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    mode: str = Field(default="LIVE", pattern="^LIVE$")
    webull_app_key: str = ""
    webull_app_secret: str = ""
    account_id: str = ""
    webull_api_endpoint: str = "api.webull.com"
    webull_environment: str = Field(default="prod", pattern="^prod$")
    webull_region_id: str = Field(default="us", pattern="^us$")
    live_trading_enabled: bool = True
    # Phase 0 of the polling-to-streaming migration (see the plan) - a
    # read-only observer that subscribes to Webull's gRPC order/position
    # event stream (TradeEventStreamService) and only logs what it
    # receives. Default off: this opens a new authenticated network
    # connection and background thread with an SDK whose exact event
    # payload schema hasn't been confirmed from real traffic yet: no
    # trading behavior depends on it until that's verified live.
    event_stream_enabled: bool = False

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
