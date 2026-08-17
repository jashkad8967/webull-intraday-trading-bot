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
    option_underlyings: str = ""
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
    # Higher-timeframe SMA trend filter: only allows the fast EMA(3/8)
    # scalp signal to fire in the direction of this slower daily-bar
    # trend. Off by default - opt in once you've confirmed it fits your
    # symbol mix (a strict trend filter can meaningfully cut entry
    # frequency on a chop-heavy universe).
    sma_trend_filter_enabled: bool = True
    sma_trend_days: int = Field(default=50, ge=5, le=250)
    # Directional short-selling in the main EMA/SMA stock strategy - a
    # fresh bearish EMA cross opens a short instead of just being skipped.
    # Off by default: the account needs margin/short approval, and Webull
    # rejections surface naturally (see is_broker_position_conflict-style
    # handling in bot.py) rather than being pre-checked here. Shorts always
    # flatten same-day regardless of OVERNIGHT_HOLD_ENABLED - overnight
    # gap/squeeze risk on a short is asymmetric (unbounded loss) unlike a
    # long's overnight risk.
    short_selling_enabled: bool = True
    popular_stock_symbols: str = (
        "NVDA,TSLA,AMD,AAPL,AMZN,META,MSFT,GOOGL,NFLX,AVGO,"
        "COIN,PLTR,MSTR,HOOD,SOFI,RIVN,GME,AMC,NIO,BABA,F,SNAP,UBER,"
        "MARA,IONQ,RGTI,QBTS,QUBT,FCX"
    )
    # Always present in the dashboard watchlist from the moment the bot
    # starts, every run - unlike POPULAR_STOCK_SYMBOLS (which only weights
    # priority within the scanned universe), these are seeded directly into
    # user_watchlist so a restart never loses them and the user never has to
    # re-add them from the dashboard.
    default_watchlist_symbols: str = (
        "AAPL,F,NVDA,MSFT,AMZN,NFLX,TSLA,NIO,ADBE,XPEV,OXY,NOW,XOM,DDOG,OPTT,"
        "FCEL,CVX,NET,FEMY,CRM,CHPT,KO,AMC,ABNB,BNKK,SNAP,DASH,SBUX,HWH,LCID,"
        "SIRI,BNGO,NVO,SPCX,SPCE,SHOP,GM,RIVN,IBM,ZM,NEGG,NVGS,ZVRA,V,MRNA,T,"
        "PYPL,PPSI,DIS,JNJ,COST,ROKU,SPYD,SPY,UBER,COIN,XYZ,WMT,DFLI,PLTR,PFE,"
        "UNH,TBB,VZ,BABA,SRPT,MARA,AVGO,GOOGL,GOOG,BB,FDX,BRK-A,GNW,OPAD,SPX,"
        "ACB,ORCL,IXIC,META,QQQ,RBLX,UPS,QCOM,AAL,CCX,SKYA,CRWD,CTRM,NKE,OCGN,"
        "SNDA,WWR,INTC,HD,NIU,GME,DIA,ATOS,BAD,DKNG,UAL,MOBX,HOOD,DAL,CLOV,"
        "RKLB,TSM,MU,JPM,AMD,NOK,BA,RIOT,TLRY,SOFI,CENN"
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
    top_gainers_limit: int = Field(default=200, ge=0, le=5000)
    fractional_shares_enabled: bool = True
    fractional_shares_min_notional: Decimal = Field(default=Decimal("25"), ge=Decimal("5"))
    option_batch_size: int = Field(default=20, ge=1, le=20)
    option_discovery_per_cycle: int = Field(default=1, ge=1, le=10)
    option_discovery_seconds: Decimal = Field(default=Decimal("15"), ge=1, le=3600)

    stock_quantity: int = Field(default=1, ge=1)
    option_quantity: int = Field(default=1, ge=1)
    max_open_positions: int = Field(default=50, ge=1)
    max_order_notional: Decimal = Field(default=Decimal("1000"), gt=0)

    poll_seconds: Decimal = Field(
        default=Decimal("0.25"), ge=Decimal("0.25"), le=Decimal("3600")
    )
    trade_cooldown_seconds: Decimal = Field(default=Decimal("0"), ge=0, le=Decimal("21600"))
    # TRADE_COOLDOWN_SECONDS is just an order-submission debounce (avoids
    # resubmitting within seconds of the last order); this is a separate,
    # much longer gate specifically on re-entering a symbol right after a
    # position in it just closed (profit, stop, or manual sell) - it
    # doesn't apply to the exit itself, only to the next BUY.
    stock_reentry_cooldown_seconds: Decimal = Field(
        default=Decimal("0"), ge=0, le=Decimal("21600")
    )
    stock_max_trades_per_hour: int = Field(default=0, ge=0, le=1000)
    stock_oscillation_weight: Decimal = Field(
        default=Decimal("0.5"),
        ge=0,
        le=Decimal("5"),
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
    # The flat regulatory pass-through fee Webull charges on the sell leg
    # only (SEC fee + FINRA TAF, rounded up to whole cents) - never charged
    # on the buy. It scales slightly with trade size (larger notional can
    # round up to 3 cents instead of 2), but a flat estimate is close enough
    # to make sure every realized P&L figure - dashboard, daily loss
    # breaker, trade log - reflects a real cost instead of assuming a free
    # round trip. Also folded into every profit target (stock, option) so a
    # target isn't hit at a price that nets a loss once this fee comes out.
    sell_fee_dollars: Decimal = Field(default=Decimal("0.02"), ge=0)
    stock_stop_loss_min_percent: Decimal = Field(default=Decimal("0.009"), gt=0, le=1)
    stock_stop_loss_max_percent: Decimal = Field(default=Decimal("0.015"), gt=0, le=1)
    stock_stop_loss_range_multiplier: Decimal = Field(
        default=Decimal("0.35"),
        ge=0,
        le=Decimal("5"),
    )
    stock_target_stop_multiple: Decimal = Field(
        default=Decimal("1.8"),
        ge=Decimal("0.5"),
        le=Decimal("5"),
    )
    stock_entry_max_spread_percent: Decimal = Field(
        default=Decimal("0.50"),
        gt=0,
        le=Decimal("5"),
    )
    stock_entry_max_extension_percent: Decimal = Field(
        default=Decimal("0.01"),
        ge=0,
        le=Decimal("0.20"),
    )
    # During core trading hours, size new stock entries as this fraction of
    # total account buying power (a genuinely fractional/decimal quantity),
    # instead of the fixed STOCK_QUANTITY whole-share sizing - see
    # dollar_stock_quantity() and README "Fractional shares". ge=0
    # deliberately (not gt=0): 0 is the escape hatch back to fixed-quantity
    # sizing all day, same pattern as OPENING_GRACE_MINUTES=0.
    #
    # This and stock_whole_share_core_session_fraction below are sized to
    # sum to 1.0 (not less) - together they're the entire per-cycle entry
    # budget, not an extra throttle beneath MIN_CASH_RESERVE_DOLLARS. A
    # smaller sum here would leave qualifying candidates unfunded (and cash
    # sitting idle) even when MIN_CASH_RESERVE_DOLLARS' floor has plenty of
    # room left above it.
    stock_core_session_position_fraction: Decimal = Field(
        default=Decimal("0.30"),
        ge=0,
        le=1,
    )
    # Webull only allows fractional trading as a MARKET order during core
    # hours (see "Fractional shares" above), so during core hours capital
    # splits between two independent per-cycle budgets instead of one
    # entry style taking every candidate: STOCK_CORE_SESSION_POSITION_FRACTION
    # above for fractional dollar-sized entries, and this fraction of
    # buying power for ordinary whole-share entries running alongside it.
    # Outside core hours this cap doesn't apply at all - fractional isn't
    # usable then anyway, so whole-share sizing spends against the full
    # remaining entry budget instead of this slice of it.
    stock_whole_share_core_session_fraction: Decimal = Field(
        default=Decimal("0.70"),
        ge=0,
        le=1,
    )
    # Hard floor on idle cash: buying_power is reduced by this amount
    # before any sizing math runs each cycle (see AutoTrader.run()), so
    # nothing downstream - stock/option/pairs entries, manual dashboard
    # buys - ever plans to spend into the last MIN_CASH_RESERVE_DOLLARS of
    # the account. This bounds what the bot is willing to risk spending;
    # see idle_cash_relaxation_enabled below for how the bot tries to
    # actually keep spending down to this floor, not just permit it.
    min_cash_reserve_dollars: Decimal = Field(default=Decimal("10"), ge=0)
    # Keeping cash deployed outranks entry quality, but not entirely -
    # entry gates only progressively loosen the longer cash sits above
    # MIN_CASH_RESERVE_DOLLARS with nothing bought (see
    # AutoTrader.idle_cash_ramp_progress), snapping back to full strictness
    # the moment any entry fires. This never touches the directional
    # EMA/SMA signal itself (still no trade without a real crossover) -
    # only the secondary confirmation gates: max spread, extension from
    # today's high/low, VWAP band, and the tick-direction veto threshold.
    idle_cash_relaxation_enabled: bool = True
    # No relaxation at all for this long after the last entry - a burst of
    # trading shouldn't immediately start loosening gates again just
    # because the very next cycle still has leftover cash.
    idle_cash_grace_seconds: int = Field(default=300, ge=0, le=21600)
    # After the grace period, gates linearly loosen toward their max
    # multiplier/relaxation over this many additional seconds, then hold
    # at the max for as long as cash keeps sitting idle.
    idle_cash_ramp_seconds: int = Field(default=1800, ge=1, le=21600)
    # Shared ceiling for every multiplicative gate (max spread, extension
    # from today's high/low, VWAP band) at full ramp - one knob, not three,
    # since there's no real reason to relax these three at different rates
    # and a fake extra layer of granularity is worse than none.
    idle_cash_max_gate_multiplier: Decimal = Field(
        default=Decimal("3"), ge=1, le=10
    )
    # Subtracted from tick_direction_veto_threshold at full ramp (e.g. the
    # default threshold 0 becomes -0.5 fully relaxed) - allows a slightly
    # tick-negative entry through rather than requiring purely non-negative
    # recent tape direction.
    idle_cash_max_tick_relaxation: Decimal = Field(
        default=Decimal("0.5"), ge=0, le=2
    )
    # The opening print is naturally wider/choppier than mid-day trading, so
    # the normal spread/extension gates - tuned for profitable mid-day
    # scalping - reject almost everything in the first few minutes after the
    # bell. This grace window relaxes both gates only for that opening
    # stretch, then snaps back to the tighter full-day thresholds.
    opening_grace_minutes: int = Field(default=10, ge=0, le=120)
    opening_grace_spread_multiplier: Decimal = Field(
        default=Decimal("2"),
        ge=1,
        le=10,
    )
    opening_grace_extension_multiplier: Decimal = Field(
        default=Decimal("2"),
        ge=1,
        le=10,
    )
    option_take_profit_percent: Decimal = Field(default=Decimal("0.75"), gt=0)
    option_stop_loss_percent: Decimal = Field(default=Decimal("0.50"), gt=0, le=1)
    # Forced exit once a held contract is this many days or fewer from
    # expiration, regardless of target/stop - theta/gamma accelerate sharply
    # in the final days and holding through that stops being a directional
    # bet and becomes pin-risk roulette.
    option_min_hold_dte: int = Field(default=2, ge=0, le=30)
    # Never risk more than this fraction of buying power on a single options
    # entry - a defined-risk-per-trade cap layered on top of (not instead
    # of) OPTION_QUANTITY and MAX_ORDER_NOTIONAL.
    option_capital_fraction: Decimal = Field(default=Decimal("0.05"), gt=0, le=1)
    stop_loss_escalate_seconds: int = Field(default=15, ge=5, le=120)
    daily_loss_circuit_breaker_enabled: bool = False
    daily_max_loss_dollars: Decimal = Field(default=Decimal("50"), gt=0)
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

    agent_enabled: bool = True
    groq_api_key: str = ""
    # A plain (non-agentic) model, not one of Groq's Compound systems -
    # research is scored entirely from provided STATE data with no web
    # search (see market_agent.py), so Compound's tool-orchestration layer
    # was pure overhead: the actual source of the truncated/malformed/
    # empty responses _parse_response kept having to work around.
    groq_model: str = "llama-3.3-70b-versatile"
    # Groq's own usage dashboard attributes each compound-mini call to 3
    # underlying model rows (the compound orchestration plus its 2 backing
    # models - see console.groq.com's per-key usage table), so the real
    # cost of one "successful" research cycle is ~3x its nominal request
    # weight. Intervals below are tuned for AGENT_DAILY_REQUEST_LIMIT=83
    # (250/3) spent across the MARKET_OPEN_TIME-to-EOD_CLOSE_TIME trading
    # day, weighted toward core hours the same way the original 250-budget
    # tuning was: ~65 core-hour calls (23400s / 360s) + ~18 extended-hour
    # calls (33600s / 1866s) = ~83.
    agent_core_research_seconds: int = Field(default=360, ge=15, le=3600)
    agent_extended_research_seconds: int = Field(default=1866, ge=15, le=3600)
    agent_daily_request_limit: int = Field(default=83, ge=1, le=250)
    # Groq's real cap is tokens per day (TPD), not request count - a quiet
    # account can exhaust TPD in well under agent_daily_request_limit
    # requests. This must match your actual Groq model/tier TPD limit (see
    # console.groq.com/settings/billing) with some margin - the default
    # assumes the free/on-demand llama-3.3-70b-versatile tier (100000 TPD).
    agent_daily_token_budget: int = Field(default=90000, ge=1000)
    # Smaller on purpose, trading research breadth for reliability: fewer
    # STATE symbols means a smaller expected response, which means a much
    # higher chance the model finishes the full JSON within budget every
    # single cycle rather than needing the truncation-salvage fallback.
    agent_max_symbols: int = Field(default=3, ge=1, le=50)
    # Per-list cap (gainers/losers/most-active each) for the deterministic
    # market-pulse context fed to the research agent - see
    # AutoTrader.refresh_market_pulse(). Small and fixed on purpose: this
    # replaced asking the agent to discover movers via open-ended web
    # search, which was the actual source of unpredictable request size.
    agent_market_pulse_symbols: int = Field(default=3, ge=1, le=10)
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
    # Widens the stop immediately after entry (avoids getting shaken out by
    # quote noise right at fill) then tightens back to adaptive_stop_percent's
    # normal value as the position ages - see AutoTrader.position_opened_at
    # and TradingStrategy.time_aware_stop_multiplier.
    time_aware_stop_enabled: bool = False
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

    trading_timezone: str = "America/New_York"
    market_open_time: str = "04:00"
    eod_close_time: str = "19:50"
    market_close_time: str = "20:00"
    option_market_open_time: str = "09:30"
    option_eod_close_time: str = "15:50"
    option_market_close_time: str = "16:00"
    eod_retry_seconds: int = Field(default=10, ge=2, le=120)
    market_holidays: str = ""
    wash_sale_block_days: int = Field(default=31, ge=31, le=365)
    wash_sale_state_file: str = "conf/wash_sale_blocks.json"
    daily_pnl_state_file: str = "conf/daily_pnl.json"
    trade_history_state_file: str = "conf/trade_history.json"
    invalid_symbol_state_file: str = "conf/invalid_symbols.json"
    stock_limit_offset: Decimal = Field(
        default=Decimal("0.005"),
        ge=0,
        le=Decimal("0.10"),
    )
    option_limit_offset: Decimal = Field(default=Decimal("0.03"), ge=0, le=Decimal("0.25"))
    log_directory: str = "logs"
    status_file: str = "status.json"
    command_file: str = "commands.json"

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
        if self.option_min_hold_dte >= self.option_max_dte:
            raise ValueError(
                "OPTION_MIN_HOLD_DTE must be lower than OPTION_MAX_DTE, or "
                "every discovered contract would fall inside its own "
                "forced-exit window and never be enterable"
            )
        if self.stock_priority_fraction + self.stock_penny_fraction > 0.90:
            raise ValueError(
                "STOCK_PRIORITY_FRACTION + STOCK_PENNY_FRACTION must be <= 0.90"
            )
        capital_total = sum(self.stock_capital_fractions().values())
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

    def default_watchlist(self) -> list[str]:
        return [
            item.strip().upper()
            for item in self.default_watchlist_symbols.split(",")
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
