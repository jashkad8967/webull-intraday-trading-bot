from decimal import Decimal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class OrderExecutionSettings(BaseSettings):
    """Stalled-order escalation/market-fallback, the stop-loss single-tick
    confirmation debounce, order-history reconciliation, the stall-breaker
    sweep, and limit-order price offsets."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    stop_loss_escalate_seconds: int = Field(default=15, ge=5, le=120)
    # Live incident: CTRM resubmitted the same never-fillable PROFIT limit
    # order for 3+ hours (40+ attempts) - escalate_stalled_stop_losses is
    # meant to prevent exactly this, but a symbol whose escalated order
    # also never fills (or whose escalation itself doesn't fire) has no
    # other backstop. After this many consecutive never-filled exit
    # attempts for one symbol (see AutoTrader.consecutive_exit_failures),
    # the next one forces a genuine MARKET order instead of another
    # limit - guaranteed to fill and end the loop, rather than hoping a
    # better price eventually clears.
    consecutive_exit_failure_market_threshold: int = Field(
        default=3, ge=1, le=20
    )
    # Live complaint: a position dips through its stop on a single noisy
    # print (the bot polls every POLL_INTERVAL_SECONDS, as fast as 0.25s)
    # and gets sold at the exact worst tick, then recovers moments later.
    # stock_decision still detects a stop breach the instant it happens
    # (unchanged - a real fast decline must still be caught quickly), but
    # AutoTrader now waits for price to stay at/below the stop level for
    # this long, continuously, before actually submitting the exit. Any
    # recovery above the stop level resets the timer. Short by design -
    # this filters out single-tick wicks without meaningfully slowing
    # down protection against a genuine move.
    stop_loss_confirmation_enabled: bool = True
    stop_loss_confirmation_seconds: Decimal = Field(
        default=Decimal("2"), ge=0, le=30
    )
    # Log-only audit: how often AutoTrader.reconcile_order_history cross-
    # checks today's Webull order history against every order_id the bot
    # itself submitted today. An order in Webull's history the bot never
    # recorded is very likely a manual action taken directly in the
    # Webull app - this never changes any bot state (position sizing,
    # pnl, gates), purely a visibility signal logged once per unrecognized
    # order per day.
    order_history_reconcile_enabled: bool = True
    order_history_reconcile_seconds: int = Field(
        default=1800, ge=60, le=86400
    )
    account_refresh_seconds: Decimal = Field(default=Decimal("1"), ge=1, le=60)
    order_timeout_seconds: int = Field(default=120, ge=15, le=3600)
    order_monitor_seconds: Decimal = Field(default=Decimal("5"), ge=1, le=60)
    stall_breaker_enabled: bool = True
    stall_breaker_seconds: int = Field(default=120, ge=15, le=3600)
    stall_breaker_min_profit: Decimal = Field(
        default=Decimal("0.01"),
        gt=0,
        le=Decimal("10"),
    )
    stock_limit_offset: Decimal = Field(
        default=Decimal("0.005"),
        ge=0,
        le=Decimal("0.10"),
    )
    option_limit_offset: Decimal = Field(default=Decimal("0.03"), ge=0, le=Decimal("0.25"))
