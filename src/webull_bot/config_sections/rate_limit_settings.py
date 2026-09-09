from decimal import Decimal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RateLimitSettings(BaseSettings):
    """Per-endpoint Webull API request-rate ceilings (market data, option
    and stock instrument lookups, account, orders)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    market_requests_per_minute: int = Field(default=240, ge=1, le=300)
    # By request: "make sure all order placements, information
    # gathering is very quick, within a second" - this governs new
    # option CONTRACT LISTING lookups (discover_option_contracts),
    # not order placement or managing existing positions (both
    # already sub-second via the "order"/"market" groups below).
    # Raised to this field's own ceiling (1.0s spacing, from the
    # previous 45/min ~1.33s) - the user explicitly chose to push to
    # this real limit over staying at the more conservative default,
    # after being told this is the field's own max (unverified live
    # at exactly this rate; back off if 429s appear on this endpoint).
    option_instrument_requests_per_minute: int = Field(default=60, ge=1, le=60)
    stock_instrument_requests_per_30_seconds: int = Field(default=9, ge=1, le=10)
    account_requests_per_second: Decimal = Field(
        default=Decimal("0.8"),
        gt=0,
        le=Decimal("1"),
    )
    order_requests_per_minute: int = Field(default=480, ge=1, le=600)
