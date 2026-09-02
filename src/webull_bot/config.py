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
    # Phase 0 of the polling-to-streaming migration (see the plan) - a
    # read-only observer that subscribes to Webull's gRPC order/position
    # event stream (TradeEventStreamService) and only logs what it
    # receives. Default off: this opens a new authenticated network
    # connection and background thread with an SDK whose exact event
    # payload schema hasn't been confirmed from real traffic yet: no
    # trading behavior depends on it until that's verified live.
    event_stream_enabled: bool = False

    stock_symbols: str = "ALL"
    option_contracts: str = ""
    option_underlyings: str = ""
    option_type: str = Field(default="BOTH", pattern="^(CALL|PUT|BOTH)$")
    option_min_dte: int = Field(default=7, ge=0, le=730)
    option_max_dte: int = Field(default=45, ge=0, le=730)
    max_symbols: int = Field(default=800, ge=0, le=50000)
    stock_universe_reserve: int = Field(default=400, ge=0, le=50000)
    stock_universe_page_size: int = Field(default=200, ge=25, le=1000)
    # By request, after live evidence: at a large MAX_SYMBOLS (today's
    # live value: 5000), the once-daily universe download + VOLFILT
    # historical-volatility scoring of the WHOLE universe took 15-20
    # minutes before AutoTrader.stock_symbols was populated at all -
    # position protection was already fixed to never block on this
    # (see resolve_targets), but no NEW entry could fire the entire
    # time either, since trade_stocks had nothing to scan. Splits the
    # once-daily load into a small, fast initial batch (unblocks
    # trading in roughly a minute instead of 15-20) followed by
    # continued growth toward the full MAX_SYMBOLS in the background -
    # see AutoTrader._grow_stock_universe. Only applies when
    # STOCK_SYMBOLS=ALL; an explicit symbol list is already small and
    # fast to resolve.
    stock_universe_initial_limit: int = Field(default=500, ge=1, le=50000)
    stock_universe_growth_batch_size: int = Field(default=1000, ge=1, le=50000)
    stock_universe_growth_interval_seconds: int = Field(
        default=180, ge=30, le=3600
    )
    stock_batch_size: int = Field(default=100, ge=1, le=300)
    # By request: "scan through all [the universe], split it up in
    # parallel streams... as many as needed to scan everything and
    # filter it down, then dynamically less as it is filtered down...
    # does not need to be as intense in extended hours." A single
    # trade_stocks cycle previously fetched exactly one STOCK_BATCH_SIZE
    # (100, Webull's own hard per-call snapshot cap) worth of quotes -
    # at a large, still-growing universe (up to MAX_SYMBOLS), that's a
    # small fraction of the universe covered per cycle. Fires multiple
    # STOCK_SNAPSHOT_MAX_SYMBOLS-sized quote batches CONCURRENTLY
    # instead (see AutoTrader.stock_scan_concurrent_batches/trade_
    # stocks) - the batch count scales with how large the current
    # universe is (more concurrent batches while it's still large/
    # freshly grown, dynamically fewer as prioritized_stock_batch's own
    # activity-based ranking naturally concentrates real candidates),
    # bounded by stock_scan_max_concurrent_batches as a hard safety cap
    # on real request volume (Webull's API already returned live 429
    # TOO_MANY_REQUESTS errors this session - see _is_rate_limited).
    # By request, after live evidence: at the original default (20),
    # concurrent batching only kicks in past batch_size*20=2000
    # symbols - with a live universe still growing toward MAX_SYMBOLS
    # (5000) and sitting at ~1846 for a long stretch along the way,
    # this meant scanning stayed at a single 100-symbol batch per
    # cycle the whole time, not the "more candidates, more trades more
    # frequently" a genuinely concurrent multi-batch scan should
    # deliver. Lowered to 5 - concurrency now starts mattering at
    # batch_size*5=500 symbols (already active almost immediately
    # after the fast initial universe load) and scales up faster as
    # the universe grows, while stock_scan_max_concurrent_batches (8)
    # and the 429-retry logic (_retry_once_on_rate_limit) still bound
    # the real request-volume risk.
    stock_scan_target_full_coverage_cycles: int = Field(
        default=5, ge=1, le=500
    )
    # By request: "we want entry and profit to be quicker" - live
    # evidence showed a full trade_stocks cycle (needed to detect a
    # FRESH entry signal, or a held position's FIRST crossing into
    # profit/loss territory before any exit order exists to actively
    # manage) only completing every 30-90+ seconds, well past the point
    # this cap (8) becomes the binding constraint on a large universe -
    # at stock_batch_size=100, needed concurrent batches already
    # exceeds 8 whenever the universe is above ~4000 symbols with the
    # default 5-cycle coverage target, so this cap was the actual
    # throughput ceiling for most of the day, not the coverage-cycles
    # target above it. Raised 8 -> 12 (a 50% increase in real request
    # volume) - still well short of the field's own max (50), and
    # _retry_once_on_rate_limit already backstops the live 429
    # TOO_MANY_REQUESTS risk this was originally capped against.
    stock_scan_max_concurrent_batches: int = Field(default=12, ge=1, le=50)
    stock_scan_extended_hours_concurrency_fraction: Decimal = Field(
        default=Decimal("0.5"), ge=0, le=1
    )
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
    # By request: "out of 7500 stocks it should easily be able to find
    # enough stocks to invest everything" - live evidence: a single
    # FDX entry consumed ~43% of the whole account's buying power in
    # one trade ($186 -> $106.51), because entry_budget (see the
    # general BUY/SHORT entry gates in trade_stocks) was only ever
    # capped by bucket_remaining (up to stock_popular_capital_fraction,
    # 70% by default, of the WHOLE bucket's allocation - not per
    # position) and the live buying_power itself, with no per-position
    # diversification cap. With capital this concentrated into 1-2
    # trades, there's little left for the other several thousand
    # scanned candidates to ever get funded, even though plenty of them
    # individually clear every other gate. Caps any single fresh
    # entry's budget at this fraction of the CURRENT (already cycle-
    # shrinking) buying_power - naturally self-reducing as more
    # capital gets deployed within the same cycle, spreading what's
    # left across more symbols instead of one candidate absorbing most
    # of a bucket's whole allocation.
    stock_max_position_fraction_of_buying_power: Decimal = Field(
        default=Decimal("0.15"),
        gt=0,
        le=1,
    )
    top_gainers_limit: int = Field(default=200, ge=0, le=5000)
    # By request: "get the top gainers before the day starts and look
    # to invest in that for quick profit." Distinct from
    # top_gainers_limit above (Webull's default DAY_1/regular-session
    # ranking, anonymously folded into the whole universe) - Webull's
    # screener also supports rank_type="PRE_MARKET" directly (today's
    # biggest pre-market movers specifically), fetched once/day and
    # fed into seed_popular_symbols + force-scanned every cycle - see
    # AutoTrader.refresh_premarket_gainers. Smaller default than
    # top_gainers_limit - a curated priority list, not a universe-
    # filler.
    premarket_gainers_limit: int = Field(default=50, ge=0, le=1000)
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
    # Already at 1.8:1 - clears the researched "most professional
    # traders target at least 1:1.5-1:2 reward:risk" convention (a
    # 40% win rate at 3:1 beats a 70% win rate at 0.5:1; risk:reward
    # matters more than win rate alone for long-run expectancy).
    stock_target_stop_multiple: Decimal = Field(
        default=Decimal("1.8"),
        ge=Decimal("0.5"),
        le=Decimal("5"),
    )
    # By request: risk-based position sizing (the professional 1-2%
    # rule) - size an entry so hitting the stop costs no more than this
    # fraction of buying power, instead of however many shares a fixed
    # dollar budget happens to afford. Set deliberately above the
    # textbook 1-2% for THIS account's size: on a ~$200 account, 1%
    # is $2/trade - barely actionable once spread and fees are
    # accounted for. This is a documented, deliberate tradeoff for a
    # small account, not a hidden compromise on the underlying
    # principle. See TradingStrategy.risk_based_share_count.
    stock_risk_per_trade_fraction: Decimal = Field(
        default=Decimal("0.03"), gt=0, le=Decimal("0.25")
    )
    # Under this strategy's own stop discipline, a held position's live
    # price should never legitimately drift this far from its cost basis -
    # the adaptive stop (stock_stop_loss_max_percent, at most ~1.5%) would
    # have already closed it out long before a real move got anywhere near
    # 15%. If average_cost and the live quote diverge by more than this,
    # treat average_cost as untrustworthy (a bad broker read, not a real
    # price move) rather than deriving a target/stop from it - see
    # TradingStrategy.stock_decision. See quote_price_sanity_percent below
    # for the related but separately-calibrated bid/ask check.
    stock_price_sanity_percent: Decimal = Field(
        default=Decimal("0.15"),
        gt=0,
        le=1,
    )
    # A quote's bid/ask diverging this far from that SAME quote's own
    # last-trade price - not over time, one snapshot - means distrust it
    # (see WebullAPI._sane_bid_or_ask) rather than pricing an order off
    # it. Deliberately much tighter than stock_price_sanity_percent: this
    # account's own real quotes, including its thinnest PENNY-bucket
    # names, never showed a spread-side divergence anywhere near this
    # (worst observed ~5%), so 8% has real margin above genuine illiquid
    # spreads while still catching the live incident that motivated this -
    # FPE's ask sat 13-60% above its own last-trade price for hours,
    # repeatedly pricing an exit order that could never fill.
    quote_price_sanity_percent: Decimal = Field(
        default=Decimal("0.08"),
        gt=0,
        le=1,
    )
    # How long AutoTrader.entry_price_sanity_cooldown_ready backs off a
    # symbol after a price_sanity_ok rejection, before letting a fresh
    # entry attempt retry it. Live incident: one illiquid symbol's quote
    # sat just past PRICE_SANITY_TOLERANCE and got retried (and
    # re-rejected) on essentially every scan cycle for hours with no
    # backoff at all - not entering is always safe, so this only ever
    # delays a retry, it never forces one through the way the exit
    # side's stalled-order backstops do.
    price_sanity_cooldown_seconds: int = Field(default=30, ge=5, le=600)
    stock_entry_max_spread_percent: Decimal = Field(
        default=Decimal("0.50"),
        gt=0,
        le=Decimal("5"),
    )
    # By request, after live evidence: WNW (and, per the user's
    # account, WKHS) stopped out shortly after core hours ended,
    # consistent with a fresh entry opened with little runway left
    # before the session's liquidity/spread conditions get materially
    # worse - core_session_active was only ever a boolean (in/out of
    # the window), with no awareness of HOW MUCH of the window was
    # actually left when a brand-new position was opened. A fresh
    # entry this close to close has almost no time to reach its
    # profit target before conditions change, unlike one opened
    # earlier in the session. Fresh entries ONLY (general BUY/SHORT
    # and volatility-scalp) - averaging down on an existing position
    # (already committed to earlier, when there was more runway) and
    # every exit path are deliberately unaffected.
    stock_entry_blackout_minutes_before_close: int = Field(
        default=15, ge=0, le=120
    )
    # By request: "start transitioning away from core hours strategy
    # around 30 minutes before end of core hours." Softer/earlier than
    # stock_entry_blackout_minutes_before_close above (a hard block on
    # every fresh entry, closer to the bell) - this window instead
    # makes entry QUALITY behave like extended hours (POPULAR-bucket-
    # only fresh entries, wider spread tolerance) without stopping
    # trading outright. Must stay >= the hard-blackout window above so
    # the softer transition always starts before (or exactly at) the
    # hard cutoff, never after it.
    late_core_session_transition_minutes: int = Field(
        default=30, ge=0, le=180
    )
    # By request: "we basically just want to be able to stay in a
    # significant profit until eod" -> clarified as "let winners run
    # further before taking profit" once the day is already
    # significantly ahead. Once today's realized pnl reaches this
    # fraction of account value, general-path (whole-share/fractional,
    # not the volatility-scalp cohort - see stock_decision's
    # profit_target_multiplier param) profit targets widen by
    # profit_target_widen_multiplier instead of taking the normal,
    # earlier target - the account is already ahead for the day, so a
    # working position gets more room to grow that lead instead of
    # being sold at the same target size it would use starting from
    # flat/behind.
    daily_significant_profit_fraction: Decimal = Field(
        default=Decimal("0.03"), gt=0, le=1
    )
    profit_target_widen_multiplier: Decimal = Field(
        default=Decimal("1.5"), ge=1, le=5
    )
    # By request: "when we have a certain profit we should also not
    # allow stops to be too low" - same daily_significant_profit_
    # fraction trigger as the target-widening above, but tightens the
    # general path's stop distance instead (< 1 = tighter) - see
    # AutoTrader.stop_tighten_multiplier/stock_decision's stop_
    # tighten_multiplier param. Combined with profit_target_widen_
    # multiplier above, once the account is already significantly
    # ahead for the day: smaller downside (tighter stop), bigger
    # upside (wider target) - an intentionally asymmetric risk:reward
    # once there's already a lead worth protecting.
    stop_tighten_multiplier: Decimal = Field(
        default=Decimal("0.7"), gt=0, le=1
    )
    stock_entry_max_extension_percent: Decimal = Field(
        default=Decimal("0.01"),
        ge=0,
        le=Decimal("0.20"),
    )
    # Volatility-scalp: a parallel, much faster entry/exit path for
    # symbols whose own realized short-window volatility clears
    # volatility_scalp_min_stdev_percent - buy a small dip, sell a small
    # rip, repeatedly, all day. Runs alongside the normal EMA/VWAP trend
    # entries (unaffected for everything else); only the profit-take side
    # of the exit is overridden for a volatility-scalp position (see
    # TradingStrategy.volatility_scalp_target_price and its use in
    # AutoTrader.trade_stocks) - the normal stop-loss stays fully in
    # effect, since a "very volatile" stock is exactly where that
    # protection matters most.
    volatility_scalp_enabled: bool = True
    # By request, after pre-market losses ("no volatility scalp in
    # extended hours"): superseded the earlier dampened-intensity dial
    # that used to live here - fresh volatility-scalp entries and
    # averaging-down now simply never fire outside core hours at all
    # (see AutoTrader.trade_stocks), so there's no partial-intensity
    # case left to configure. Exits/repricing/position management for
    # anything already held are unaffected either way.
    volatility_scalp_lookback_samples: int = Field(default=20, ge=5, le=200)
    # By request: "regime-dependent" strategy switching - momentum in
    # trending conditions, mean-reversion in ranging ones (research:
    # neither is universally better; each thrives in the condition it
    # fits). Kaufman's Efficiency Ratio (net price movement over the
    # window / sum of the window's absolute tick-to-tick movement) is
    # the standard, well-documented regime input behind KAMA - reuses
    # the same tick-price window volatility-scalp eligibility already
    # maintains (TradingStrategy.volatility_price_history), so this
    # costs zero additional API calls. ER near 1 = price moved directly
    # (trending/efficient); near 0 = lots of back-and-forth with little
    # net progress (ranging/choppy). 0.5 is the common convention.
    trend_efficiency_trending_threshold: Decimal = Field(
        default=Decimal("0.5"), gt=0, le=1
    )
    trend_efficiency_lookback_samples: int = Field(default=10, ge=3, le=100)
    # Lowered from 1.5% -> 0.8% by request - "if the bar for entry is
    # too restrictive, lower the bar." This is the hard AND gate every
    # entry signal sits behind (is_volatility_scalp_eligible), so it's
    # the single biggest lever on how MANY symbols the whole strategy
    # even considers - a stock only needs to be moderately choppy now,
    # not extremely so, to qualify for the fast dip/breakout/HA-reversal
    # entry path.
    volatility_scalp_min_stdev_percent: Decimal = Field(
        default=Decimal("0.008"), gt=0, le=1
    )
    # By request: "the stocks being chosen have very low volume, thus
    # they do not fluctuate much, we need high volume stocks for more
    # volatility." is_volatility_scalp_eligible previously only checked
    # realized price stdev - a thin, illiquid name can show a large %
    # stdev purely from a few small prints knocking a wide, empty
    # spread around, not real tradeable movement. Requires cumulative
    # DOLLAR volume (price x share volume, regular + extended - see
    # TradingStrategy.metrics/prices) to also clear this floor, all day
    # (not just extended hours - see AutoTrader.trade_stocks' separate,
    # harder extended-hours cutoff).
    #
    # Live incident: a first version of this floor measured raw SHARE
    # count (500,000 shares) instead of dollar volume - meaningless for
    # a penny stock. SOAR cleared 500k shares of "volume" at ~$0.28/
    # share, only ~$140k of real dollar liquidity - too thin to absorb
    # this strategy's own repeated order flow, and its PROFIT exit
    # failed to fill even after three escalation-and-reprice cycles,
    # forcing a market-order exit at a loss the same day this shipped.
    # $5M/day is comfortably above that failure and scales correctly
    # at any price level, not just penny names.
    volatility_scalp_min_dollar_volume: Decimal = Field(
        default=Decimal("5000000"), ge=0
    )
    # Originally lowered from 0.5% -> 0.2% by request ("constantly buy
    # the dip... even a little rise"), then raised back up here as a
    # structural fix, not a same-day band-aid: 0.2% is noise-level on a
    # cheap, choppy penny stock - live incident, BTCT averaged down at
    # 1.79 then 1.78, essentially the same price, gaining no real risk
    # reduction per add. Compared against freqtrade's documented DCA
    # pattern, whose own docs warn a trigger this tight "runs out of
    # money" refilling into noise rather than a real dip. 1.0% is a
    # genuinely meaningful pullback - still well within reach multiple
    # times a day for a name that clears VOLATILITY_SCALP_MIN_STDEV_
    # PERCENT in the first place, but no longer fires on a single bid/
    # ask bounce. See volatility_scalp_averaging_step_multiplier for how
    # this widens further at each successive averaging level.
    volatility_scalp_dip_entry_percent: Decimal = Field(
        default=Decimal("0.01"), gt=0, le=1
    )
    # Each successive averaging-down buy requires a proportionally
    # bigger drop than the last, via required = dip_entry_percent * (1
    # + this * level) - level 0 (the first averaging buy) uses the base
    # threshold, level 1 needs 1.5x that, level 2 needs 2x, etc. A
    # position already several buys deep needs a genuinely bigger move
    # to justify yet another add, not just another noise-level tick -
    # spans the whole averaging ladder across a real range instead of
    # exhausting all VOLATILITY_SCALP_MAX_AVERAGING_BUYS attempts within
    # a percent or two of movement.
    volatility_scalp_averaging_step_multiplier: Decimal = Field(
        default=Decimal("0.5"), ge=0, le=5
    )
    # By request, after an end-of-day retrospective: "we just kept
    # buying at the wrong time." The SMA trend filter added earlier
    # only catches a MULTI-DAY downtrend (refreshed once daily from
    # daily-bar closes) - it does nothing for a stock simply having a
    # bad DAY today specifically, which is what repeated same-day
    # losses on one symbol (BTCT, three times in one session) actually
    # looks like: an intraday decline that hasn't shown up in the daily
    # SMA yet, since today's close hasn't happened. The scalp entry
    # path never checked the intraday VWAP at all (see vwap_supports_
    # entry, already used by the general strategy) - a stock trading
    # meaningfully below its own session VWAP is showing real intraday
    # weakness, not just a normal dip. Needs its own, much wider band
    # than VWAP_ENTRY_BAND_PERCENT (0.1%, tuned for the general
    # strategy's more liquid names) - a genuinely wide-spread, choppy
    # penny stock's normal dip-buy can easily sit several percent below
    # its own VWAP without that being a real warning sign.
    volatility_scalp_vwap_band_percent: Decimal = Field(
        default=Decimal("0.05"), ge=0, le=Decimal("0.5")
    )
    # Raised 0.2% -> 0.5% -> 1.0% by request. The first raise wasn't
    # enough - live incident: LHAI entered 1.185, averaged down to a
    # blended cost of ~1.17, and the 0.5% target closed it at 1.18,
    # barely above cost after fees ("closed too low and early"). 0.5%
    # of a ~$1 stock is a fraction of a cent in absolute terms - too
    # small to survive fees plus any real slippage. 1.0% keeps the
    # "quick" scalp character (still tiny relative to this cohort's
    # own volatility floor, MIN_HISTORICAL_VOLATILITY_PERCENT >= 3%)
    # while giving a real move room to actually register as profit.
    volatility_scalp_target_percent: Decimal = Field(
        default=Decimal("0.01"), gt=0, le=1
    )
    # By request: "when buying multiple shares, if needed be able to
    # sell them in parts as the value shifts, to maximize profits, or
    # minimize loss... buy 20, sell 5 every 5 cents it goes up... buy
    # 10, average down 10, and if it goes up a little sell 10 and keep
    # the rest for later." Scoped to PROFIT only - a stop-loss always
    # sells the full remaining quantity (real capital protection, no
    # partial on the downside, matching "minimize loss" specifically).
    # See TradingStrategy.volatility_scalp_partial_exit_quantity/
    # AutoTrader.evaluate_held_stock_exits.
    volatility_scalp_partial_exit_enabled: bool = True
    # Fraction of the CURRENTLY HELD quantity sold on each partial
    # exit (0.5 = sell half, keep half riding). Applied repeatedly as
    # price keeps climbing, so a held position naturally scales out in
    # a shrinking ladder rather than one all-or-nothing sale.
    volatility_scalp_partial_exit_fraction: Decimal = Field(
        default=Decimal("0.5"), gt=0, lt=1
    )
    # Required price move, from the price at the LAST partial exit,
    # before another one can fire - without this, the very next 0.25s
    # cycle would immediately sell again at essentially the same price
    # (the quick target itself doesn't move once a position is
    # partially closed, only the held quantity shrinks). This is what
    # actually implements "sell some more every N cents/percent it
    # keeps climbing" instead of dumping the whole position at once.
    volatility_scalp_partial_exit_reprice_percent: Decimal = Field(
        default=Decimal("0.01"), gt=0, le=1
    )
    # Once the remaining quantity after a partial sale would drop to
    # or below this many shares, sell the FULL remainder instead of
    # another partial - keeps the tail end of a ladder from grinding
    # down into odd-lot slivers too small to matter (or, for a sub-$1
    # stock, below Webull's own 100-share minimum order size).
    volatility_scalp_partial_exit_min_remainder_shares: int = Field(
        default=10, ge=1, le=1000
    )
    # Gates TradingStrategy.volatility_scalp_exit_override's fourth,
    # most-eager exit path (stalling momentum on a profitable position) -
    # by request: "too trigger happy to sell... not capturing the
    # profits when it can." Price must have already covered at least
    # this fraction of the full distance from cost to the quick target
    # before an early stall-triggered exit is allowed to fire - a small,
    # immediate profit alone isn't enough anymore, it has to actually be
    # most of the way to the real target first.
    volatility_scalp_momentum_stall_min_profit_fraction: Decimal = Field(
        default=Decimal("0.6"), gt=0, le=1
    )
    # Raised 3 -> 8 by request, after finding buying power sitting idle
    # ("not investing all the capital") - 3 concurrent slots capped how
    # much of the account's capital the scalp strategy could ever have
    # working at once, regardless of how much buying power remained.
    # Per-trade sizing (volatility_scalp_target_notional_buying_power_
    # fraction) and the total-exposure/position-value caps already
    # backstop this independently - raising the slot count lets sizing
    # actually reach those existing caps instead of stopping short of
    # them for lack of an open slot.
    #
    # By request, urgent, after live evidence: "it has sold twice for
    # heavy losses without averaging down." Traced the exact numbers -
    # FNGR (a sub-$1 stock, forced into Webull's 100-share minimum lot
    # regardless of account size, ~$45 notional at its price) needed
    # to average down with only $47.80 remaining buying power (5
    # concurrent scalp positions already open, spreading an ~$187
    # account thin). That single forced-minimum buy alone would have
    # consumed 94% of what was left, blowing straight past the 12%
    # per-symbol risk budget - averaging_down_capacity correctly
    # computed ZERO room before any averaging could even start, not
    # because of a broken cap, but because too many concurrent
    # positions had already spread the account's real capital too
    # thin to defend any single one of them. Lowered 8 -> 3 - fewer
    # concurrent slots means genuinely more capital stays available
    # behind each open position, so a forced-minimum-lot averaging buy
    # on a low-priced stock doesn't immediately exhaust the risk
    # budget the moment it's needed.
    volatility_scalp_max_concurrent_positions: int = Field(
        default=3, ge=1, le=20
    )
    # By request: "if they dip a lot after you buy, average it out with
    # another buy" - caps how many additional buys a single held cohort
    # position can make while averaging down, bounding worst-case
    # exposure per symbol to (this + 1) * volatility_scalp_share_count's
    # fixed lot size, instead of an unbounded chase. 0 disables
    # averaging entirely. Raised 3 -> 5 by request - "not averaging
    # down enough."
    #
    # By request, after urgent live evidence ("it is still not
    # averaging down properly, make sure it tries to average down
    # before the stop loss"): live AVGDOWN diagnostic showed FNGR
    # blocked on "averaging cap reached" for 6+ minutes straight before
    # its stop-loss fired - averaging_down_capacity's own account-risk-
    # derived ceiling (see volatility_scalp_max_symbol_risk_fraction)
    # came out well above 5 for this account's actual buying power at
    # the time, confirming the CONFIGURED "5" itself, not real risk
    # capacity, was the binding constraint. Raised 5 -> 10 (the field's
    # own max) - averaging_down_capacity's risk-based cap (still fully
    # in effect, unchanged) remains the real backstop on worst-case
    # exposure regardless of this ceiling.
    volatility_scalp_max_averaging_buys: int = Field(default=10, ge=0, le=10)
    # Research finding (compared against freqtrade's documented DCA
    # pattern after "basically only taking losses" was reported live):
    # a mature DCA implementation never fully suppresses the stop-loss
    # during averaging - it keeps a wide-but-always-active hard stop
    # live from entry as a catastrophic-loss backstop distinct from the
    # per-level re-buy logic. A drop beyond this means a real
    # breakdown, not a normal dip, and the actual stop-loss is let
    # through instead of staying suppressed until every averaging
    # attempt is exhausted.
    #
    # By request, after live evidence ("way too chill with stop loss
    # right now instead of averaging down first"): the DCA ladder's
    # per-level required drop (volatility_scalp_dip_entry_percent *
    # (1 + averaging_step_multiplier * level) - 1%, 1.5%, 2%, 2.5%, 3%
    # across the 5 default levels, each measured against the average
    # cost AFTER the prior add) barely fit inside the original 5%
    # backstop - by the time a fast decline reached the later levels'
    # required drop, the cumulative real move from the ORIGINAL entry
    # price could already exceed 5%, hitting the backstop before more
    # than 1-2 levels ever got a real chance to fire. Raised 5% -> 8%
    # to give the whole ladder genuine room to operate across a fast
    # decline, not just the first level or two, while still keeping a
    # real catastrophic-loss backstop rather than removing it.
    volatility_scalp_hard_stop_percent: Decimal = Field(
        default=Decimal("0.08"), gt=0, le=1
    )
    # By request: bound worst-case per-symbol exposure from averaging
    # down. Research finding acted on directly: "doubling down three
    # times can turn a 7% position into an 18% loss... in a bad market
    # that 50% can be 80%." Even fully averaged down to volatility_
    # scalp_max_averaging_buys and hitting the hard-stop floor, a
    # single symbol can't cost more than this fraction of buying
    # power - see TradingStrategy.averaging_down_capacity, which may
    # cap effective averaging BELOW the configured max on a small
    # account. The "5" above becomes a ceiling, not a target.
    volatility_scalp_max_symbol_risk_fraction: Decimal = Field(
        default=Decimal("0.12"), gt=0, le=1
    )
    # Live incident: GAUZ's routine 2-7% spread meant the general
    # STOCK_ENTRY_MAX_SPREAD_PERCENT (0.50%, tuned for a quote-glitch on
    # an otherwise normal, liquid stock) almost never let the exit
    # pricing fall back to the ask - exits depended entirely on the bid
    # alone clearing cost, a much harder bar than the entry side's dip
    # signal, so the position kept averaging down far faster than it
    # could ever exit. This cohort is deliberately wide-spread/choppy by
    # its own selection criterion, so its own exit pricing gets a wider,
    # separately-tunable bound instead of the general one.
    volatility_scalp_max_exit_spread_percent: Decimal = Field(
        default=Decimal("8"), gt=0, le=Decimal("50")
    )
    # Live incident: GAUZ alone grew to ~66% of total account value.
    # Caps any single cohort symbol's total position value (existing +
    # a prospective new buy, whether a fresh entry or an averaging-down
    # buy) to this fraction of account value - averaging is still
    # allowed up to VOLATILITY_SCALP_MAX_AVERAGING_BUYS, but never to
    # the point of concentrating most of the account in one name.
    #
    # By request, after live evidence ("even it is supposed to avg down
    # it is not executing it"): this check runs AFTER the real
    # averaging-down gate (which has its own AVGDOWN diagnostic) and
    # was silently zeroing the buy quantity with no logging at all - a
    # candidate could clear every gate condition and still never place
    # an order. The 35% default was calibrated for the OLD 5-buy
    # ladder (roughly 5 buys * ~7% of account value each); raising
    # volatility_scalp_max_averaging_buys to 10 without widening this
    # too meant a deep, real decline could hit THIS cap well before
    # exhausting the new averaging capacity, silently undoing that
    # fix. Raised 35% -> 50% - still meaningfully below the whole-
    # cohort ceiling just below (60%), so one symbol still can't
    # consume the entire cohort's exposure allowance alone.
    volatility_scalp_max_position_fraction: Decimal = Field(
        default=Decimal("0.50"), gt=0, le=1
    )
    # Per-symbol caps alone don't bound worst case: with up to
    # VOLATILITY_SCALP_MAX_CONCURRENT_POSITIONS symbols each individually
    # allowed to reach VOLATILITY_SCALP_MAX_POSITION_FRACTION, a
    # correlated selloff across the whole cohort (likely, since these are
    # explicitly the most volatile names selected together) had no
    # aggregate brake - three symbols could each legitimately reach 35%
    # of the account. This caps total cohort exposure across every
    # symbol combined, so an account-wide correlated move is still
    # bounded even when each individual symbol's own cap is satisfied.
    volatility_scalp_max_total_exposure_fraction: Decimal = Field(
        default=Decimal("0.60"), gt=0, le=1
    )
    # Opening-range-breakout entry signal, adapted from the classic Dual
    # Thrust strategy: fires when price pushes above the rolling window's
    # own recent local range (the same lookback the dip signal already
    # uses) by K times that range's own size - a fresh breakout to a new
    # high with real range behind it, not just a one-tick blip. This is
    # an ADDITIONAL alternative entry trigger (OR'd with the existing dip
    # signal, not a replacement) - by request, every extra qualifying
    # signal should mean MORE trading opportunities, not a stricter bar.
    # Lowered from 0.5 -> 0.2 - "if the bar for entry is too
    # restrictive, lower the bar" - a smaller K means a smaller push
    # past the recent range is enough to count as a real breakout.
    volatility_scalp_breakout_k: Decimal = Field(
        default=Decimal("0.2"), gt=0, le=Decimal("5")
    )
    # Heikin-Ashi reversal confirmation: synthetic OHLC bars are bucketed
    # from the same rolling tick-price window (HEIKIN_ASHI_BAR_SAMPLES
    # consecutive ticks per bar), then transformed to Heikin-Ashi
    # candles. A third, independent alternative entry trigger alongside
    # the dip and breakout signals - fires on a confirmed bullish
    # reversal (a green HA bar with little/no lower wick immediately
    # following a red one).
    # bar_samples * bar_count should stay <= volatility_scalp_lookback_
    # samples (default 20) so a full bar_count of bars can actually form
    # - _synthetic_bars degrades gracefully with fewer if not, but signal
    # quality is best with the full count. 3 * 6 = 18, comfortably under
    # the 20-sample window.
    heikin_ashi_bar_samples: int = Field(default=3, ge=2, le=50)
    heikin_ashi_bar_count: int = Field(default=6, ge=3, le=50)
    # Parabolic SAR trailing-stop exit: computed over the same synthetic
    # bars as the Heikin-Ashi signal. Used as an ADDITIONAL exit trigger
    # for a held volatility-scalp position, alongside (not instead of)
    # the existing quick profit target - either one can independently
    # close the position, locking in a trend reversal even if price
    # hasn't yet cleared the fixed percentage target.
    parabolic_sar_af_step: Decimal = Field(
        default=Decimal("0.02"), gt=0, le=1
    )
    parabolic_sar_af_max: Decimal = Field(default=Decimal("0.2"), gt=0, le=1)
    # Curated daily cohort, not the whole scanned universe: identify a
    # small handful of the cheapest, most volatile names and concentrate
    # on rapidly cycling just those until they cool off, instead of
    # spreading thin across every eligible symbol seen in passing. See
    # AutoTrader.select_volatility_scalp_symbols, re-run periodically
    # (VOLATILITY_SCALP_RESELECT_SECONDS) so a symbol that's slowed down
    # gets dropped and a newly-hot one takes its place.
    volatility_scalp_symbol_count: int = Field(default=4, ge=1, le=10)
    # Raised from $1.50 -> $5 by request - "the stocks in the strategy
    # can be below 5 [dollars]."
    volatility_scalp_max_price: Decimal = Field(
        default=Decimal("5"), gt=0, le=Decimal("50")
    )
    # Target dollar notional per volatility-scalp entry (see
    # TradingStrategy.volatility_scalp_share_count) - by request, don't
    # cap every penny stock at a flat 100 shares or every $1+ stock at a
    # flat 20-50 shares; size UP toward this dollar budget instead,
    # rounded to a clean lot (nearest 100 shares under $1, matching
    # Webull's own lot-restricted-band minimum there; nearest 10 shares
    # at $1+). This is a CEILING, not the actual per-trade target most
    # of the time - see volatility_scalp_target_notional_buying_power_
    # fraction just below, which usually binds first on a small
    # account. volatility_scalp_max_position_fraction/
    # volatility_scalp_max_total_exposure_fraction and the buying-power
    # affordability check already applied at every call site still
    # backstop this - raising the per-trade target doesn't remove
    # either cap, it just means sizing can actually reach them instead
    # of always landing far below.
    volatility_scalp_target_notional: Decimal = Field(
        default=Decimal("400"), gt=0, le=Decimal("100000")
    )
    # Live sanity check caught this: a flat dollar target alone doesn't
    # scale with account size - on a small account (live example:
    # $107.80 buying power), a $400 flat target gets silently zeroed by
    # the affordability check on nearly every attempt (scalp_quantity
    # forced to 0, not gracefully shrunk - see trade_stocks), meaning
    # close to ZERO trades instead of "high frequency." The actual
    # per-trade target is min(volatility_scalp_target_notional, buying_
    # power * this fraction) - scales down automatically on a small
    # account and up as the account grows, so this doesn't need
    # re-tuning by hand as the account's size changes.
    volatility_scalp_target_notional_buying_power_fraction: Decimal = Field(
        default=Decimal("0.15"), gt=0, le=Decimal("1")
    )
    # By request: "we don't only want it to stick with the same cohort
    # throughout the day, it should also add to the cohort, which is
    # why we also have such a big universe." select_volatility_scalp_
    # symbols already re-ranks the cohort from data across the WHOLE
    # scanned universe (not just today's starting picks) and fully
    # replaces stale members with newly-hot ones - the mechanism the
    # user is asking for already existed - but the original 1800s (30
    # minute) cadence made that feel static relative to how fast
    # everything else in this bot moves (1s scalp repricing, 0.25s
    # position protection). Lowered to 300s (5 minutes) - meaningfully
    # more dynamic without excessive churn. An open position in a
    # symbol that drops out of the cohort is never stranded either way -
    # volatility_scalp_positions (a separate, position-based set) keeps
    # it fully managed regardless of current cohort membership.
    volatility_scalp_reselect_seconds: int = Field(
        default=300, ge=60, le=86400
    )
    # Zeroed by explicit request - "orders can be made as frequently as
    # possible without a cooldown." The only remaining gap between a
    # position closing and re-entering the same symbol is however long
    # the fill/account-refresh round-trip itself actually takes (see
    # volatility_scalp_positions' synchronous, race-free tracking in
    # bot.py) - there's no artificial wait layered on top of that.
    volatility_scalp_reentry_cooldown_seconds: int = Field(
        default=0, ge=0, le=300
    )
    # By request, after the DAIC incident (3 stop-losses in ~9 minutes
    # on one symbol during a fast decline, erasing the day's gains):
    # a narrow, symbol-specific exception to the zeroed cooldown above -
    # only pauses fresh re-entry into a symbol that JUST stopped out,
    # for a few minutes, long enough for a fast decline to finish
    # shaking out. Deliberately NOT a same-day quarantine (rejected
    # earlier as "a bandaid") and compatible with "keep trading through
    # losses" - every other symbol and slot is unaffected. See
    # AutoTrader.post_stop_reentry_ready.
    volatility_scalp_post_stop_cooldown_seconds: int = Field(
        default=300, ge=0, le=3600
    )
    # Warm-starts a symbol's volatility window from real M1 bars the
    # moment it's first scanned, instead of needing several live scan
    # cycles just to accumulate enough snapshot-poll samples to become
    # eligible - see WebullAPI.recent_minute_closes and AutoTrader.
    # trade_stocks' bar-seed call. Self-limiting: only ever fetched once
    # per symbol (skipped the moment its window is non-empty), so this
    # never grows unbounded as the watchlist rotates through.
    volatility_scalp_bar_seed_enabled: bool = True
    # How often a resting volatility-scalp PROFIT order gets re-quoted to
    # the current ask - deliberately much faster than the generic
    # ORDER_MONITOR_SECONDS (order_monitor_seconds) reprice cadence every
    # other resting PROFIT order uses, since this strategy exists
    # specifically to capture a fast-moving, choppy stock's small moves
    # "cent by cent" rather than rest passively - see
    # AutoTrader.reprice_volatility_scalp_exits. Lowered 1 -> 0.25
    # (the field's own floor) by request: "bring repricing to the 0.25
    # lane as well" - matches poll_seconds/the rest of the fast
    # position-protection loop's cadence exactly.
    volatility_scalp_reprice_seconds: Decimal = Field(
        default=Decimal("0.25"), ge=Decimal("0.25"), le=Decimal("30")
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
    # because the very next cycle still has leftover cash. Short on
    # purpose: keeping cash deployed outranks entry quality (see below),
    # so idle capital should barely get a breather before gates start
    # loosening again.
    idle_cash_grace_seconds: int = Field(default=60, ge=0, le=21600)
    # After the grace period, gates linearly loosen toward their max
    # multiplier/relaxation over this many additional seconds, then hold
    # at the max for as long as cash keeps sitting idle.
    idle_cash_ramp_seconds: int = Field(default=600, ge=1, le=21600)
    # Shared ceiling for every multiplicative gate (max spread, extension
    # from today's high/low, VWAP band) at full ramp - one knob, not three,
    # since there's no real reason to relax these three at different rates
    # and a fake extra layer of granularity is worse than none.
    idle_cash_max_gate_multiplier: Decimal = Field(
        default=Decimal("5"), ge=1, le=10
    )
    # Subtracted from tick_direction_veto_threshold at full ramp (e.g. the
    # default threshold 0 becomes -1.0 fully relaxed) - allows a
    # tick-negative entry through rather than requiring purely non-negative
    # recent tape direction.
    idle_cash_max_tick_relaxation: Decimal = Field(
        default=Decimal("1.0"), ge=0, le=2
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
    # By request: "we do not want intense play in extended hours, but
    # we want play for sure." Live evidence: over a ~7.5 hour pre-
    # market stretch, "spread too wide to scalp profitably" (entry_
    # spread_ok, the GENERAL momentum path's entry gate - despite the
    # "scalp" wording in that decision reason string, it has nothing
    # to do with the separate volatility-scalp cohort, which is
    # already fully blocked outside core hours by its own hard gate)
    # rejected candidates roughly 2-3x more than every other reason
    # combined - STOCK_ENTRY_MAX_SPREAD_PERCENT's tight 0.5% default
    # is realistic for core hours' real liquidity, but genuinely hard
    # for almost anything to clear before real two-sided volume shows
    # up pre-market. Modestly loosens (not fully opens) the spread bar
    # outside core hours only - the existing "only established/
    # popular symbols trade outside core hours" bucket restriction
    # already keeps this to established names, not a free-for-all.
    # By request, recalibrated: "pre trading should not be too
    # intense, and it should just set up the main gainers for the
    # day" - pre-market's job shifts toward identifying candidates
    # (refresh_premarket_gainers/refresh_agent_predicted_gainers) for
    # when core hours actually start, not aggressive trading itself.
    # Lowered 3x -> 2x - still more room than the tight core-hours
    # 0.5% default (real pre-market liquidity genuinely can't clear
    # that), just less loosened than before.
    extended_hours_spread_multiplier: Decimal = Field(
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
    market_holidays: str = ""
    wash_sale_block_days: int = Field(default=31, ge=31, le=365)
    wash_sale_state_file: str = "conf/wash_sale_blocks.json"
    daily_pnl_state_file: str = "conf/daily_pnl.json"
    trade_history_state_file: str = "conf/trade_history.json"
    invalid_symbol_state_file: str = "conf/invalid_symbols.json"
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
