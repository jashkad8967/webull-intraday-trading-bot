from decimal import Decimal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RiskCircuitBreakerSettings(BaseSettings):
    """Account-wide and per-symbol circuit breakers: the daily equity-loss
    halt, the loss-spree dollar breaker, the freqtrade-style stop-loss
    guard and per-symbol quarantine, the manual-touch pause, the
    time-aware post-entry stop widening, and the VIXY regime gate."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Was flipped to enabled-by-default earlier this session ("make the
    # daily circuit breaker real"), and it did trip live - correctly,
    # by its own math (5% of equity) - the same day the MGN/FAMI spike-
    # chasing bug (see multi_day_momentum_max_extension_1d) produced
    # the losses that tripped it. By explicit request afterward ("we do
    # not want the circuit breaker to stop all trading"): disabled
    # again. The root-cause fix (the extension guard above) addresses
    # the actual bad entries directly; this halt-everything-for-the-
    # rest-of-the-day mechanism was the wrong lever for that problem.
    daily_loss_circuit_breaker_enabled: bool = False
    # Picked at the upper/more-permissive end of the 3-5% convention
    # given this account's dual purpose (real capital to grow, but also
    # a learning testbed where informative losses have value) - not the
    # tightest possible setting, a deliberate, documented choice.
    daily_max_loss_fraction: Decimal = Field(
        default=Decimal("0.05"), gt=0, le=1
    )
    loss_circuit_breaker_enabled: bool = False
    loss_spree_position_count: int = Field(default=3, ge=2, le=100)
    loss_spree_total_dollars: Decimal = Field(default=Decimal("1"), gt=0)
    loss_reevaluation_seconds: int = Field(default=120, ge=30, le=3600)
    # freqtrade-style StoplossGuard (see AutoTrader.stop_loss_guard_active) -
    # a frequency-based circuit breaker, distinct from the dollar/equity
    # breakers above: pauses NEW entries only (never liquidates) once
    # STOP_LOSS_GUARD_TRADE_LIMIT stops have fired within the trailing
    # STOP_LOSS_GUARD_LOOKBACK_SECONDS window, for STOP_LOSS_GUARD_
    # COOLDOWN_SECONDS, then resumes automatically.
    stop_loss_guard_enabled: bool = True
    stop_loss_guard_trade_limit: int = Field(default=4, ge=1, le=50)
    stop_loss_guard_lookback_seconds: int = Field(default=1200, ge=60, le=21600)
    stop_loss_guard_cooldown_seconds: int = Field(default=600, ge=60, le=21600)
    # freqtrade-style LowProfitPairs (see AutoTrader.symbol_quarantined) - the
    # same idea as the stop-loss guard above but scoped per-symbol instead of
    # account-wide: once a symbol's own realized P&L over the trailing
    # SYMBOL_QUARANTINE_LOOKBACK_SECONDS window falls at or below
    # -SYMBOL_QUARANTINE_LOSS_DOLLARS (and it's had at least SYMBOL_
    # QUARANTINE_MIN_TRADES exits in that window), new entries on that one
    # symbol pause for SYMBOL_QUARANTINE_COOLDOWN_SECONDS while every other
    # symbol keeps trading normally.
    symbol_quarantine_enabled: bool = False
    symbol_quarantine_lookback_seconds: int = Field(default=1800, ge=60, le=21600)
    symbol_quarantine_min_trades: int = Field(default=3, ge=1, le=50)
    symbol_quarantine_loss_dollars: Decimal = Field(default=Decimal("0.50"), gt=0)
    symbol_quarantine_cooldown_seconds: int = Field(default=900, ge=60, le=21600)
    # By request: "when i touch a stock stop doing anything with it
    # while i am there." A manual action (a real order placed directly
    # in the Webull app, detected by monitor_working_orders, OR a
    # dashboard manual buy/sell - both already recorded as MANUAL_BUY/
    # MANUAL_SELL via record_trade) stamps a per-symbol timestamp; every
    # automated action on that symbol - fresh entries, averaging down,
    # repricing, exits, escalation - pauses for this many seconds
    # afterward, treated as a proxy for "the user is actively there."
    # Deliberately pauses PROTECTIVE exits too, not just new entries -
    # "stop doing anything" was explicit, and the window is bounded
    # (5 minutes by default), not an indefinite hands-off.
    manual_touch_pause_seconds: int = Field(default=300, ge=0, le=3600)
    # Widens the stop immediately after entry (avoids getting shaken out by
    # quote noise right at fill) then tightens back to adaptive_stop_percent's
    # normal value as the position ages - see AutoTrader.position_opened_at
    # and TradingStrategy.time_aware_stop_multiplier. On: a live incident
    # showed several positions (PFSA, XOS, WFF) stopped out 19s-7min after
    # entry, well within ordinary tick-to-tick noise for a low-priced
    # stock at this account's tight (0.9-1.5%) base stop - this widens
    # that window right when a fresh entry needs it most instead of
    # cutting it before the setup has any real chance to play out.
    time_aware_stop_enabled: bool = True
    time_aware_stop_widen_seconds: int = Field(default=60, ge=1, le=3600)
    time_aware_stop_widen_multiplier: Decimal = Field(
        default=Decimal("1.5"), ge=Decimal("1"), le=Decimal("5")
    )
    # Generalizes option_market_regime_ok's VIXY-rolling-percentile gate to
    # stock entries too (see AutoTrader.trade_stocks) - rejects a fresh EMA
    # cross when VIXY is spiking into the top of its own recent range,
    # regardless of how good that one symbol's own setup looks.
    regime_gate_enabled: bool = False
    regime_gate_reject_percentile: Decimal = Field(
        default=Decimal("0.85"), gt=0, le=1
    )
