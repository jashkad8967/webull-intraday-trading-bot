import math
from collections import defaultdict, deque
from decimal import Decimal, ROUND_DOWN

from webull_bot.strategy_logic.decision.stock_option_decision import (
    _exit_bias,
    adaptive_stop_percent,
    entry_extension_ok,
    entry_spread_ok,
    option_average_down_signal,
    option_decision,
    research_supports_entry,
    stock_decision,
    volatility_scalp_entry_spread_ok,
)
from webull_bot.strategy_logic.sizing.order_quantity import (
    dollar_stock_quantity,
    exit_blocked_by_lot_restriction,
    fractional_stock_quantity,
    minimum_lot_size,
    option_order_quantity,
    risk_based_share_count,
    stock_order_quantity,
)
from webull_bot.strategy_logic.constants import (
    OBI_BUY_THRESHOLD,
    OBI_DEPTH_LEVELS,
    OBI_ENABLED,
    OPTION_DELTA_MAX,
    OPTION_DELTA_MIN,
    OPTION_IV_PERCENTILE_MIN_SAMPLES,
    OPTION_IV_REJECT_PERCENTILE,
    OPTION_VIXY_REJECT_PERCENTILE,
    OPTION_VIXY_SYMBOL,
)
from webull_bot.strategy_logic.momentum.rsi import (
    relative_strength_index,
    rsi_overbought_exit,
    rsi_supports_entry,
)
from webull_bot.strategy_logic.portfolio.position_pnl import (
    open_position_count,
    portfolio_decision,
    position_day_pnl,
    position_unrealized_pnl,
)
from webull_bot.strategy_logic.regime.market_regime import (
    obi_supports_entry,
    option_delta_ok,
    option_iv_percentile_ok,
    option_market_regime_ok,
    stock_market_regime_ok,
)
from webull_bot.strategy_logic.selection.priority_ranking import (
    analyst_priority_bonus,
    priority_score,
    prioritized_stock_batch,
    research_candidates,
    selection_bucket,
    stock_scan_concurrent_batches,
)
from webull_bot.strategy_logic.volatility_scalp.exits_and_sizing import (
    averaging_down_capacity,
    volatility_scalp_average_down_signal,
    volatility_scalp_exit_override,
    volatility_scalp_partial_exit_quantity,
    volatility_scalp_share_count,
    volatility_scalp_target_price,
)
from webull_bot.strategy_logic.volatility_scalp.signals import (
    _parabolic_sar,
    _synthetic_bars,
    dual_thrust_breakout_signal,
    heikin_ashi_bullish_reversal_signal,
    is_volatility_scalp_eligible,
    parabolic_sar_exit_signal,
    realized_volatility_percent,
    seed_volatility_window,
    symbol_regime,
    trend_efficiency_ratio,
    volatility_scalp_dip_signal,
    volatility_scalp_micro_exhaustion_confirmed,
    volatility_scalp_momentum_stalled_or_rising,
    volatility_scalp_momentum_stalling,
    volatility_scalp_momentum_stalling_short,
)
from webull_bot.strategy_logic.market_state.snapshot import (
    clear_market_state,
    quote_number,
    rotating_batch,
    update_stock_snapshot,
)
from webull_bot.strategy_logic.market_state.vwap_trend import (
    _update_vwap,
    multi_day_momentum_supports_entry,
    recent_momentum_supports_entry,
    sma_trend_supports_entry,
    update_recent_tick_history,
    update_volume_delta,
    volatility_scalp_vwap_supports_entry,
    vwap,
    vwap_supports_entry,
)
from webull_bot.strategy_logic.regime.trend_signals import (
    _ema,
    option_direction_signal,
    option_entry_confirmed,
    tick_direction_ok,
    tick_direction_score,
    trend_signal,
)
from webull_bot.strategy_logic.types import Decision, PortfolioDecision


