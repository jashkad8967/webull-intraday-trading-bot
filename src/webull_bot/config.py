from datetime import time
from decimal import Decimal
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from webull_bot.config_sections.connection_settings import ConnectionSettings
from webull_bot.config_sections.entry_filter_settings import EntryFilterSettings
from webull_bot.config_sections.entry_timing_settings import EntryTimingSettings
from webull_bot.config_sections.position_sizing_settings import PositionSizingSettings
from webull_bot.config_sections.stock_exit_settings import StockExitSettings
from webull_bot.config_sections.universe_settings import UniverseSettings
from webull_bot.config_sections.volatility_scalp_settings import VolatilityScalpSettings


class Settings(
    ConnectionSettings,
    UniverseSettings,
    EntryFilterSettings,
    VolatilityScalpSettings,
    PositionSizingSettings,
    EntryTimingSettings,
    StockExitSettings,
):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    option_take_profit_percent: Decimal = Field(default=Decimal("0.75"), gt=0)
    option_stop_loss_percent: Decimal = Field(default=Decimal("0.50"), gt=0, le=1)
    # By explicit request, for a one-off diagnostic: "make sure it
    # fires... no barrier, quickly sell it, and then change the option
    # strategy again." Off by default (real gates always apply) - when
    # explicitly turned on, trade_options skips the direction signal,
    # delta, IV percentile, market-regime, wash-sale, stop-loss-guard,
    # and quarantine checks for entries (structural checks - DTE,
    # affordability/sizing, cooldown, rate cap, max open positions -
    # still apply, so this can't spam unlimited orders), and
    # option_take_profit_percent is meant to be turned down alongside
    # it (live .env only, not this default) so the position closes
    # again almost immediately via the normal exit path instead of
    # being held. This is a temporary smoke-test switch to prove the
    # order-placement pipeline works end to end on a real contract -
    # not a permanent strategy change.
    option_smoke_test_mode: bool = False
    # Forced exit once a held contract is this many days or fewer from
    # expiration, regardless of target/stop - theta/gamma accelerate sharply
    # in the final days and holding through that stops being a directional
    # bet and becomes pin-risk roulette. By request: "closed out at least
    # 1 week before" expiration.
    option_min_hold_dte: int = Field(default=7, ge=0, le=30)
    # Never risk more than this fraction of buying power on a single options
    # entry - a defined-risk-per-trade cap layered on top of (not instead
    # of) OPTION_QUANTITY and MAX_ORDER_NOTIONAL.
    option_capital_fraction: Decimal = Field(default=Decimal("0.05"), gt=0, le=1)
    # By request: "look for cheaper options to buy in to." select_atm_
    # options always picked the single strike nearest the money -
    # correct for delta, but on a small account often unaffordable
    # outright (option_order_quantity silently rounds to 0 contracts
    # when a single contract's premium*100 exceeds what the risk cap
    # allows, quietly producing zero real option trades). This is how
    # many of the NEAREST-to-ATM candidate strikes (per expiration/
    # type) get quoted at discovery time so a cheaper, still-reasonably
    # -close strike can be picked instead when the true ATM one doesn't
    # fit - see WebullAPI.select_atm_options's own docstring.
    option_affordability_shortlist_size: int = Field(default=6, ge=1, le=20)
    # By request: "you can also use averaging down... for options as
    # well" - options analog of the volatility-scalp averaging-down
    # knobs, but wider, since options routinely move a much larger
    # percentage than the underlying stock does.
    option_averaging_down_dip_percent: Decimal = Field(
        default=Decimal("0.20"), gt=0, le=1
    )
    option_averaging_step_multiplier: Decimal = Field(
        default=Decimal("0.5"), ge=0, le=5
    )
    option_max_averaging_buys: int = Field(default=2, ge=0, le=10)
    option_averaging_reentry_cooldown_seconds: Decimal = Field(
        default=Decimal("60"), ge=0, le=3600
    )
    # By request: "you can... use call and put simultaneously type
    # strategies for options as well" - off by default (a genuine
    # straddle risks paying two premiums instead of one when the
    # underlying doesn't move enough), opt-in via this flag. See its
    # use in trade_options' direction-match gate.
    option_straddle_enabled: bool = False
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
    market_requests_per_minute: int = Field(default=240, ge=1, le=300)
    option_instrument_requests_per_minute: int = Field(default=45, ge=1, le=60)
    stock_instrument_requests_per_30_seconds: int = Field(default=9, ge=1, le=10)
    account_requests_per_second: Decimal = Field(
        default=Decimal("0.8"),
        gt=0,
        le=Decimal("1"),
    )
    order_requests_per_minute: int = Field(default=480, ge=1, le=600)
    # Lowered from 5 -> 2 -> 1 (the field's own floor) by request - "make
    # sure the data is received as frequently as possible." This just
    # gates how often the bot code ATTEMPTS a refresh (see
    # AutoTrader.account_state); the actual API call is still separately
    # paced by account_requests_per_second's token-bucket limiter
    # (0.8/sec = a hard 1.25s floor between real calls - see
    # WebullAPI._RATE_LIMITS["account"]), so this can't itself cause
    # over-limit calls, it just means a refresh fires the instant the
    # limiter allows one instead of waiting out an extra artificial gap.
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

    agent_enabled: bool = True
    groq_api_key: str = ""
    # Not one of Groq's Compound systems - research is scored entirely from
    # provided STATE data with no web search (see market_agent.py), so
    # Compound's tool-orchestration layer was pure overhead: the actual
    # source of the truncated/malformed/empty responses _parse_response
    # kept having to work around. Groq has since removed every plain
    # (non-reasoning) chat model from its catalog - gpt-oss-120b is a
    # reasoning model too, but its hidden "thinking" tokens are small and
    # bounded (unlike Compound's orchestration overhead) and controllable
    # via groq_reasoning_effort below, so it's the closer match.
    groq_model: str = "openai/gpt-oss-120b"
    # Only meaningful for a gpt-oss model (see market_agent.py's request
    # builder) - "low" keeps hidden reasoning-token spend small so it
    # doesn't crowd out the actual JSON answer within max_completion_tokens.
    groq_reasoning_effort: str = "low"
    # Fixed cadence, no core/extended split (the agent reviews account
    # performance, not per-symbol setups, so there's no reason to research
    # more often just because the market's more active).
    strategy_review_enabled: bool = True
    # By request: "space out the research agent sentiment to every 30
    # minutes." Raised 900s (15min) -> 1800s (30min) - also frees up
    # daily request/token budget headroom for the new once-daily
    # predict_likely_gainers call (see AutoTrader.refresh_agent_
    # predicted_gainers), which shares this same Groq account budget.
    strategy_review_interval_seconds: int = Field(default=1800, ge=60, le=3600)
    # How many of the most recent StatusWriter.trades entries go into each
    # review's payload - small and fixed on purpose: this runs 4x/hour,
    # so the prompt has to stay bounded regardless of how many trades a
    # high-frequency account racks up between reviews.
    strategy_review_trade_history_limit: int = Field(default=15, ge=1, le=50)
    # Groq's own usage dashboard attributes each compound-mini call to 3
    # underlying model rows (the compound orchestration plus its 2 backing
    # models - see console.groq.com's per-key usage table), so the real
    # cost of one "successful" cycle can be ~3x its nominal request
    # weight. Sized for STRATEGY_REVIEW_INTERVAL_SECONDS=900 across the
    # MARKET_OPEN_TIME-to-EOD_CLOSE_TIME trading day (~16h / 900s ≈ 64
    # reviews/day), with margin.
    agent_daily_request_limit: int = Field(default=75, ge=1, le=250)
    # Groq's real cap is tokens per day (TPD), not request count - a quiet
    # account can exhaust TPD in well under agent_daily_request_limit
    # requests. This must match your actual Groq model/tier TPD limit (see
    # console.groq.com/settings/billing, which is the only place Groq
    # reports it - it isn't in any response header) with some margin.
    agent_daily_token_budget: int = Field(default=90000, ge=1000)
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

    trading_timezone: str = "America/New_York"
    market_open_time: str = "04:00"
    eod_close_time: str = "19:50"
    market_close_time: str = "20:00"
    option_market_open_time: str = "09:30"
    option_eod_close_time: str = "15:50"
    option_market_close_time: str = "16:00"
    eod_retry_seconds: int = Field(default=10, ge=2, le=120)
    # By request, after pre-market losses: "capturing any profits to
    # close out the day as much as possible" outside core hours - how
    # often AutoTrader.close_profitable_positions_during_extended_hours
    # checks open equity positions and closes anything currently
    # sitting at a profit. Slower than eod_retry_seconds (that one's a
    # tight end-of-day retry loop) since this runs continuously through
    # the whole pre-market/after-hours session, not just a final
    # closeout window.
    extended_hours_profit_sweep_seconds: int = Field(
        default=60, ge=5, le=3600
    )
    # Live incident: is_trading_day only ever checked weekday (Mon-Fri)
    # against this list, which defaulted to empty - on Labor Day 2026
    # (a Monday), the bot spent the whole session repeatedly attempting
    # doomed orders every ~30s, each one rejected by Webull with
    # OPENAPI_CAN_NOT_TRADING_FOR_NON_TRADING_HOURS, until manually
    # fixed live via a MARKET_HOLIDAYS env override. Defaulting to a
    # real NYSE holiday calendar (rather than leaving new deploys to
    # rediscover this the same way) closes the gap out of the box;
    # .env can still override/extend this for a year not covered here.
    market_holidays: str = (
        "2026-01-01,2026-01-19,2026-02-16,2026-04-03,2026-05-25,"
        "2026-06-19,2026-07-03,2026-09-07,2026-11-26,2026-12-25,"
        "2027-01-01,2027-01-18,2027-02-15,2027-03-26,2027-05-31,"
        "2027-06-18,2027-07-05,2027-09-06,2027-11-25,2027-12-24"
    )
    wash_sale_block_days: int = Field(default=31, ge=31, le=365)
    wash_sale_state_file: str = "conf/wash_sale_blocks.json"
    daily_pnl_state_file: str = "conf/daily_pnl.json"
    trade_history_state_file: str = "conf/trade_history.json"
    invalid_symbol_state_file: str = "conf/invalid_symbols.json"
    option_contracts_state_file: str = "conf/option_contracts.json"
    # DISABLED by request, after an independent risk review found fully-
    # automatic live application of model-generated tuning to be the
    # most urgent operational risk in the system - a synthetic test
    # suite can prove config.py still behaves structurally after a
    # change, it cannot prove the change improves live expectancy. This
    # flag is now an actual functional gate (scripts/apply_strategy_
    # review.py reads and enforces it directly) - previously it was
    # declared but never read anywhere, giving false confidence that it
    # controlled anything. The real, primary gate is
    # .github/workflows/strategy-tuning-auto-apply.yml's own `if: false`
    # on the job itself (see that file's header comment) - this is
    # deliberate defense-in-depth on top of that, not the only gate.
    # See src/webull_bot/strategy_tuning.py for the bounded lever table
    # this would gate if ever re-enabled.
    strategy_tuning_auto_apply_enabled: bool = False
    strategy_tuning_cooldown_hours: int = Field(default=24, ge=1, le=168)
    strategy_tuning_step_fraction: Decimal = Field(
        default=Decimal("0.10"), gt=0, le=Decimal("0.5")
    )
    strategy_tuning_state_file: str = "conf/strategy_tuning.json"
    stock_limit_offset: Decimal = Field(
        default=Decimal("0.005"),
        ge=0,
        le=Decimal("0.10"),
    )
    option_limit_offset: Decimal = Field(default=Decimal("0.03"), ge=0, le=Decimal("0.25"))
    log_directory: str = "logs"
    status_file: str = "status.json"
    command_file: str = "commands.json"

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

    def holidays(self) -> set[str]:
        return {item.strip() for item in self.market_holidays.split(",") if item.strip()}

    def session_time(self, value: str) -> time:
        hour, minute = (int(part) for part in value.split(":"))
        return time(hour, minute)


@lru_cache
def settings() -> Settings:
    return Settings()
