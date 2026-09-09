from decimal import Decimal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class EntryTimingSettings(BaseSettings):
    """Trade-frequency cadence (poll interval, cooldowns, per-hour cap),
    priority-score ranking inputs (oscillation, most-active, analyst
    consensus), the EMA crossover period, and the tick-direction/VWAP
    entry confirmation signals."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    poll_seconds: Decimal = Field(
        default=Decimal("0.25"), ge=Decimal("0.25"), le=Decimal("3600")
    )
    trade_cooldown_seconds: Decimal = Field(default=Decimal("0"), ge=0, le=Decimal("21600"))
    # TRADE_COOLDOWN_SECONDS is just an order-submission debounce (avoids
    # resubmitting within seconds of the last order); this is a separate,
    # much longer gate specifically on re-entering a symbol right after a
    # position in it just closed (profit, stop, or manual sell) - it
    # doesn't apply to the exit itself, only to the next BUY. Not 0: with
    # no cooldown at all, a symbol that just stopped out could be bought
    # right back into the same whipsaw on the very next scan.
    stock_reentry_cooldown_seconds: Decimal = Field(
        default=Decimal("180"), ge=0, le=Decimal("21600")
    )
    stock_max_trades_per_hour: int = Field(default=0, ge=0, le=1000)
    stock_oscillation_weight: Decimal = Field(
        default=Decimal("0.5"),
        ge=0,
        le=Decimal("5"),
    )
    # Flat priority_score bonus for a symbol currently on Webull's
    # most-active screener (see AutoTrader.refresh_market_pulse and
    # TradingStrategy.most_active_symbols) - most-active names see the
    # heaviest order flow and tend to produce the most (and fastest)
    # scalp setups, so this pushes them to the front of the scan batch
    # instead of competing on equal footing with everything else in
    # priority_score. Scaled against that function's typical range (a
    # strong oscillation bonus alone tops out around 10, a strong
    # research-assisted score around 25-30) - large enough to reliably
    # win ties, not so large it drowns out a genuinely bad setup's low
    # research/activity score.
    most_active_priority_bonus: Decimal = Field(
        default=Decimal("15"),
        ge=0,
        le=Decimal("100"),
    )
    # Soft, two-sided priority_score nudge from analyst target price/rating
    # consensus (see TradingStrategy.analyst_priority_bonus and
    # AnalystDataService) - re-ranks candidates that already cleared every
    # other gate, never blocks or forces an entry on its own. Scaled well
    # below MOST_ACTIVE_PRIORITY_BONUS: real-time order flow (most-active)
    # is a much stronger scalp signal than a slow-moving analyst consensus,
    # so this should nudge ties, not override activity-driven ranking.
    analyst_priority_enabled: bool = True
    analyst_priority_bonus_max: Decimal = Field(
        default=Decimal("5"),
        ge=0,
        le=Decimal("50"),
    )
    # How long a symbol's fetched target price/rating is trusted before
    # AnalystDataService fetches it again - deliberately long. Analyst
    # revisions are a same-day-rare event, not an intraday one, and this
    # also bounds how often the two-call Webull fundamentals lookup runs
    # per symbol against the shared STOCK_INSTRUMENT rate bucket.
    analyst_data_cache_seconds: int = Field(
        default=43200, ge=300, le=604800
    )
    ema_fast_period: int = Field(default=3, ge=2, le=500)
    ema_slow_period: int = Field(default=8, ge=3, le=1000)
    reenter_on_trend: bool = True
    reenter_confirmation_polls: int = Field(default=2, ge=1, le=20)
    # Proxy for order-flow imbalance: real bid/ask depth isn't available
    # from the quote feed (Level 1 snapshot only - no size/depth), so this
    # approximates it from the same poll-to-poll price prints already
    # collected for the EMA crossover, counting net upticks vs downticks.
    # An EMA crossover can fire while the last several individual prints
    # are still net negative; this is a real, if noisy, extra confirmation
    # that recent tape direction agrees before entering.
    tick_direction_enabled: bool = True
    tick_direction_window: int = Field(default=10, ge=3, le=100)
    tick_direction_veto_threshold: Decimal = Field(
        default=Decimal("0"), ge=-1, le=1
    )
    vwap_entry_band_percent: Decimal = Field(
        default=Decimal("0.001"),
        ge=0,
        le=Decimal("0.05"),
    )
