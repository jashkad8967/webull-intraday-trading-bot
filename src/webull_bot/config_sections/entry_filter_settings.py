from decimal import Decimal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class EntryFilterSettings(BaseSettings):
    """Secondary confirmation gates on top of the core EMA/SMA entry signal:
    historical/recent/multi-day volatility and momentum, RSI, and the
    volatility-scalp micro-exhaustion confirmation gate."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    historical_volatility_filter_enabled: bool = True
    historical_volatility_days: int = Field(default=20, ge=5, le=120)
    min_historical_volatility_percent: Decimal = Field(
        default=Decimal("3"),
        ge=0,
        le=100,
    )
    # Higher-timeframe SMA trend filter: only allows the fast EMA(3/8)
    # scalp signal to fire in the direction of this slower daily-bar
    # trend. Off by default - opt in once you've confirmed it fits your
    # symbol mix (a strict trend filter can meaningfully cut entry
    # frequency on a chop-heavy universe).
    sma_trend_filter_enabled: bool = True
    sma_trend_days: int = Field(default=50, ge=5, le=250)
    # By request: "look at tickers in the last 10 mins for momentum...
    # to analyze the upcoming trend." The daily SMA above already covers
    # the historical/multi-day trend, and session VWAP (see volatility_
    # scalp_vwap_supports_entry) already covers "the whole day" - this
    # fills the one genuinely missing timeframe in between: whether the
    # last RECENT_MOMENTUM_LOOKBACK_MINUTES minutes look like a normal
    # dip or an actively accelerating breakdown. Deliberately NOT "block
    # any recent decline" - this cohort exists specifically to dip-buy a
    # short-term decline, so a moderate pullback is the setup, not a
    # warning sign. Only blocks a decline steeper than RECENT_MOMENTUM_
    # MAX_DECLINE_PERCENT over the lookback window - a real, fast
    # breakdown, not routine chop.
    recent_momentum_filter_enabled: bool = True
    recent_momentum_lookback_minutes: int = Field(default=10, ge=2, le=60)
    recent_momentum_refresh_seconds: int = Field(default=120, ge=30, le=1800)
    recent_momentum_max_decline_percent: Decimal = Field(
        default=Decimal("0.05"), gt=0, le=1
    )
    # By request: a 4th, stricter confirmation gate sitting downstream
    # of volatility_scalp_dip_signal specifically (not breakout/reversal,
    # which stay untouched) - proves a dip is actual liquidity
    # exhaustion (a sharp drop, a real bounce off the floor, and a
    # volume spike that's already fading) rather than just "X% off a
    # local high." See TradingStrategy.volatility_scalp_micro_
    # exhaustion_confirmed and AutoTrader.update_recent_tick_history/
    # update_volume_delta. Zero additional API calls - built entirely
    # from data the existing scan pass already returns.
    volatility_scalp_micro_exhaustion_filter_enabled: bool = True
    # How far back (real elapsed seconds, not sample count) the local
    # high/low is measured - see recent_tick_history's own comment for
    # why this can't be a sample count in this codebase.
    volatility_scalp_micro_exhaustion_lookback_seconds: int = Field(
        default=300, ge=10, le=3600
    )
    # Required drop from the lookback window's local high to the
    # current price - a real, sharp move within the window, not routine
    # noise.
    volatility_scalp_micro_exhaustion_velocity_percent: Decimal = Field(
        default=Decimal("0.025"), gt=0, le=1
    )
    # Required recovery off the lookback window's local low, as a
    # fraction of its full high-low range - proves the price has
    # already sprung back partway before this entry, not still
    # actively falling.
    volatility_scalp_micro_exhaustion_wick_ratio: Decimal = Field(
        default=Decimal("0.40"), gt=0, le=1
    )
    # Required multiple of the smoothed baseline volume delta - a real
    # capitulation spike, not routine trading.
    volatility_scalp_micro_exhaustion_volume_multiplier: Decimal = Field(
        default=Decimal("2.5"), gt=0, le=50
    )
    # Smoothing factor for the rolling volume-delta EMA (see
    # AutoTrader.update_volume_delta) - higher reacts faster to a
    # sudden spike, lower stays steadier against routine noise.
    volatility_scalp_micro_exhaustion_volume_ema_alpha: Decimal = Field(
        default=Decimal("0.2"), gt=0, le=1
    )
    # By request: "find common instances historically of dips... known
    # patterns... use that to also decide on an entry or exit." RSI
    # (Wilder's Relative Strength Index) is the single most widely
    # documented, historically-validated way to flag a statistically
    # overextended dip or peak - see TradingStrategy.rsi_supports_
    # entry/rsi_overbought_exit. 14/30/70 are the standard, textbook
    # RSI convention, not tuned specifically for this cohort.
    rsi_filter_enabled: bool = True
    rsi_period: int = Field(default=14, ge=2, le=100)
    rsi_oversold_threshold: Decimal = Field(default=Decimal("30"), ge=0, le=100)
    rsi_overbought_threshold: Decimal = Field(default=Decimal("70"), ge=0, le=100)
    # By request: "also include not only short term patterns like 5-10
    # mins, but also 1 day and 5 day and month." recent_momentum (10
    # min) and sma_trend (50-day average) already exist - this fills
    # the explicit 1-day/5-day/~1-month timeframes named directly,
    # using real daily-bar closes (AutoTrader.refresh_multi_day_
    # momentum/WebullAPI.daily_closes), not derived from the tick
    # window. Same "block only a real, sustained decline, not routine
    # chop" philosophy as recent_momentum - deliberately more
    # permissive at longer horizons (a stock can legitimately be down
    # over a month while still being a good dip-buy today).
    multi_day_momentum_filter_enabled: bool = True
    multi_day_momentum_refresh_seconds: int = Field(
        default=1800, ge=300, le=21600
    )
    multi_day_momentum_lookback_days: int = Field(default=25, ge=6, le=90)
    multi_day_momentum_max_decline_1d: Decimal = Field(
        default=Decimal("0.15"), gt=0, le=1
    )
    multi_day_momentum_max_decline_5d: Decimal = Field(
        default=Decimal("0.30"), gt=0, le=1
    )
    multi_day_momentum_max_decline_month: Decimal = Field(
        default=Decimal("0.50"), gt=0, le=1
    )
    # Live incident (this bug): MGN was bought as a scalp "dip entry" at
    # $0.2071 while still ~74% above its prior close of ~$0.1196 - a
    # pullback off an intraday blow-off-top spike, not a real dip - and
    # kept falling, hitting the hard stop within ~2 minutes. FAMI hit
    # the same pattern minutes later (bought at 3x the prior close),
    # and together they tripped the daily loss circuit breaker. This is
    # the well-documented "don't chase/buy a stock still extended far
    # above its prior close" pattern (avoiding gap-and-crap / blow-off-
    # top setups) - a short-term pullback within a huge, still-elevated
    # intraday spike is a falling knife continuing to unwind, not a
    # stable range to mean-revert in. Reuses the same daily_closes[0]
    # "prior close" data multi_day_momentum_supports_entry already has,
    # just checking the opposite direction (extension up, not decline
    # down) - blocks a BUY when price is still more than this fraction
    # above yesterday's close, even if the immediate few minutes look
    # like a dip. Deliberately generous (50%) since real breakouts do
    # legitimately run - this targets the extreme, already-cracking
    # spike cases like MGN/FAMI (74%-280% above prior close), not
    # ordinary strength.
    multi_day_momentum_max_extension_1d: Decimal = Field(
        default=Decimal("0.50"), gt=0, le=5
    )