class TradingStrategy:
    """Owns selection, sizing, entry, exit, and portfolio policy."""

    def __init__(self, config):
        if config.ema_fast_period >= config.ema_slow_period:
            raise ValueError("fast EMA must be lower than slow EMA")
        self.config = config
        self.history = defaultdict(
            lambda: deque(maxlen=config.ema_slow_period + 1)
        )
        self.activity: dict[str, float] = {}
        self.prices: dict[str, Decimal] = {}
        self.metrics: dict[str, dict] = {}
        self.selection_buckets: dict[str, str] = {}
        self.trend_streak: dict[str, int] = {}
        self.vwap_state: dict[str, dict] = {}
        self.crossover_counts: dict[str, int] = defaultdict(int)
        self.tick_history: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=config.tick_direction_window)
        )
        # Higher-timeframe SMA trend reference (real daily-bar closes, not
        # derived from tick polls) - refreshed once daily by
        # AutoTrader.refresh_sma_trend, deliberately NOT cleared by
        # clear_market_state's once-daily reset so a failed refresh keeps
        # yesterday's (still roughly valid) SMA rather than going empty.
        self.sma_trend: dict[str, Decimal] = {}
        # Percent net change over the last RECENT_MOMENTUM_LOOKBACK_
        # MINUTES minutes, refreshed by AutoTrader.refresh_recent_
        # momentum - see recent_momentum_supports_entry. Same "not
        # cleared by the once-daily reset" convention as sma_trend
        # above, but refreshed much more often (minutes, not days).
        self.recent_momentum: dict[str, Decimal] = {}
        # Real daily-bar closes, newest-first, refreshed by AutoTrader.
        # refresh_multi_day_momentum - see multi_day_momentum_supports_
        # entry. Same "not cleared by the once-daily reset" convention
        # as sma_trend above.
        self.daily_closes: dict[str, list[float]] = {}
        # Webull's most-active screener, refreshed independently every
        # MARKET_PULSE_REFRESH_SECONDS by AutoTrader.refresh_market_pulse -
        # not tied to clear_market_state's once-daily reset, same as
        # market_pulse_cache itself isn't.
        self.most_active_symbols: set[str] = set()
        # Analyst target-price/rating soft priority nudge, refreshed
        # independently (and much more slowly) by AutoTrader's
        # AnalystDataService - see analyst_priority_bonus. Not tied to
        # clear_market_state's once-daily reset, same as most_active_symbols
        # and sma_trend isn't: this is background-fetched on its own
        # gradual cadence and shouldn't be thrown away just because the
        # trading day rolled over.
        self.analyst_priority: dict[str, float] = {}
        # Volatility-scalp: a rolling window of raw prices per symbol
        # (independent of self.prices, which only ever holds the latest
        # one) - see update_stock_snapshot, realized_volatility_percent,
        # volatility_scalp_dip_signal.
        self.volatility_price_history: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=config.volatility_scalp_lookback_samples)
        )
        # By request: micro-exhaustion dip confirmation. A SEPARATE
        # structure from volatility_price_history above, deliberately -
        # that deque only gets appended once per symbol per SLOW scan
        # pass (confirmed live: 30-90s+ between passes), so its 20
        # samples span an irregular, unpredictable real-time window (a
        # few minutes to tens of minutes), not a clean short window.
        # Several already-tuned functions (dip_signal, momentum_
        # stalled_or_rising, eligibility) assume bare floats there;
        # retrofitting timestamps into it risks regressing all of them.
        # This new deque stores (monotonic_timestamp, price) tuples
        # instead, so velocity/wick math below can filter by ACTUAL
        # elapsed wall-clock time regardless of how irregular the
        # append cadence is - see AutoTrader.update_recent_tick_history
        # and volatility_scalp_micro_exhaustion_confirmed.
        self.recent_tick_history: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=50)
        )
        # Per-symbol volume-delta tracking for the same confirmation
        # gate - Webull's snapshot volume is cumulative for the day, so
        # a meaningful "volume spike" has to be derived from the DELTA
        # between consecutive snapshots, smoothed into a rolling
        # baseline (a simple EMA). Updated by AutoTrader.update_volume_
        # delta, once per symbol per snapshot - deliberately NOT touched
        # by the gate-check function itself (see its docstring): the
        # existing diagnostic-visibility gate tuples in bot.py already
        # evaluate every condition TWICE per cycle (once for the real
        # gate, once for the log-only "why isn't this firing" summary),
        # so a stateful gate here would silently double-count every
        # cycle and corrupt the EMA.
        self.volume_delta_baseline: dict[str, Decimal] = {}
        self.volume_delta_ema: dict[str, Decimal] = {}
        # The most recent single-cycle delta itself (a spike candidate,
        # compared against volume_delta_ema's smoothed baseline) -
        # separate from the EMA so the gate isn't comparing the
        # baseline against itself.
        self.volume_delta_latest: dict[str, Decimal] = {}

    clear_market_state = clear_market_state
    rotating_batch = staticmethod(rotating_batch)
    quote_number = staticmethod(quote_number)
    update_stock_snapshot = update_stock_snapshot

    # Below this many samples, a stdev estimate is too noisy to trust -
    # not configurable (unlike the window length itself), same reasoning
    # as the other hardcoded quality-filter thresholds up top.
    VOLATILITY_SCALP_MIN_SAMPLES = 5
    # How many of the most recent samples count as the "local high" a dip
    # is measured against - see volatility_scalp_dip_signal.
    VOLATILITY_SCALP_LOCAL_HIGH_SAMPLES = 5

    realized_volatility_percent = realized_volatility_percent
    trend_efficiency_ratio = trend_efficiency_ratio
    symbol_regime = symbol_regime
    seed_volatility_window = seed_volatility_window
    is_volatility_scalp_eligible = is_volatility_scalp_eligible
    volatility_scalp_dip_signal = volatility_scalp_dip_signal
    volatility_scalp_momentum_stalled_or_rising = volatility_scalp_momentum_stalled_or_rising
    volatility_scalp_momentum_stalling = volatility_scalp_momentum_stalling
    volatility_scalp_momentum_stalling_short = volatility_scalp_momentum_stalling_short
    _synthetic_bars = _synthetic_bars
    heikin_ashi_bullish_reversal_signal = heikin_ashi_bullish_reversal_signal
    dual_thrust_breakout_signal = dual_thrust_breakout_signal
    _parabolic_sar = staticmethod(_parabolic_sar)
    parabolic_sar_exit_signal = parabolic_sar_exit_signal
    volatility_scalp_exit_override = volatility_scalp_exit_override
    volatility_scalp_average_down_signal = volatility_scalp_average_down_signal
    averaging_down_capacity = averaging_down_capacity
    volatility_scalp_partial_exit_quantity = volatility_scalp_partial_exit_quantity
    volatility_scalp_share_count = volatility_scalp_share_count
    volatility_scalp_target_price = volatility_scalp_target_price

    _update_vwap = _update_vwap
    vwap = vwap
    vwap_supports_entry = vwap_supports_entry
    volatility_scalp_vwap_supports_entry = volatility_scalp_vwap_supports_entry
    sma_trend_supports_entry = sma_trend_supports_entry
    recent_momentum_supports_entry = recent_momentum_supports_entry
    multi_day_momentum_supports_entry = multi_day_momentum_supports_entry
    update_recent_tick_history = update_recent_tick_history
    update_volume_delta = update_volume_delta

    volatility_scalp_micro_exhaustion_confirmed = volatility_scalp_micro_exhaustion_confirmed

    relative_strength_index = relative_strength_index
    rsi_supports_entry = rsi_supports_entry
    rsi_overbought_exit = rsi_overbought_exit

    priority_score = priority_score
    analyst_priority_bonus = staticmethod(analyst_priority_bonus)
    stock_scan_concurrent_batches = stock_scan_concurrent_batches
    prioritized_stock_batch = prioritized_stock_batch
    selection_bucket = selection_bucket
    research_candidates = research_candidates

    _ema = staticmethod(_ema)
    trend_signal = trend_signal
    option_direction_signal = option_direction_signal
    option_entry_confirmed = option_entry_confirmed
    tick_direction_score = tick_direction_score
    tick_direction_ok = tick_direction_ok
    obi_supports_entry = staticmethod(obi_supports_entry)
    option_delta_ok = staticmethod(option_delta_ok)
    option_iv_percentile_ok = staticmethod(option_iv_percentile_ok)
    option_market_regime_ok = staticmethod(option_market_regime_ok)
    stock_market_regime_ok = staticmethod(stock_market_regime_ok)

    adaptive_stop_percent = adaptive_stop_percent
    stock_decision = stock_decision
    _exit_bias = _exit_bias
    entry_spread_ok = entry_spread_ok
    volatility_scalp_entry_spread_ok = volatility_scalp_entry_spread_ok
    entry_extension_ok = entry_extension_ok
    research_supports_entry = staticmethod(research_supports_entry)
    option_decision = option_decision
    minimum_lot_size = staticmethod(minimum_lot_size)
    exit_blocked_by_lot_restriction = classmethod(exit_blocked_by_lot_restriction)
    risk_based_share_count = risk_based_share_count
    stock_order_quantity = stock_order_quantity
    fractional_stock_quantity = fractional_stock_quantity
    dollar_stock_quantity = dollar_stock_quantity
    option_order_quantity = option_order_quantity
    option_average_down_signal = option_average_down_signal

    open_position_count = staticmethod(open_position_count)
    position_unrealized_pnl = position_unrealized_pnl
    position_day_pnl = position_day_pnl
    portfolio_decision = staticmethod(portfolio_decision)
