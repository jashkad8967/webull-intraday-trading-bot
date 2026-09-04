import logging
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from zoneinfo import ZoneInfo

from rich.logging import RichHandler

from webull_bot.analyst_data import AnalystDataService
from webull_bot.commands import CommandQueue
from webull_bot.config import settings
from webull_bot.daily_pnl import DailyPnlTracker
from webull_bot.errors.broker_conflict import is_broker_position_conflict
from webull_bot.errors.fractional_ticker import is_fractional_ticker_unsupported
from webull_bot.errors.fractional_trading import is_fractional_trading_not_enabled
from webull_bot.errors.order_cancellation import is_order_not_cancelable
from webull_bot.errors.short_selling import is_short_selling_unsupported
from webull_bot.errors.symbol_restrictions import is_symbol_restricted_to_closing_only
from webull_bot.invalid_symbols import InvalidSymbolTracker
from webull_bot.market_agent import MarketResearchAgent
from webull_bot.pairs import (
    PAIRS,
    PAIRS_CAPITAL_FRACTION,
    PAIRS_MAX_CONCURRENT,
    PairsStrategy,
)
from webull_bot.risk.entry_blackout import fresh_entry_blackout_active
from webull_bot.risk.profit_target_multiplier import profit_target_multiplier
from webull_bot.risk.stop_tighten_multiplier import stop_tighten_multiplier
from webull_bot.sizing.diversification_budget import (
    diversification_capped_entry_budget,
)
from webull_bot.sizing.fractional_quantity import is_fractional_quantity
from webull_bot.sizing.fractional_slots import max_fractional_position_slots
from webull_bot.sizing.stock_entry_sizing import size_stock_entry
from webull_bot.status import StatusWriter
from webull_bot.strategy import (
    OBI_DEPTH_LEVELS,
    OPTION_VIXY_SYMBOL,
    Decision,
    TradingStrategy,
)
from webull_bot.trade_events import TradeEventStreamService
from webull_bot.trading.guards.daily_loss_breaker import handle_daily_loss_breaker
from webull_bot.trading.guards.order_error_guard import (
    CONSECUTIVE_ORDER_ERROR_LIMIT,
    ORDER_ERROR_WINDOW_SECONDS,
    record_order_error,
)
from webull_bot.trading.guards.portfolio_circuit_breaker import (
    handle_portfolio_circuit_breaker,
)
from webull_bot.trading.guards.post_stop_reentry import post_stop_reentry_ready
from webull_bot.trading.guards.price_sanity import (
    price_sanity_cooldown_ready,
    price_sanity_ok,
)
from webull_bot.trading.guards.stop_loss_guard import stop_loss_guard_active
from webull_bot.trading.guards.symbol_quarantine import symbol_quarantined
from webull_bot.trading.handlers.broker_conflict_check import _broker_conflict
from webull_bot.trading.handlers.broker_conflict_handler import handle_broker_conflict
from webull_bot.trading.handlers.fractional_ticker_handler import (
    handle_fractional_ticker_unsupported,
)
from webull_bot.trading.handlers.fractional_trading_handler import (
    handle_fractional_trading_not_enabled,
)
from webull_bot.trading.handlers.short_selling_handler import (
    handle_short_selling_unsupported,
)
from webull_bot.trading.handlers.symbol_restriction_handler import (
    handle_symbol_restricted_to_closing_only,
)
from webull_bot.trading.momentum.historical_volatility_filter import (
    filter_by_historical_volatility,
)
from webull_bot.trading.momentum.multi_day_momentum_refresh import (
    refresh_multi_day_momentum,
)
from webull_bot.trading.momentum.recent_momentum_refresh import (
    refresh_recent_momentum,
)
from webull_bot.trading.momentum.scalp_cohort_selection import (
    select_volatility_scalp_symbols,
)
from webull_bot.trading.momentum.volatility_window_seeding import (
    seed_volatility_windows,
)
from webull_bot.trading.options.option_contract_discovery import (
    discover_option_contracts,
)
from webull_bot.trading.orders.close_instruments import close_instruments
from webull_bot.trading.orders.exit_failure_tracking import _note_exit_failure
from webull_bot.trading.orders.force_market_exit import should_force_market_exit
from webull_bot.trading.orders.iceberg_order_processing import (
    process_iceberg_orders,
)
from webull_bot.trading.orders.locks import _rekey_working_order, _working_orders_lock
from webull_bot.trading.orders.manual_buy import _manual_buy
from webull_bot.trading.orders.manual_cancel_order import _manual_cancel_order
from webull_bot.trading.orders.manual_sell import _manual_sell
from webull_bot.trading.orders.manual_touch import _manual_touch_active
from webull_bot.trading.orders.order_history_reconciliation import (
    reconcile_order_history,
)
from webull_bot.trading.orders.order_monitoring import monitor_working_orders
from webull_bot.trading.orders.pending_order_release import _release_pending_order
from webull_bot.trading.orders.phantom_exit_confirmation import (
    _reverse_if_never_filled,
)
from webull_bot.trading.orders.position_protection_loop import (
    _position_protection_loop,
)
from webull_bot.trading.orders.rate_limit_retry import (
    _is_rate_limited,
    _retry_once_on_rate_limit,
)
from webull_bot.trading.orders.realized_pnl_tracking import (
    record_realized_exit,
    reverse_phantom_exit,
)
from webull_bot.trading.orders.scaled_order_placement import (
    HARD_ORDER_NOTIONAL_CEILING,
    ICEBERG_MIN_SHARES,
    ICEBERG_SLICE_INTERVAL_SECONDS,
    ICEBERG_SLICE_SHARES,
    place_stock_scaled,
)
from webull_bot.trading.orders.stop_loss_confirmation import (
    stop_loss_confirmed,
    stop_ready_to_submit,
)
from webull_bot.trading.orders.trade_recording import record_trade
from webull_bot.trading.orders.ui_command_dispatch import process_ui_commands
from webull_bot.trading.orders.watchlist import add_to_watchlist
from webull_bot.trading.quoting.batched_quotes import _batched_quotes
from webull_bot.trading.quoting.stall_equity_quotes import _stall_equity_quotes
from webull_bot.trading.quoting.stall_exit_price import _stall_exit_price
from webull_bot.trading.repricing.resting_entry_repricer import (
    reprice_resting_entries,
)
from webull_bot.trading.repricing.resting_exit_repricer import reprice_resting_exits
from webull_bot.trading.repricing.scalp_entry_repricer import (
    reprice_volatility_scalp_entries,
)
from webull_bot.trading.repricing.scalp_exit_repricer import (
    reprice_volatility_scalp_exits,
)
from webull_bot.trading.scalp.scalp_entry_price import volatility_scalp_entry_price
from webull_bot.trading.scalp.scalp_position_exposure import (
    volatility_scalp_position_value_ok,
)
from webull_bot.trading.scalp.scalp_reentry import volatility_scalp_reentry_ready
from webull_bot.trading.scalp.scalp_total_exposure import (
    volatility_scalp_total_exposure_ok,
)
from webull_bot.trading.screeners.agent_assessment_stub import agent_assessment
from webull_bot.trading.screeners.agent_discoveries import refresh_agent_discoveries
from webull_bot.trading.screeners.agent_predicted_gainers_refresh import (
    refresh_agent_predicted_gainers,
)
from webull_bot.trading.screeners.market_pulse_entries import _market_pulse_entries
from webull_bot.trading.screeners.market_pulse_refresh import refresh_market_pulse
from webull_bot.trading.screeners.premarket_gainers_refresh import (
    refresh_premarket_gainers,
)
from webull_bot.trading.screeners.screener_market_pulse_active import (
    safe_market_pulse_active,
)
from webull_bot.trading.screeners.screener_premarket_gainers import (
    safe_premarket_gainers,
)
from webull_bot.trading.screeners.screener_top_gainers import safe_top_gainers
from webull_bot.trading.screeners.screener_top_losers import safe_top_losers
from webull_bot.trading.screeners.strategy_review import submit_strategy_review
from webull_bot.trading.sweeps.extended_hours_profit_sweep import (
    close_profitable_positions_during_extended_hours,
)
from webull_bot.trading.sweeps.fractional_pre_close_sweep import (
    close_fractional_positions_before_core_close,
)
from webull_bot.trading.sweeps.stall_position_boost import boost_stalled_positions
from webull_bot.trading.universe.overnight_hold import overnight_hold_symbols
from webull_bot.trading.universe.pairs_symbol_exclusion import exclude_pairs_symbols
from webull_bot.trading.universe.popular_reinstatement import (
    filter_with_popular_reinstated,
)
from webull_bot.trading.universe.resolve_targets import (
    _resolve_targets_work,
    resolve_targets,
)
from webull_bot.trading.universe.sma_trend_refresh import refresh_sma_trend
from webull_bot.trading.universe.snapshot_batch_capping import (
    cap_batch_to_snapshot_limit,
)
from webull_bot.trading.universe.symbol_universe_backfill import (
    backfill_stock_symbols,
)
from webull_bot.trading.universe.universe_download import (
    _download_and_filter_universe,
)
from webull_bot.trading.universe.universe_growth import _grow_stock_universe
from webull_bot.trading.universe.universe_resolution_body import (
    _resolve_targets_work_body,
)
from webull_bot.trading.util.account_state import account_state
from webull_bot.trading.util.clock import is_trading_day, now, session_moment
from webull_bot.trading.util.compact_number import _compact_number
from webull_bot.trading.util.concurrent_dispatch import (
    _POSITION_PROTECTION_MAX_WORKERS,
    _dispatch_concurrently,
)
from webull_bot.trading.util.cooldowns import (
    cooldown_ready,
    has_pending_buy_order,
    rate_capped,
    reentry_cooldown_ready,
)
from webull_bot.trading.util.day_end_summary import log_day_end_summary
from webull_bot.trading.util.idle_cash_ramp import idle_cash_ramp_progress
from webull_bot.trading.util.order_book_imbalance import _quote_size, obi_score_for
from webull_bot.trading.util.status_snapshot import write_status_snapshot
from webull_bot.trading.util.trade_event_logging import log_trade_events
from webull_bot.wash_sale import WashSaleTracker
from webull_bot.webull_api import (
    MarketDataPermissionError,
    QuoteUnavailableError,
    WebullAPI,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[
        RichHandler(
            show_time=True,
            show_level=True,
            show_path=False,
            markup=False,
            log_time_format="%H:%M:%S",
            omit_repeated_times=False,
        )
    ],
)
log = logging.getLogger("webull-bot")


class AutoTrader:
    # Pure Webull-error classifiers and the fractional-quantity check -
    # moved out to their own single-purpose files under errors/ and
    # sizing/ (see each file's own docstring for what live incident it
    # traces back to), bound here as staticmethods so every existing
    # self.is_x_y(...) call site elsewhere in this class keeps working
    # unchanged.
    is_broker_position_conflict = staticmethod(is_broker_position_conflict)
    is_fractional_quantity = staticmethod(is_fractional_quantity)
    is_fractional_ticker_unsupported = staticmethod(is_fractional_ticker_unsupported)
    is_fractional_trading_not_enabled = staticmethod(is_fractional_trading_not_enabled)
    is_order_not_cancelable = staticmethod(is_order_not_cancelable)
    is_short_selling_unsupported = staticmethod(is_short_selling_unsupported)
    is_symbol_restricted_to_closing_only = staticmethod(
        is_symbol_restricted_to_closing_only
    )
    # Same pattern, moved out to risk/ and sizing/ - pure sizing/risk
    # multiplier math, no shared instance state.
    diversification_capped_entry_budget = staticmethod(
        diversification_capped_entry_budget
    )
    fresh_entry_blackout_active = staticmethod(fresh_entry_blackout_active)
    max_fractional_position_slots = staticmethod(max_fractional_position_slots)
    profit_target_multiplier = staticmethod(profit_target_multiplier)
    stop_tighten_multiplier = staticmethod(stop_tighten_multiplier)
    # These take self (they read config/instance state), so they're
    # assigned directly - not staticmethod-wrapped - which is enough
    # for Python's normal descriptor protocol to bind them as regular
    # instance methods, same as if they'd been defined in the class
    # body directly. Moved out to trading/ (see each file's own
    # docstring).
    now = now
    is_trading_day = is_trading_day
    session_moment = session_moment
    cooldown_ready = cooldown_ready
    reentry_cooldown_ready = reentry_cooldown_ready
    rate_capped = rate_capped
    has_pending_buy_order = has_pending_buy_order
    # Broker-error handlers, each paired with its errors/ classifier -
    # moved out to trading/*_handler.py.
    handle_broker_conflict = handle_broker_conflict
    handle_fractional_trading_not_enabled = handle_fractional_trading_not_enabled
    handle_fractional_ticker_unsupported = handle_fractional_ticker_unsupported
    handle_short_selling_unsupported = handle_short_selling_unsupported
    handle_symbol_restricted_to_closing_only = handle_symbol_restricted_to_closing_only
    should_force_market_exit = should_force_market_exit
    stop_ready_to_submit = stop_ready_to_submit
    stop_loss_confirmed = stop_loss_confirmed
    # Volatility-scalp cohort small helpers - moved out to trading/scalp_*.py.
    volatility_scalp_entry_price = volatility_scalp_entry_price
    volatility_scalp_reentry_ready = volatility_scalp_reentry_ready
    volatility_scalp_position_value_ok = volatility_scalp_position_value_ok
    volatility_scalp_total_exposure_ok = volatility_scalp_total_exposure_ok
    # Pairs-symbol exclusion, screener safety wrappers, and small number/
    # formatting helpers - moved out to trading/*.py, one function per file.
    exclude_pairs_symbols = staticmethod(exclude_pairs_symbols)
    safe_top_gainers = safe_top_gainers
    safe_premarket_gainers = safe_premarket_gainers
    safe_top_losers = safe_top_losers
    safe_market_pulse_active = safe_market_pulse_active
    _market_pulse_entries = staticmethod(_market_pulse_entries)
    _compact_number = staticmethod(_compact_number)
    # Small standalone helpers (idle-cash ramp, agent stubs/discoveries,
    # OBI scoring, realized-pnl bookkeeping, price sanity, DAIC post-stop
    # cooldown, watchlist, day-end/trade-event logging, universe backfill/
    # overnight-hold/batch-capping) - moved out to trading/*.py, one
    # function (or a tightly-coupled pair) per file.
    idle_cash_ramp_progress = idle_cash_ramp_progress
    agent_assessment = agent_assessment
    _quote_size = staticmethod(_quote_size)
    obi_score_for = obi_score_for
    refresh_agent_discoveries = refresh_agent_discoveries
    record_realized_exit = record_realized_exit
    reverse_phantom_exit = reverse_phantom_exit
    price_sanity_ok = price_sanity_ok
    price_sanity_cooldown_ready = price_sanity_cooldown_ready
    post_stop_reentry_ready = post_stop_reentry_ready
    log_trade_events = log_trade_events
    backfill_stock_symbols = backfill_stock_symbols
    overnight_hold_symbols = overnight_hold_symbols
    add_to_watchlist = add_to_watchlist
    log_day_end_summary = log_day_end_summary
    cap_batch_to_snapshot_limit = staticmethod(cap_batch_to_snapshot_limit)
    refresh_sma_trend = refresh_sma_trend
    # Order-exit bookkeeping, popular-symbol reinstatement, market/agent
    # screener refreshers, account-wide and per-symbol trading guards, and
    # dashboard/manual command handling - moved out to trading/orders,
    # trading/universe, trading/screeners, and trading/guards.
    _release_pending_order = _release_pending_order
    _note_exit_failure = _note_exit_failure
    _reverse_if_never_filled = _reverse_if_never_filled
    filter_with_popular_reinstated = filter_with_popular_reinstated
    refresh_market_pulse = refresh_market_pulse
    refresh_premarket_gainers = refresh_premarket_gainers
    refresh_agent_predicted_gainers = refresh_agent_predicted_gainers
    stop_loss_guard_active = stop_loss_guard_active
    symbol_quarantined = symbol_quarantined
    close_instruments = close_instruments
    _manual_cancel_order = _manual_cancel_order
    process_ui_commands = process_ui_commands
    # Daily universe-resolution pipeline (non-blocking dispatch, download/
    # filter, background growth, and the full-day-reset work body) - moved
    # out to trading/universe/*.py.
    resolve_targets = resolve_targets
    _resolve_targets_work = _resolve_targets_work
    _download_and_filter_universe = _download_and_filter_universe
    _grow_stock_universe = _grow_stock_universe
    _resolve_targets_work_body = _resolve_targets_work_body
    # Momentum/volatility scanning: historical-volatility filtering,
    # recent/multi-day momentum refresh, volatility-window bar seeding,
    # and volatility-scalp cohort selection - moved out to trading/momentum/.
    filter_by_historical_volatility = filter_by_historical_volatility
    refresh_recent_momentum = refresh_recent_momentum
    refresh_multi_day_momentum = refresh_multi_day_momentum
    seed_volatility_windows = seed_volatility_windows
    select_volatility_scalp_symbols = select_volatility_scalp_symbols
    # Circuit-breaker guards - moved out to trading/guards/.
    handle_portfolio_circuit_breaker = handle_portfolio_circuit_breaker
    handle_daily_loss_breaker = handle_daily_loss_breaker
    record_order_error = record_order_error
    # Resting-order repricing (scalp entries/exits and general resting
    # entries/exits) - moved out to trading/repricing/.
    reprice_volatility_scalp_entries = reprice_volatility_scalp_entries
    reprice_resting_entries = reprice_resting_entries
    reprice_volatility_scalp_exits = reprice_volatility_scalp_exits
    reprice_resting_exits = reprice_resting_exits
    # Manual dashboard buy/sell command execution - moved out to
    # trading/orders/.
    _manual_sell = _manual_sell
    _manual_buy = _manual_buy
    # Batched-quote helpers used across repricers and stall detection -
    # moved out to trading/quoting/.
    _batched_quotes = _batched_quotes
    _stall_equity_quotes = _stall_equity_quotes
    _stall_exit_price = _stall_exit_price
    # Profitable-position closing sweeps (fractional pre-core-close,
    # extended-hours) - moved out to trading/sweeps/.
    close_fractional_positions_before_core_close = (
        close_fractional_positions_before_core_close
    )
    close_profitable_positions_during_extended_hours = (
        close_profitable_positions_during_extended_hours
    )
    # Account-state refresh, stock entry sizing, and agent strategy
    # review - moved out to trading/util/, sizing/, and trading/screeners/
    # respectively.
    account_state = account_state
    size_stock_entry = size_stock_entry
    submit_strategy_review = submit_strategy_review
    # Iceberg/scaled order placement and its slice-processing follow-up -
    # moved out to trading/orders/.
    place_stock_scaled = place_stock_scaled
    process_iceberg_orders = process_iceberg_orders
    # Order-history reconciliation audit - moved out to trading/orders/.
    reconcile_order_history = reconcile_order_history
    # Core trade-lifecycle bookkeeping - moved out to trading/orders/.
    record_trade = record_trade
    # Options-chain discovery - moved out to trading/options/.
    discover_option_contracts = discover_option_contracts
    # Working-order fill/manual-action monitoring and stale-order
    # cancellation - moved out to trading/orders/.
    monitor_working_orders = monitor_working_orders
    # Fast-cadence position-protection background thread - moved out to
    # trading/orders/.
    _position_protection_loop = _position_protection_loop
    # Stall-breaker profit sweep and dashboard status-snapshot writing -
    # moved out to trading/sweeps/ and trading/util/ respectively.
    boost_stalled_positions = boost_stalled_positions
    write_status_snapshot = write_status_snapshot

    def __init__(self):
        self.config = settings()
        self.config.validate_runtime()
        self.api = WebullAPI(self.config)
        self.strategy = TradingStrategy(self.config)
        self.market_agent = (
            MarketResearchAgent(self.config, log)
            if self.config.agent_enabled
            else None
        )
        self.analyst_service = (
            AnalystDataService(self.api, self.config, log)
            if self.config.analyst_priority_enabled
            else None
        )
        self.trade_event_service = (
            TradeEventStreamService(self.config, log)
            if self.config.event_stream_enabled
            else None
        )
        self.timezone = ZoneInfo(self.config.trading_timezone)
        self.wash_sales = WashSaleTracker(
            self.config.wash_sale_state_file,
            self.config.wash_sale_block_days,
            self.timezone,
            log,
        )
        self.daily_pnl = DailyPnlTracker(
            self.config.daily_pnl_state_file,
            self.timezone,
            log,
        )
        self.invalid_symbols = InvalidSymbolTracker(
            self.config.invalid_symbol_state_file,
            log,
        )
        self.wash_skip_logged: set[str] = set()
        self.unmanaged_held_logged: set[str] = set()
        # See close_profitable_positions_during_extended_hours - dedupes
        # its "skipping a fractional position outside core hours" log so
        # it fires once per symbol per occurrence, not every ~90s cycle
        # for the whole pre-market/after-hours window.
        self.extended_hours_fractional_skip_logged: set[str] = set()
        self.last_trade: dict[str, float] = {}
        self.last_exit_at: dict[str, float] = {}
        # See post_stop_reentry_ready - keyed by bare symbol (not the
        # "STOCK:SYMBOL" key), stamped only on a STOP-type record_trade.
        self.last_volatility_stop_loss_at: dict[str, float] = {}
        # See volatility_scalp_partial_exit_quantity - keyed by bare
        # symbol, the price of the most recent partial-exit sale on an
        # open position, so the ladder only fires again after another
        # VOLATILITY_SCALP_PARTIAL_EXIT_REPRICE_PERCENT move ("sell 5
        # every 5 cents it goes up"), not on every 0.25s cycle.
        self.volatility_scalp_last_partial_exit_price: dict[str, Decimal] = {}
        self.trade_times: dict[str, deque] = defaultdict(deque)
        self.status = StatusWriter(
            self.config.status_file,
            state_file=self.config.trade_history_state_file,
        )
        self.last_status_write = 0.0
        # Coarser than last_status_write on purpose - a chart doesn't need
        # a point every poll cycle (0.25s), just enough to look live.
        self.last_balance_history_write = 0.0
        self.stock_symbols: list[str] = []
        self.reserve_symbols: list[str] = []
        self.stock_categories: dict[str, str] = {}
        self.invalid_stock_symbols: set[str] = set()
        self.option_contracts: list[dict] = []
        self.pending_stock_exits: set[str] = set()
        self.pending_option_exits: set[str] = set()
        # Symbols currently held via the volatility-scalp dip-buy path -
        # see trade_stocks' dip-entry block and the quick-target override
        # applied to these positions' exit decision below.
        self.volatility_scalp_positions: set[str] = set()
        # The curated daily cohort (see select_volatility_scalp_symbols) -
        # a small priority subset still used for prioritized batch
        # scanning/dashboard display, but no longer what entries/exits
        # are actually gated on (see is_volatility_scalp_eligible calls
        # in trade_stocks - condensed onto eligibility alone).
        self.volatility_scalp_symbols: set[str] = set()
        # Every symbol that was volatility-scalp eligible the last time
        # it was scanned, persisted across cycles (cleared once daily by
        # clear_market_state, same as the cohort). Same "force into every
        # cycle's batch" treatment as self.volatility_scalp_symbols
        # below, extended to the FULL broadened set - by request, "make
        # sure the data is received as frequently as possible": a
        # symbol that qualified once shouldn't fall back to slow
        # rotating-batch cadence just because it isn't in the curated
        # top handful.
        self.volatility_scalp_recently_eligible: set[str] = set()
        # How many averaging-down buys a currently-held cohort position
        # has already made (see AutoTrader's averaging-buy entry block
        # and TradingStrategy.volatility_scalp_average_down_signal) -
        # capped by volatility_scalp_max_averaging_buys, reset to 0 the
        # moment the position fully closes.
        self.volatility_scalp_average_down_count: dict[str, int] = defaultdict(int)
        self.last_volatility_average_down: dict[str, float] = {}
        # The price actually used for a symbol's most recent volatility-
        # scalp buy (fresh entry OR averaging-down) - by request, "when
        # you average down, you buy at a lower price, not the same
        # price." volatility_scalp_average_down_signal alone only checks
        # the price against the position's BLENDED average cost, which a
        # repeated buy at the same price barely moves - so the same
        # price could keep re-qualifying as "X% below average cost"
        # indefinitely without ever making a genuinely new, lower low.
        # This tracks the actual last fill price and requires a strictly
        # lower one before averaging down again. Reset the moment the
        # position fully closes.
        self.volatility_scalp_last_buy_price: dict[str, Decimal] = {}
        # -inf, not 0.0: time.monotonic() starts near zero at process
        # boot too, so a 0.0 default would silently throttle the very
        # first selection until VOLATILITY_SCALP_RESELECT_SECONDS
        # (default 30 min) into every run, leaving the cohort empty that
        # whole time for no reason.
        self.last_volatility_symbol_selection = float("-inf")
        # Broker-side "closing orders only" restriction (see
        # is_symbol_restricted_to_closing_only) - deliberately NOT
        # broker_conflict_symbols, which skips a symbol's exit management
        # entirely too. A close-only restriction means the opposite: new
        # entries are blocked but exits must keep working normally for
        # any position already held.
        self.entry_restricted_symbols: set[str] = set()
        # By request: "when i touch a stock stop doing anything with it
        # while i am there." Stamped by record_trade on any MANUAL_BUY/
        # MANUAL_SELL - see manual_touch_active and manual_touch_
        # pause_seconds.
        self.manual_touch_at: dict[str, float] = {}
        self.stock_cursor = 0
        # The real number of symbols trade_stocks actually fetched
        # quotes for last cycle - can be several STOCK_BATCH_SIZE
        # multiples now (see stock_scan_concurrent_batches), not always
        # a flat STOCK_BATCH_SIZE. Read by the SCAN status log instead
        # of recomputing a static min(stock_batch_size, ...), which
        # would otherwise misreport real per-cycle scan coverage once
        # the universe is large enough for more than one concurrent
        # batch.
        self.last_scan_batch_size = 0
        self.option_cursor = 0
        self.option_discovery_cursor = 0
        self.option_discovery_attempted: set[str] = set()
        self.discover_all_options = False
        self.option_iv_history: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=30)
        )
        self.vixy_history: deque = deque(maxlen=30)
        self.options_enabled = True
        self.resolved_date = None
        # See resolve_targets - tracks which date's slow, one-time
        # universe/VOLFILT/SMA refresh is currently running on its
        # background thread, so a cycle mid-way through that ~15-20
        # minute window (at the current 5000-symbol universe size)
        # doesn't kick off a second, redundant thread every cycle.
        self._resolve_targets_in_progress_for = None
        # Live incident (VVOS): the day's very first volatility-scalp
        # dip entries can fire before _resolve_targets_work_body's bulk
        # seed_volatility_windows call (also gated behind resolve_
        # targets' background thread) has populated real history for
        # the symbol - volatility_scalp_momentum_stalled_or_rising fails
        # OPEN (doesn't block) below 3 samples, so those first-of-the-
        # day entries get NO real momentum-stall confirmation at all,
        # just the raw dip-percent check - VVOS entered while still
        # actively falling, not on a confirmed low. False until the
        # bulk seed completes for moment.date(); see the new "scalp -
        # daily volatility window seed still in progress" fresh-entry
        # gate in trade_stocks.
        self.volatility_windows_seeded_date = None
        self.last_close_attempt = 0.0
        self.last_fractional_sweep = 0.0
        self.last_extended_hours_profit_sweep = 0.0
        self.last_status_log = 0.0
        self.opening_grace_logged_date = None
        self.last_option_discovery = 0.0
        self.last_account_refresh = 0.0
        self.last_order_monitor = 0.0
        self.last_reprice = 0.0
        self.last_volatility_reprice = 0.0
        self.last_volatility_entry_reprice = 0.0
        self.last_held_exit_scan = 0.0
        self.last_entry_reprice = 0.0
        self.last_recent_momentum_refresh = 0.0
        self.last_multi_day_momentum_refresh = 0.0
        self.last_stall_boost = 0.0
        # See idle_cash_ramp_progress()/record_trade() - tracks how long
        # cash has sat above MIN_CASH_RESERVE_DOLLARS with nothing bought,
        # to progressively relax entry quality gates the longer it sits.
        self.last_capital_deployed_at = time.monotonic()
        # freqtrade-style StoplossGuard - see stop_loss_guard_active().
        # Tracks recent STOP-exit timestamps to pause new entries (only -
        # never liquidates, unlike handle_portfolio_circuit_breaker) if
        # too many fire within a lookback window.
        self.recent_stop_losses: deque = deque()
        self.stop_loss_guard_until = 0.0
        # freqtrade-style LowProfitPairs - see symbol_quarantined(). Same
        # shape as the stop-loss guard above but partitioned per "key"
        # (e.g. "STOCK:AAPL") instead of account-wide.
        self.symbol_pnl_history: dict[str, deque] = defaultdict(deque)
        self.symbol_quarantine_until: dict[str, float] = {}
        # Time-aware stop - see TradingStrategy.adaptive_stop_percent and
        # trade_stocks/trade_options. Set on every BUY/SHORT fill, cleared
        # on exit, so a fresh entry always starts its own noise-grace
        # window regardless of how long the symbol was previously held.
        self.position_opened_at: dict[str, float] = {}
        self.cached_buying_power = Decimal("0")
        # See account_state - option sizing/affordability must use
        # this, never cached_buying_power (a separate, stock-only pool).
        self.cached_option_buying_power = Decimal("0")
        self.cached_raw_buying_power = Decimal("0")
        self.cached_positions: list[dict] = []
        # Read by _position_protection_loop's background thread - see
        # its docstring and run()'s "self.cached_core_session_active =
        # core_session_active" assignment.
        self.cached_core_session_active = False
        # By request: "we want entry and profit to be quicker." These
        # mirror cached_core_session_active above - trade_stocks (main
        # thread) computes each of these once per cycle already; the
        # new evaluate_held_stock_exits (background thread, see
        # _position_protection_loop) reads the cached copies so a held
        # position's FIRST crossing into profit/loss territory (the
        # step that used to only get detected once every 30-90+ seconds
        # on the slow full-universe scan) can be detected and acted on
        # at the fast 0.25s cadence instead, without needing its own
        # separate, possibly-inconsistent recomputation of any of them.
        # Plain attribute assignment stays atomic under the GIL, same
        # convention as cached_core_session_active.
        self.cached_opening_grace_active = False
        self.cached_idle_relaxation_multiplier = Decimal("1")
        self.cached_idle_relaxation_amount = Decimal("0")
        self.cached_effective_core_session_active = False
        self.cached_profit_target_multiplier = Decimal("1")
        self.cached_stop_tighten_multiplier = Decimal("1")
        # Webull's own account-level today's total P&L, refreshed
        # alongside cached_buying_power/cached_positions in account_state
        # - see account_day_pnl_from_balance and write_status_snapshot's
        # dashboard total.
        self.cached_account_day_pnl: Decimal | None = None
        # Total net liquidation value (cash + market value of every held
        # position) - the dashboard's "Account Value" figure, distinct
        # from buying_power (spendable cash only).
        self.cached_account_value: Decimal | None = None
        self.working_orders: dict[str, dict] = {}
        # By request: "held positions should be checked every 0.25s
        # separately, the rest of the scan can take its own time" -
        # live evidence (CHOW) showed a single-threaded main loop lets
        # position-protection (fill/cancel detection, exit repricing,
        # stop-loss escalation) inherit the SLOW full-universe-scan
        # cadence (SCAN cycles observed 30-90s+ despite POLL_SECONDS=
        # 0.25), so a stuck exit order can sit unrefreshed for far
        # longer than intended before the next chance to reprice or
        # escalate it. Runs position protection on its own background
        # thread at the real poll_seconds cadence (see
        # _position_protection_loop/run) instead - this lock guards
        # self.working_orders (and the few sibling dicts touched
        # alongside it - stop_exit_submitted, stop_loss_escalated,
        # consecutive_exit_failures) since that thread and the main
        # thread's trade_stocks (via record_trade, for fresh entries)
        # now mutate them concurrently.
        self.working_orders_lock = threading.Lock()
        self.entries_paused = False
        self.circuit_breaker_time = 0.0
        self.last_circuit_research = 0.0
        self.last_day_end_log_date = None
        self.seed_popular_symbols: set[str] = set()
        self.agent_popular_symbols: set[str] = set()
        # See refresh_premarket_gainers - today's rank_type="PRE_MARKET"
        # screener results, refreshed once/day.
        self.premarket_gainers: set[str] = set()
        self.premarket_gainers_date = None
        # See refresh_agent_predicted_gainers - the research agent's
        # own speculative once/day pre-market gainer predictions.
        self.agent_predicted_gainers: set[str] = set()
        self.agent_predicted_gainers_date = None
        self.market_pulse_cache: dict[str, list[dict]] = {
            "gainers": [],
            "losers": [],
            "most_active": [],
        }
        self.last_market_pulse_refresh = 0.0
        # Symbols currently held short via the main strategy (see
        # trade_stocks' SHORT branch) - always flattened same-day
        # regardless of OVERNIGHT_HOLD_ENABLED, since a short's overnight
        # gap/squeeze risk is unbounded, unlike a long's. Separate from
        # position_buckets/ALWAYS_FLATTEN_BUCKETS since a short can land in
        # any selection bucket (POPULAR/PENNY/DISCOVERY/...), not a
        # dedicated one.
        self.short_symbols: set[str] = set()
        self.position_buckets: dict[str, str] = {}
        self.stop_exit_submitted: dict[str, float] = {}
        self.stop_loss_escalated: set[str] = set()
        # Counts consecutive never-filled exit attempts per symbol (see
        # reverse_phantom_exit's callers) - independent of, and a backstop
        # for, stop_loss_escalated: a symbol whose escalated order also
        # never fills (or whose escalation itself never fires) would
        # otherwise resubmit at the same or a slightly-repriced level
        # forever. At CONSECUTIVE_EXIT_FAILURE_MARKET_THRESHOLD, the next
        # attempt forces a genuine MARKET order - guaranteed to fill,
        # which structurally ends the loop instead of hoping a better
        # price eventually clears. Reset to 0 on a fresh BUY/SHORT.
        self.consecutive_exit_failures: dict[str, int] = defaultdict(int)
        # Monotonic timestamp of when a symbol's price first crossed into
        # stop-loss territory, continuously - see stop_loss_confirmed and
        # STOP_LOSS_CONFIRMATION_SECONDS. Popped the instant price recovers
        # above the stop level or the position closes, so a wick that
        # reverses never accumulates confirmation time toward a later,
        # unrelated breach.
        self.stop_condition_since: dict[str, float] = {}
        # Monotonic timestamp of a symbol's last price_sanity_ok
        # rejection - see price_sanity_cooldown_ready. Live
        # incident: one illiquid symbol's quote sat just past the sanity
        # tolerance and got retried (and re-rejected) on every single
        # scan cycle for hours, with nothing backing it off.
        self.price_sanity_rejected_at: dict[str, float] = {}
        # Every order_id the bot has itself submitted today (reset daily
        # in resolve_targets) - see reconcile_order_history. Deliberately
        # separate from status.trades, which is a fixed-size ring buffer
        # (TRADE_HISTORY, default 50) far too short to cover a full day's
        # worth of orders on a high-frequency account.
        self.submitted_order_ids_today: set[str] = set()
        # An order_id already logged as an unrecognized (likely manual)
        # order today - reconcile_order_history logs each one once per
        # day, not every reconciliation cycle.
        self.reconciliation_flagged_order_ids: set[str] = set()
        self.last_order_history_reconcile = 0.0
        self.daily_realized_loss = self.daily_pnl.realized_loss
        self.daily_realized_pnl = self.daily_pnl.realized_pnl
        self.daily_loss_breaker_triggered = False
        self.commands = CommandQueue(self.config.command_file)
        self.user_watchlist: set[str] = set(self.config.default_watchlist())
        # Symbols a dashboard "add to watchlist" command just added -
        # forced into the very next scan batch once, regardless of
        # priority ranking. See trade_stocks' injection right after
        # prioritized_stock_batch.
        self.priority_scan_symbols: set[str] = set()
        self.gate_rejections: dict[str, int] = defaultdict(int)
        # Separate from gate_rejections above - by request, "it is not
        # averaging down at all" - the averaging-down diagnostic (see
        # trade_stocks) otherwise gets crowded out of the top-5 GATES
        # summary by the far more numerous fresh-entry rejection
        # reasons every cycle. See the AVGDOWN log line in run().
        self.avgdown_gate_rejections: dict[str, int] = defaultdict(int)
        # By request: "scan through everything... figure out what you
        # missed" - live evidence showed real option CALL/PUT signals
        # firing constantly (169/186 cycles), zero orders ever placed,
        # and NO visibility into any of the gates between a signal and
        # an order (unlike the stock side's GATES summary). Own
        # dedicated counter, same "avgdown_gate_rejections" pattern -
        # would otherwise get crowded out of the shared gate_rejections
        # summary by the far more numerous stock-side reasons.
        self.option_gate_rejections: dict[str, int] = defaultdict(int)
        self.broker_conflict_symbols: set[str] = set()
        # Throttles the SANITY warning below so a persistently bad broker
        # read logs a periodic reminder instead of one line per scan cycle.
        self.cost_sanity_warned_at: dict[str, float] = {}
        self.fractional_trading_enabled = True
        self.fractional_unsupported_symbols: set[str] = set()
        self.short_selling_supported = True
        self.iceberg_orders: dict[str, dict] = {}
        self.order_error_times: deque = deque()
        self.pairs = PairsStrategy()
        self.pairs_positions: dict[tuple[str, str], dict] = {}
        self.last_pairs_sample = 0.0

    def evaluate_held_stock_exits(self) -> None:
        """By request: "we want entry and profit to be quicker." Detects
        a held LONG stock position's FIRST crossing into profit/loss
        territory - the step that previously only happened inside
        trade_stocks' slow, full-universe scan (live evidence: 30-90s+
        between cycles) - and places its exit order right here, on the
        fast poll_seconds cadence, instead of waiting for the scan to
        come back around. Once an order actually exists, it was already
        being actively managed fast (reprice_volatility_scalp_exits/
        reprice_resting_exits at 1s/5s, escalate_stalled_stop_losses at
        15s) - this closes the one remaining slow step: the very first
        detection.

        Deliberately narrow scope to keep this safe to run unreviewed
        on live capital: LONG stock positions only (no shorts/covers -
        their pricing/WASH-blocking differs enough that duplicating it
        here isn't worth the added risk for a first pass), whole-share
        only (no fractional lot-restriction edge cases). Anything
        outside this scope is simply skipped here and falls back to the
        exact same slow-loop handling as before - a pure addition,
        never a regression, since the slow loop's own handling for
        every position is completely unchanged and still runs.

        Reuses the exact same TradingStrategy.stock_decision/
        volatility_scalp_exit_override calls the slow loop uses (same
        cached multipliers - see cached_profit_target_multiplier's
        __init__ comment - so the decision computed here is identical
        to what the slow loop would eventually compute), and the same
        _stall_exit_price/stock_stop_exit_price pricing plus price-
        sanity/lot-restriction/stop-confirmation gates the slow loop
        already relies on - never a new pricing path, only the existing
        one running sooner. Also stamps stop_condition_since itself
        (mirroring the slow loop's own stamp) so the stop-loss
        confirmation window starts counting from the FIRST fast-loop
        detection, not from whenever the slow loop happens to also
        notice the same breach.
        """
        now = time.monotonic()
        if now - self.last_held_exit_scan < float(self.config.poll_seconds):
            return
        self.last_held_exit_scan = now
        with _working_orders_lock(self):
            working_snapshot = list(self.working_orders.values())
        keys_with_orders = {
            str(order.get("key"))
            for order in working_snapshot
            if order.get("action") in ("PROFIT", "STOP")
        }
        candidates = []
        for item in self.cached_positions:
            if item.get("instrument_type") != "EQUITY":
                continue
            symbol = str(item.get("symbol", "")).upper()
            if not symbol:
                continue
            quantity = Decimal(str(item.get("quantity", "0")))
            if quantity <= 0:
                continue
            key = f"STOCK:{symbol}"
            if key in keys_with_orders or symbol in self.pending_stock_exits:
                continue
            # By request: "when i touch a stock stop doing anything
            # with it while i am there."
            if _manual_touch_active(self, symbol):
                continue
            # Live incident (PETZ): broker_conflict_symbols is meant to
            # pause a symbol's exit management entirely, not just
            # entries - see _broker_conflict's own docstring.
            if _broker_conflict(self, symbol):
                continue
            candidates.append(symbol)
        if not candidates:
            return
        quotes = self._batched_quotes(candidates)

        def _evaluate_one(symbol: str) -> None:
            key = f"STOCK:{symbol}"
            try:
                quote = quotes.get(symbol)
                if quote is None:
                    return
                quantity, cost = self.api.stock_position(symbol, self.cached_positions)
                if quantity <= 0 or cost <= 0:
                    return
                if self.is_fractional_quantity(quantity):
                    return
                price = self.api.quote_price(quote)
                if price is None:
                    return
                opened_at = self.position_opened_at.get(key)
                seconds_since_entry = (
                    time.monotonic() - opened_at if opened_at is not None else None
                )
                decision = self.strategy.stock_decision(
                    key,
                    price,
                    quantity,
                    cost,
                    self.agent_assessment(symbol),
                    self.cached_opening_grace_active,
                    self.cached_idle_relaxation_multiplier,
                    self.cached_idle_relaxation_amount,
                    seconds_since_entry,
                    self.cached_effective_core_session_active,
                    self.cached_profit_target_multiplier,
                    self.cached_stop_tighten_multiplier,
                )
                is_scalp_cohort = self.strategy.is_volatility_scalp_eligible(
                    symbol
                ) or symbol in self.volatility_scalp_positions
                if is_scalp_cohort:
                    self.volatility_scalp_positions.add(symbol)
                    estimated_average_down_quantity = self.strategy.volatility_scalp_share_count(
                        price,
                        buying_power=self.cached_buying_power,
                        intensity=Decimal("1"),
                    )
                    per_buy_risk_dollars = (
                        price
                        * Decimal(estimated_average_down_quantity)
                        * self.config.volatility_scalp_hard_stop_percent
                    )
                    remaining_capacity = self.strategy.averaging_down_capacity(
                        per_buy_risk_dollars,
                        self.cached_buying_power,
                        self.config.volatility_scalp_max_symbol_risk_fraction,
                        self.config.volatility_scalp_max_averaging_buys,
                    )
                    averaging_available = (
                        self.volatility_scalp_average_down_count[symbol]
                        < remaining_capacity
                    )
                    decision = self.strategy.volatility_scalp_exit_override(
                        decision,
                        quantity,
                        cost,
                        price,
                        averaging_available=averaging_available,
                        symbol=symbol,
                    )
                if decision.action == "LOSS":
                    if symbol not in self.stop_condition_since:
                        self.stop_condition_since[symbol] = time.monotonic()
                else:
                    self.stop_condition_since.pop(symbol, None)
                # By request: "we still need to prioritize profit over
                # cutting losses, so we may need to wait in some cases
                # rather than quickly sell off" / "get rid of the loss
                # fast please." This fast path now only ever acts on
                # PROFIT - a LOSS decision still gets stamped into
                # stop_condition_since above (so the slow loop's own
                # confirmation timer starts counting from the same
                # moment this fast loop first saw it, losing no real
                # time there), but the actual STOP order submission is
                # deliberately left to the slow loop only, giving a
                # losing position the slow loop's own full cycle to
                # recover before anything fires - fast to take profit,
                # patient to cut losses, by explicit request.
                if decision.action != "PROFIT":
                    return
                if self.strategy.exit_blocked_by_lot_restriction(quantity, price):
                    return
                if not self.price_sanity_cooldown_ready(symbol):
                    return
                fee_per_share = self.config.sell_fee_dollars / quantity
                min_profit = cost * self.config.volatility_scalp_target_percent
                if is_scalp_cohort:
                    limit_price = self._stall_exit_price(
                        quote,
                        cost,
                        min_profit,
                        fee_per_share,
                        max_spread_percent=(
                            self.config.volatility_scalp_max_exit_spread_percent
                        ),
                    )
                else:
                    ask = self.api.quote_ask(quote)
                    target = decision.target_price
                    if target is None:
                        return
                    limit_price = max(ask, target) if ask else target
                if limit_price is None:
                    return
                if not self.price_sanity_ok(symbol, price, limit_price):
                    return
                # By request: "buy 20 shares, then sell 5 every 5 cents
                # it goes up... sell 10 and keep the rest for later
                # profit." Scoped to the volatility-scalp cohort's
                # PROFIT branch only - the slow loop's PROFIT path and
                # every LOSS/STOP path everywhere else always sells the
                # full quantity, matching the user's own framing that
                # this is about riding further upside, not softening a
                # loss exit.
                #
                # Live correction (this bug): a plain, never-averaged-
                # down position (GELS, PPBT) got a partial slice sold
                # off on its very first profit exit - by explicit
                # request, partial exits only make sense for a position
                # that's actually been averaged down (multiple buys at
                # different costs, so locking in part of the gain while
                # letting the rest ride against a blended cost basis is
                # meaningful); a single, un-averaged entry should just
                # sell in full on profit like it always did.
                sell_quantity = quantity
                is_partial = False
                if is_scalp_cohort and self.volatility_scalp_average_down_count[symbol] > 0:
                    partial_quantity = self.strategy.volatility_scalp_partial_exit_quantity(
                        int(quantity),
                        price,
                        self.volatility_scalp_last_partial_exit_price.get(symbol),
                    )
                    if partial_quantity <= 0:
                        return
                    if partial_quantity < quantity:
                        sell_quantity = Decimal(partial_quantity)
                        is_partial = True
                order_id = self.api.place_stock(
                    symbol, "SELL", sell_quantity, limit_price=limit_price
                )
                self.pending_stock_exits.add(symbol)
                self.stop_exit_submitted[symbol] = time.monotonic()
                pnl = self.record_realized_exit(cost, limit_price, sell_quantity)
                self.record_trade(
                    key, order_id, "PARTIAL_PROFIT" if is_partial else "PROFIT",
                    limit_price, pnl=pnl,
                    entry_price=cost, quantity=sell_quantity,
                )
                if is_partial:
                    self.volatility_scalp_last_partial_exit_price[symbol] = price
                    self.pending_stock_exits.discard(symbol)
                else:
                    self.volatility_scalp_last_partial_exit_price.pop(symbol, None)
                log.info(
                    "REPRICE| %-8s | %s (fast) | qty=%s | limit=%s | id=%s",
                    symbol,
                    "PARTIAL_PROFIT" if is_partial else "PROFIT",
                    sell_quantity,
                    limit_price,
                    order_id,
                )
            except Exception as exc:
                log.error(
                    "PROTECT| %s | fast held-exit evaluation failed | %s",
                    symbol,
                    exc,
                )

        _dispatch_concurrently(candidates, _evaluate_one)

    def escalate_stalled_stop_losses(self) -> None:
        """Cancel and re-flag an exit (stop-loss OR profit-take) for a more
        aggressive re-quote if its gentler price hasn't filled quickly.

        A stop sitting unfilled while price keeps falling turns a bounded
        loss into an unbounded one. A profit-take sitting unfilled at a
        fixed target that the market never actually reaches (thin ETF, wide
        spread) is a different failure mode with the same fix: without
        this, it cancels on the generic order timeout, then resubmits at
        the exact same unreachable price next cycle since nothing about
        the decision changed - forever, never realizing the gain.
        """
        threshold = float(self.config.stop_loss_escalate_seconds)
        now = time.monotonic()
        for symbol, submitted_at in list(self.stop_exit_submitted.items()):
            key = f"STOCK:{symbol}"
            if symbol not in self.pending_stock_exits:
                self.stop_exit_submitted.pop(symbol, None)
                self.stop_loss_escalated.discard(symbol)
                continue
            if now - submitted_at < threshold:
                continue
            # By request: "when i touch a stock stop doing anything
            # with it while i am there."
            if _manual_touch_active(self, symbol):
                continue
            # Live incident (PETZ): see _broker_conflict's docstring.
            if _broker_conflict(self, symbol):
                continue
            order_id = None
            action = None
            with _working_orders_lock(self):
                snapshot = list(self.working_orders.items())
            for oid, order in snapshot:
                if order.get("key") == key and order.get("action") in (
                    "STOP",
                    "PROFIT",
                ):
                    order_id = oid
                    action = order.get("action")
                    break
            if order_id:
                try:
                    _retry_once_on_rate_limit(self.api.cancel, order_id)
                except Exception as exc:
                    if self.is_order_not_cancelable(exc):
                        log.warning(
                            "STOP   | %s | escalation cancel skipped | "
                            "order already resolving | %s",
                            symbol,
                            exc,
                        )
                    else:
                        log.error(
                            "STOP   | %s | escalation cancel failed | %s",
                            symbol,
                            exc,
                        )
                    continue
                with _working_orders_lock(self):
                    order = self.working_orders.pop(order_id, None)
                # This order is being deliberately abandoned mid-flight (it
                # never filled at the gentler price) - a fresh order fires
                # its own PROFIT/STOP decision and records its own pnl next
                # cycle, so the pnl recorded at THIS order's submission has
                # to be reversed now or it inflates the daily total for an
                # exit that never actually happened.
                if order:
                    self.reverse_phantom_exit(order.get("pnl"), order_id)
                    self._note_exit_failure(key)
            self.stop_loss_escalated.add(symbol)
            self.pending_stock_exits.discard(symbol)
            self.stop_exit_submitted.pop(symbol, None)
            log.warning(
                "STOP   | %s | %s exit unfilled after %ss | escalating to "
                "an aggressive crossing price",
                symbol,
                (action or "pending").lower(),
                threshold,
            )
            # Live incident (CLGN): this log line has always claimed
            # "escalating to an aggressive crossing price," but nothing
            # below it ever actually placed one - the cancelled order
            # was simply abandoned here, leaving the position with NO
            # resting exit at all until trade_stocks' own quote loop
            # (SCAN cadence, observed 30-90s+ live) happened to reach
            # this symbol again. CLGN's PROFIT order stalled while still
            # genuinely above cost, got cancelled here, and then sat
            # completely unprotected for the next ~34 seconds while the
            # price crashed straight through cost and past the hard-
            # stop floor before the slow loop ever noticed - turning a
            # real, in-hand profit into a much larger loss. Reusing the
            # full stock_decision/pricing pipeline here isn't safe to
            # duplicate quickly (it carries its own careful, live-
            # incident-tuned safeguards - price sanity, lot-restriction,
            # stop-loss wick confirmation), so this stays narrow and
            # provably safe: only acts when the position has ALREADY
            # dropped to or below cost (an undeniable loss already
            # forming, not a guess), and only ever fires the same
            # aggressive-cross SELL price genuine stop escalations
            # already use elsewhere - never touches the profit-
            # preserving path when price is still genuinely above cost,
            # which keeps waiting for the slow loop's existing, already-
            # correct _stall_exit_price logic exactly as before.
            try:
                quantity, cost = self.api.stock_position(symbol, self.cached_positions)
                if quantity > 0 and cost > 0:
                    quote = self._batched_quotes([symbol]).get(symbol)
                    if quote is not None:
                        price = self.api.quote_price(quote)
                        # Live incident ("still selling too fast and not
                        # waiting to average down"): this used to sell
                        # the instant price <= cost, full stop - no
                        # regard for the volatility-scalp cohort's
                        # entire "average down instead of stopping out"
                        # design (volatility_scalp_exit_override,
                        # already respected everywhere else a LOSS
                        # decision is handled). SST bought 14:52:46,
                        # stalled, escalated 37s later, and got sold
                        # immediately here without ever getting a chance
                        # to average down - a real regression this fix
                        # introduced. Now runs the SAME override the
                        # rest of the codebase uses before selling: a
                        # scalp-cohort position only sells here if
                        # averaging capacity is already exhausted or the
                        # hard-stop floor is genuinely breached, exactly
                        # matching the slow loop's own logic. A non-
                        # scalp position (no averaging plan behind it)
                        # is unaffected - still sells immediately, since
                        # there's nothing to wait for.
                        should_sell = price is not None and price <= cost
                        if should_sell and (
                            self.strategy.is_volatility_scalp_eligible(symbol)
                            or symbol in self.volatility_scalp_positions
                        ):
                            self.volatility_scalp_positions.add(symbol)
                            estimated_average_down_quantity = self.strategy.volatility_scalp_share_count(
                                price,
                                buying_power=self.cached_buying_power,
                                intensity=Decimal("1"),
                            )
                            per_buy_risk_dollars = (
                                price
                                * Decimal(estimated_average_down_quantity)
                                * self.config.volatility_scalp_hard_stop_percent
                            )
                            remaining_capacity = self.strategy.averaging_down_capacity(
                                per_buy_risk_dollars,
                                self.cached_buying_power,
                                self.config.volatility_scalp_max_symbol_risk_fraction,
                                self.config.volatility_scalp_max_averaging_buys,
                            )
                            averaging_available = (
                                self.volatility_scalp_average_down_count[symbol]
                                < remaining_capacity
                            )
                            override = self.strategy.volatility_scalp_exit_override(
                                Decision("LOSS", "already at/below cost", price),
                                quantity,
                                cost,
                                price,
                                averaging_available=averaging_available,
                                symbol=symbol,
                            )
                            should_sell = override.action == "LOSS"
                        if should_sell:
                            limit_price = self.api.stock_limit_price(quote, "SELL")
                            if limit_price is not None and self.price_sanity_ok(
                                symbol, price, limit_price
                            ):
                                new_order_id = self.api.place_stock(
                                    symbol,
                                    "SELL",
                                    quantity,
                                    limit_price=limit_price,
                                    fractional=self.is_fractional_quantity(quantity),
                                )
                                self.wash_sales.block(
                                    symbol, "stop-loss exit submitted"
                                )
                                self.pending_stock_exits.add(symbol)
                                self.stop_exit_submitted[symbol] = time.monotonic()
                                pnl = self.record_realized_exit(
                                    cost, limit_price, quantity
                                )
                                self.record_trade(
                                    key,
                                    new_order_id,
                                    "STOP",
                                    limit_price,
                                    pnl=pnl,
                                    entry_price=cost,
                                    quantity=quantity,
                                )
                                log.warning(
                                    "STOP   | %s | already at/below cost after "
                                    "the stall - selling immediately instead "
                                    "of waiting for the next scan",
                                    symbol,
                                )
            except Exception as exc:
                log.error(
                    "STOP   | %s | immediate post-escalation sell failed | %s",
                    symbol,
                    exc,
                )

    def trade_stocks(
        self,
        positions: list[dict],
        buying_power: Decimal,
        opening_grace_active: bool = False,
        core_session_active: bool = False,
    ) -> Decimal:
        open_count = self.strategy.open_position_count(positions)
        # Superseded by the hard "no volatility scalp in extended hours"
        # gate on the fresh-entry and averaging-down blocks below (by
        # request, after pre-market losses) - fresh entries and
        # averaging now only ever fire when core_session_active, so
        # full intensity is always correct here; there's no longer a
        # dampened outside-core-hours case to compute.
        volatility_scalp_intensity = Decimal("1")
        volatility_scalp_effective_max_concurrent = max(
            1,
            int(
                self.config.volatility_scalp_max_concurrent_positions
                * volatility_scalp_intensity
            ),
        )
        volatility_scalp_effective_max_averaging = int(
            self.config.volatility_scalp_max_averaging_buys
            * volatility_scalp_intensity
        )
        # freqtrade-style StoplossGuard: too many recent stop-losses pauses
        # NEW entries only (unlike handle_portfolio_circuit_breaker, this
        # never liquidates existing positions) - see stop_loss_guard_active.
        guard_active = self.stop_loss_guard_active()
        # Generalizes option_market_regime_ok's VIXY-rolling-percentile
        # gate to stock entries - self.vixy_history is populated by
        # trade_options (which runs after trade_stocks each cycle), so
        # this reads one cycle stale; negligible for a slow-moving
        # market-wide signal. Computed once per cycle, not per symbol -
        # this is a market-wide read, not a per-symbol one.
        regime_gate_active = self.config.regime_gate_enabled and not (
            self.strategy.stock_market_regime_ok(
                self.vixy_history,
                self.vixy_history[-1] if self.vixy_history else None,
                self.config.regime_gate_reject_percentile,
            )
        )
        # Keeping cash deployed outranks entry quality, but only
        # progressively - see idle_cash_ramp_progress(). ramp_progress is
        # 0 right after any entry, climbing to 1 the longer buying_power
        # sits idle above MIN_CASH_RESERVE_DOLLARS with nothing bought.
        ramp_progress = self.idle_cash_ramp_progress(buying_power)
        idle_relaxation_multiplier = Decimal("1") + ramp_progress * (
            self.config.idle_cash_max_gate_multiplier - Decimal("1")
        )
        idle_relaxation_amount = ramp_progress * self.config.idle_cash_max_tick_relaxation
        # By request: "stay in a significant profit until eod" -> "let
        # winners run further before taking profit." See stock_
        # decision's profit_target_multiplier param.
        profit_target_multiplier = self.profit_target_multiplier(
            self.daily_realized_pnl,
            self.cached_account_value,
            self.config.daily_significant_profit_fraction,
            self.config.profit_target_widen_multiplier,
        )
        # By request: "when we have a certain profit we should also
        # not allow stops to be too low." Same trigger as above.
        stop_tighten_multiplier = self.stop_tighten_multiplier(
            self.daily_realized_pnl,
            self.cached_account_value,
            self.config.daily_significant_profit_fraction,
            self.config.stop_tighten_multiplier,
        )
        # By request, after live evidence (WNW/WKHS stopping out
        # shortly after core hours ended): block FRESH entries only
        # (not averaging down, not any exit) once fewer than
        # stock_entry_blackout_minutes_before_close minutes remain in
        # the core session - see the general BUY/SHORT and volatility-
        # scalp fresh-entry gates below. Computed once per cycle, not
        # per symbol - a market-wide clock read, not a per-symbol one,
        # same convention as regime_gate_active above. Irrelevant (and
        # harmless) whenever core_session_active is already False -
        # the existing "only established/popular symbols trade outside
        # core hours" gate already covers that case.
        moment = self.now()
        option_close_moment = self.session_moment(
            moment, self.config.option_market_close_time
        )
        minutes_until_close = (
            option_close_moment - moment
        ).total_seconds() / 60
        fresh_entry_blackout_active = self.fresh_entry_blackout_active(
            minutes_until_close,
            float(self.config.stock_entry_blackout_minutes_before_close),
            core_session_active,
        )
        # By request: "start transitioning away from core hours
        # strategy around 30 minutes before end of core hours." Softer
        # than fresh_entry_blackout_active above (which HARD-blocks
        # every fresh entry once inside its own, shorter window) - this
        # instead makes the last late_core_session_transition_minutes
        # of core hours behave like extended hours for entry-quality
        # purposes: only the POPULAR bucket gets fresh entries (same
        # "only established/popular symbols trade outside core hours"
        # gate the general BUY/SHORT paths already have), and the
        # spread tolerance widens the same way it does outside core
        # hours (via effective_core_session_active, passed to stock_
        # decision in place of the real core_session_active). Position
        # management/exits are completely unaffected - only entry
        # QUALITY winds down early, nothing stops working.
        late_core_session_transition_active = (
            core_session_active
            and 0
            <= minutes_until_close
            < float(self.config.late_core_session_transition_minutes)
        )
        effective_core_session_active = (
            core_session_active and not late_core_session_transition_active
        )
        # See evaluate_held_stock_exits/cached_opening_grace_active's
        # __init__ comment - refreshed every cycle so the fast
        # position-protection thread always reads this cycle's values.
        self.cached_opening_grace_active = opening_grace_active
        self.cached_idle_relaxation_multiplier = idle_relaxation_multiplier
        self.cached_idle_relaxation_amount = idle_relaxation_amount
        self.cached_effective_core_session_active = effective_core_session_active
        self.cached_profit_target_multiplier = profit_target_multiplier
        self.cached_stop_tighten_multiplier = stop_tighten_multiplier
        self.refresh_agent_discoveries()
        if self.analyst_service is not None:
            # One cycle stale relative to any fetch a symbol's own
            # request() call below just queued - same acceptable
            # staleness as regime_gate_active's vixy_history read above,
            # and more so here since analyst data moves far slower than
            # VIXY. Cheap, in-memory only - see AnalystDataService.snapshot.
            self.strategy.analyst_priority = self.analyst_service.snapshot()
        # By request: "scan through all [the universe]... split it up
        # in parallel streams... as many as needed to scan everything
        # and filter it down, then dynamically less as it is filtered
        # down... does not need to be as intense in extended hours."
        # One rotation of prioritized_stock_batch previously covered
        # only a single STOCK_BATCH_SIZE slice of a universe that can
        # be much larger (up to MAX_SYMBOLS) - see
        # stock_scan_concurrent_batches for how the rotation count
        # scales with the universe size, dynamically fewer once it
        # stops growing, and reduced further outside core hours.
        # Deduped while preserving order across rotations (a symbol
        # could legitimately repeat if the cursor wraps within one
        # cycle on a small/shrunk universe).
        scan_watch_symbols = (
            self.seed_popular_symbols | self.agent_popular_symbols | self.user_watchlist
        )
        concurrent_batches = self.strategy.stock_scan_concurrent_batches(
            len(self.stock_symbols), core_session_active
        )
        batch = []
        seen_in_batch: set[str] = set()
        for _ in range(concurrent_batches):
            rotation, self.stock_cursor = self.strategy.prioritized_stock_batch(
                self.stock_symbols,
                self.stock_cursor,
                positions,
                self.agent_assessment,
                scan_watch_symbols,
            )
            if not rotation:
                break
            for symbol in rotation:
                if symbol not in seen_in_batch:
                    seen_in_batch.add(symbol)
                    batch.append(symbol)
        if self.priority_scan_symbols:
            # A symbol just added via the dashboard has zero accumulated
            # activity score yet, so it ranks at the very bottom of
            # prioritized_stock_batch's popular/penny scoring and can
            # lose out to every already-active watchlist symbol every
            # single cycle - live incident: HOWL, added manually, never
            # once appeared in a scan batch. Force it into THIS batch
            # once, regardless of ranking, so a manual add is guaranteed
            # to actually get looked at.
            injected = [
                symbol
                for symbol in self.priority_scan_symbols
                if symbol in self.stock_symbols and symbol not in batch
            ]
            if injected:
                batch = list(batch) + injected
            self.priority_scan_symbols.clear()
        # premarket_gainers included here too - by request, "get the
        # top gainers before the day starts and look to invest in that
        # for quick profit" - today's actual pre-market movers get
        # scanned every cycle instead of only via prioritized_stock_
        # batch's normal ranking, same reasoning as the volatility-
        # scalp cohort just below.
        force_scan = (
            self.volatility_scalp_symbols
            | self.volatility_scalp_recently_eligible
            | self.premarket_gainers
            | self.agent_predicted_gainers
        )
        if force_scan:
            # Left to prioritized_stock_batch's normal ranking, any one
            # of these might only get re-evaluated once every several
            # cycles, which can't support "multiple times a minute"
            # trading. Force every one of them into every single cycle's
            # batch (not one-time, unlike priority_scan_symbols above) so
            # entry/exit decisions always run through the same, single,
            # correct code path below - no separate/duplicated logic
            # needed. Covers both the curated cohort (a small priority
            # subset) AND every symbol that was volatility-scalp
            # eligible the last time it was scanned - by request, "make
            # sure the data is received as frequently as possible" for
            # the whole broadened set, not just the curated handful.
            missing = [
                symbol
                for symbol in force_scan
                if symbol in self.stock_symbols and symbol not in batch
            ]
            if missing:
                batch = list(batch) + missing
        # Safety net, unconditional: ANY symbol with a real nonzero
        # EQUITY position must get an exit decision every single cycle,
        # regardless of whether it's currently in stock_symbols at all.
        # Live incident: MYND, a real held position, fell out of the
        # daily volatility-filtered scan universe (VOLFILT keeps only
        # ~200 of the full universe) and simply stopped being evaluated
        # - no stop-loss, no profit-target, nothing - while it kept
        # sliding to an 11%+ unrealized loss with zero protective
        # action taken. A position already being risked with real money
        # must never depend on still being in the day's scan list to
        # get managed.
        held_symbols = [
            str(item.get("symbol", "")).upper()
            for item in positions
            if item.get("instrument_type") == "EQUITY"
            and Decimal(str(item.get("quantity", "0"))) != 0
        ]
        unmanaged_held = [
            symbol for symbol in held_symbols if symbol and symbol not in batch
        ]
        if unmanaged_held:
            # Throttled to once per symbol while the condition persists
            # (same pattern as wash_skip_logged) - this still fires
            # every single cycle underneath (the batch-injection itself
            # is unconditional and unaffected), only the WARNING log
            # line is deduped. Live incident: BSEM/GWRS sat outside the
            # scan universe for hours, logging the identical warning
            # every ~5s (3000+ times in one session) and burying real
            # signal in the noise, even though the guard itself was
            # working correctly the whole time.
            newly_unmanaged = sorted(
                set(unmanaged_held) - self.unmanaged_held_logged
            )
            if newly_unmanaged:
                log.warning(
                    "GUARD  | %s held position(s) fell out of the scanned "
                    "universe - forcing them back into the batch so exit "
                    "management resumes | %s",
                    len(newly_unmanaged),
                    ",".join(newly_unmanaged),
                )
            self.unmanaged_held_logged = set(unmanaged_held)
            batch = list(batch) + unmanaged_held
        else:
            # Cleared once every held symbol is back in the scan
            # universe, so a later recurrence (a different symbol, or
            # the same one falling out again after recovering) logs
            # again instead of staying silent forever.
            self.unmanaged_held_logged = set()
        # Live incident: force-injecting the curated cohort AND every
        # volatility-scalp-eligible symbol (self.volatility_scalp_
        # symbols | self.volatility_scalp_recently_eligible, above) on
        # top of an already-full stock_batch_size batch pushed the
        # combined batch size past Webull's own hard 100-symbol snapshot
        # limit (WebullAPI.stock_quotes) - the ENTIRE quote fetch for
        # that cycle then raised and failed, losing price data for every
        # symbol in the batch, not just the extra ones.
        batch = self.cap_batch_to_snapshot_limit(
            batch,
            unmanaged_held,
            limit=concurrent_batches * WebullAPI.STOCK_SNAPSHOT_MAX_SYMBOLS,
        )
        self.last_scan_batch_size = len(batch)
        # Scoped to this cycle's actual scan batch, not the whole
        # (possibly thousands-strong) universe - refresh_recent_momentum
        # already throttles itself, but a full-universe fetch every 120s
        # would be real, avoidable API load for symbols that aren't even
        # being evaluated for entries this cycle.
        self.refresh_recent_momentum(list(batch))
        self.refresh_multi_day_momentum(list(batch))
        bucket_remaining = {
            bucket: buying_power * fraction
            for bucket, fraction in self.config.stock_capital_fractions().items()
        }
        bucket_slot_limits = self.config.stock_bucket_slot_limits()
        bucket_position_counts = {bucket: 0 for bucket in bucket_slot_limits}
        # Two independent capital pools for this cycle, computed once (not
        # re-derived from a live-shrinking buying_power on every candidate)
        # so fractional and whole-share sizing genuinely run side by side -
        # see size_stock_entry. Previously fractional sizing was tried for
        # every eligible candidate and almost always succeeded, so
        # whole-share sizing (a LARGER capital slice than fractional's) was
        # essentially unreachable during core hours.
        fractional_remaining = (
            buying_power * self.config.stock_core_session_position_fraction
        )
        whole_share_remaining = (
            buying_power * self.config.stock_whole_share_core_session_fraction
        )
        max_fractional_positions = self.max_fractional_position_slots(
            self.config.max_open_positions,
            self.config.stock_core_session_position_fraction,
            self.config.stock_whole_share_core_session_fraction,
        )
        fractional_position_count = 0
        known_popular = self.seed_popular_symbols | self.agent_popular_symbols | self.user_watchlist
        for position in positions:
            if (
                position.get("instrument_type") != "EQUITY"
                or Decimal(str(position.get("quantity", "0"))) == 0
            ):
                continue
            if self.is_fractional_quantity(Decimal(str(position.get("quantity", "0")))):
                fractional_position_count += 1
            position_symbol = str(position.get("symbol", "")).upper()
            bucket = self.position_buckets.get(position_symbol)
            if bucket not in bucket_position_counts:
                position_price = Decimal(
                    str(
                        self.strategy.prices.get(
                            position_symbol,
                            position.get("cost_price", "0"),
                        )
                    )
                )
                if position_symbol in known_popular:
                    bucket = "POPULAR"
                elif (
                    position_price > 0
                    and position_price < self.config.penny_stock_max_price
                ):
                    bucket = "PENNY"
                else:
                    bucket = "DISCOVERY"
                self.position_buckets[position_symbol] = bucket
            bucket_position_counts[bucket] += 1
        quotes: list[dict] = []
        invalid: set[str] = set()
        grouped: dict[str, list[str]] = {"US_STOCK": [], "US_ETF": []}
        for symbol in batch:
            grouped[self.stock_categories.get(symbol, "US_STOCK")].append(symbol)
        # By request: "scan through all [the universe]... split it up
        # in parallel streams" - a large batch (now up to
        # concurrent_batches * STOCK_SNAPSHOT_MAX_SYMBOLS symbols, see
        # above) is chunked back down to Webull's own per-call cap and
        # every chunk's quote fetch fires CONCURRENTLY, instead of one
        # chunk waiting out the previous chunk's full round-trip first.
        # A single chunk's own failure only drops that chunk's symbols
        # (same "one group's failure shouldn't cost every other
        # group's data" convention _batched_quotes already uses) -
        # except MarketDataPermissionError, which is a systemic
        # account-level problem, not a per-chunk one, and must still
        # propagate/stop the bot exactly like before this change.
        chunks: list[tuple[str, list[str]]] = []
        for category, category_symbols in grouped.items():
            for start in range(0, len(category_symbols), WebullAPI.STOCK_SNAPSHOT_MAX_SYMBOLS):
                chunk_symbols = category_symbols[
                    start : start + WebullAPI.STOCK_SNAPSHOT_MAX_SYMBOLS
                ]
                if chunk_symbols:
                    chunks.append((category, chunk_symbols))
        chunk_results: list[tuple[list[dict], set[str]] | None] = [None] * len(chunks)
        chunk_errors: list[Exception | None] = [None] * len(chunks)

        def _fetch_chunk(index: int) -> None:
            category, chunk_symbols = chunks[index]
            try:
                chunk_results[index] = self.api.stock_quotes_resilient(
                    chunk_symbols, category
                )
            except Exception as exc:
                chunk_errors[index] = exc

        if chunks:
            with ThreadPoolExecutor(
                max_workers=min(len(chunks), self.config.stock_scan_max_concurrent_batches)
            ) as pool:
                list(pool.map(_fetch_chunk, range(len(chunks))))
        permission_error = next(
            (exc for exc in chunk_errors if isinstance(exc, MarketDataPermissionError)),
            None,
        )
        if permission_error is not None:
            raise permission_error
        if chunks and all(err is not None for err in chunk_errors):
            # Every single chunk failed (not just one) - same "give up
            # this cycle" behavior the old single-call version had on
            # any failure, since there's no usable data at all.
            log.error(
                "STOCKS | quote batch failed | %s", chunk_errors[0]
            )
            return buying_power
        for index, (category, _chunk_symbols) in enumerate(chunks):
            if chunk_errors[index] is not None:
                log.warning(
                    "STOCKS | quote chunk failed | %s | %s",
                    category,
                    chunk_errors[index],
                )
                continue
            category_quotes, category_invalid = chunk_results[index]
            quotes.extend(category_quotes)
            if category_invalid:
                if self.config.exclude_etfs and category == "US_STOCK":
                    invalid.update(category_invalid)
                    continue
                alternate = "US_ETF" if category == "US_STOCK" else "US_STOCK"
                try:
                    alternate_quotes, alternate_invalid = (
                        self.api.stock_quotes_resilient(
                            sorted(category_invalid),
                            alternate,
                        )
                    )
                except Exception as exc:
                    if isinstance(exc, MarketDataPermissionError):
                        raise
                    log.warning(
                        "STOCKS | alternate-category quote fetch failed | %s | %s",
                        alternate,
                        exc,
                    )
                    invalid.update(category_invalid)
                    continue
                quotes.extend(alternate_quotes)
                corrected = category_invalid - alternate_invalid
                for symbol in corrected:
                    self.stock_categories[symbol] = alternate
                invalid.update(alternate_invalid)
        if invalid:
            self.invalid_stock_symbols.update(invalid)
            self.invalid_symbols.add(invalid)
            self.stock_symbols = [
                symbol for symbol in self.stock_symbols if symbol not in invalid
            ]
            replacements = self.backfill_stock_symbols(len(invalid))
            self.stock_cursor %= max(1, len(self.stock_symbols))
            log.warning(
                "SKIP   | invalid=%s | %s | backfilled=%s",
                len(invalid),
                ",".join(sorted(invalid)),
                replacements,
            )
        quote_by_symbol = {
            str(quote.get("symbol", "")).upper(): quote for quote in quotes
        }
        if (
            self.config.volatility_scalp_enabled
            and self.config.volatility_scalp_bar_seed_enabled
        ):
            self.seed_volatility_windows(
                [symbol for symbol in batch if symbol in quote_by_symbol]
            )
        batch_moment = time.monotonic()
        for symbol in batch:
            if symbol in self.broker_conflict_symbols:
                continue
            try:
                quote = quote_by_symbol.get(symbol)
                if not quote:
                    continue
                price = self.api.quote_price(quote)
                self.strategy.update_stock_snapshot(quote, price)
                # By request: micro-exhaustion dip confirmation - see
                # TradingStrategy.volatility_scalp_micro_exhaustion_
                # confirmed. Updated here, once per symbol per real
                # snapshot (not inside the gate-check function itself -
                # see that method's docstring for why a stateful gate
                # would double-count against bot.py's existing
                # diagnostic-visibility tuples). batch_moment is a
                # single time.monotonic() reading shared across this
                # whole batch rather than a fresh one per symbol.
                if price is not None and price > 0:
                    self.strategy.update_recent_tick_history(
                        symbol, price, batch_moment
                    )
                snapshot_volume = self.strategy.metrics.get(symbol, {}).get("volume")
                if snapshot_volume is not None:
                    self.strategy.update_volume_delta(
                        symbol, Decimal(str(snapshot_volume))
                    )
                if self.strategy.is_volatility_scalp_eligible(symbol):
                    self.volatility_scalp_recently_eligible.add(symbol)
                else:
                    self.volatility_scalp_recently_eligible.discard(symbol)
                # By request: "when i touch a stock stop doing anything
                # with it while i am there." Price/volume tracking above
                # still runs (keeps the dashboard/PnL accurate and the
                # strategy's own state warm for when the pause ends),
                # but every decision/order action below - fresh entries,
                # averaging down, PROFIT/STOP submission - is skipped
                # entirely for manual_touch_pause_seconds after a
                # detected manual buy/sell on this symbol.
                if _manual_touch_active(self, symbol):
                    continue
                if self.analyst_service is not None:
                    self.analyst_service.request(symbol, price)
                quantity, cost = self.api.stock_position(symbol, positions)
                key = f"STOCK:{symbol}"
                opened_at = self.position_opened_at.get(key)
                seconds_since_entry = (
                    time.monotonic() - opened_at if opened_at is not None else None
                )
                decision = self.strategy.stock_decision(
                    key,
                    price,
                    quantity,
                    cost,
                    self.agent_assessment(symbol),
                    opening_grace_active,
                    idle_relaxation_multiplier,
                    idle_relaxation_amount,
                    seconds_since_entry,
                    effective_core_session_active,
                    profit_target_multiplier,
                    stop_tighten_multiplier,
                )
                # Condensed onto eligibility alone (any symbol currently
                # volatile enough to qualify - see is_volatility_scalp_
                # eligible), not the narrower curated self.volatility_
                # scalp_symbols cohort list - not just symbols opened via
                # the dip-buy path below, a position already held through
                # the normal trend entry gets the same fast cycling once
                # it qualifies.
                #
                # Live incident (this bug, caught from a real trade
                # log): BTCT averaged down 5 times (its blended cost
                # landed around $1.8494), then stopped out at $1.81 - a
                # 2.1% drop, well inside the 5% hard-stop floor that
                # should have protected it. is_volatility_scalp_
                # eligible is a LIVE, continuously-recalculated stdev
                # check - once several fills naturally calmed the
                # rolling window down below the eligibility threshold,
                # the position instantly lost ALL cohort protection
                # (averaging eligibility AND the hard-stop floor) and
                # fell back to the plain, much tighter adaptive stop.
                # Real capital was already committed across 5 averaging
                # buys - that exposure doesn't shrink just because a
                # transient stdev recalculation dipped under the bar for
                # one cycle. Once a symbol has actually been adopted
                # into cohort management (self.volatility_scalp_
                # positions), it now keeps that treatment for as long as
                # it's held, regardless of whether it's still live-
                # eligible this exact cycle - eligibility still fully
                # gates whether a NEW position gets adopted in the first
                # place, just not whether an existing one keeps its
                # protection.
                if quantity > 0 and (
                    self.strategy.is_volatility_scalp_eligible(symbol)
                    or symbol in self.volatility_scalp_positions
                ):
                    # Live incident (this bug, caught from a real trade
                    # log): a position opened via the NORMAL trend-entry
                    # path that later became scalp-eligible got this
                    # fast quick-profit-take on the way UP (the block
                    # above applies unconditionally to any eligible
                    # held position), but NOT averaging-down protection
                    # on the way down, since self.volatility_scalp_
                    # positions only ever got populated by the scalp's
                    # OWN dip-buy entry path - so its full, larger
                    # adaptive stop-loss stayed active and fired for a
                    # real loss several times the size of this cohort's
                    # own tiny profit-takes (OSRH -1.14, VBIO -0.86 vs.
                    # profits of 0.01-0.06) - heads win small, tails
                    # lose big. Auto-adopts ANY held, currently-eligible
                    # position into full cohort management the moment
                    # it's seen here, regardless of how it was opened,
                    # so it gets the SAME averaging-down recovery plan
                    # and suppressed stop-loss as a symbol dip-bought by
                    # this strategy directly - closing the asymmetry
                    # that quick-profit-take alone was blind to.
                    self.volatility_scalp_positions.add(symbol)
                    # Live incident (VVOS): this used to hardcode
                    # averaging_available=True for ANY cohort position,
                    # so the hard-stop-floor suppression above always
                    # gave a position the FULL 8% of room regardless of
                    # whether it could actually still average down.
                    # VVOS's averaging-down gate hit "cap reached" after
                    # just 1 add (thin buying power on a ~$200 account
                    # left almost no room under the 12% per-symbol risk
                    # budget - see averaging_down_capacity), yet the
                    # exit override kept holding it, unprotected, for
                    # another ~19 minutes and several more points of
                    # adverse move, all the way to the 8% floor - room
                    # that was meant for a DCA ladder that had already
                    # stopped adding. Re-derives the SAME capacity check
                    # the entry-side averaging gate uses (see the
                    # identical calculation below in this method) so the
                    # stop suppression only stays in effect while there
                    # is still real averaging capacity left; once
                    # exhausted, the position falls back to its normal,
                    # tighter adaptive stop instead of riding the full
                    # DCA-sized floor for no further protection.
                    averaging_available = True
                    if symbol in self.volatility_scalp_positions:
                        estimated_average_down_quantity = self.strategy.volatility_scalp_share_count(
                            price,
                            buying_power=self.cached_buying_power,
                            intensity=Decimal("1"),
                        )
                        per_buy_risk_dollars = (
                            price
                            * Decimal(estimated_average_down_quantity)
                            * self.config.volatility_scalp_hard_stop_percent
                        )
                        remaining_capacity = self.strategy.averaging_down_capacity(
                            per_buy_risk_dollars,
                            self.cached_buying_power,
                            self.config.volatility_scalp_max_symbol_risk_fraction,
                            self.config.volatility_scalp_max_averaging_buys,
                        )
                        averaging_available = (
                            self.volatility_scalp_average_down_count[symbol]
                            < remaining_capacity
                        )
                    decision = self.strategy.volatility_scalp_exit_override(
                        decision,
                        quantity,
                        cost,
                        price,
                        averaging_available=averaging_available,
                        symbol=symbol,
                    )
                if decision.action == "LOSS":
                    if symbol not in self.stop_condition_since:
                        self.stop_condition_since[symbol] = time.monotonic()
                else:
                    self.stop_condition_since.pop(symbol, None)
                if decision.action == "HOLD" and quantity == 0:
                    self.gate_rejections[decision.reason] += 1
                if (
                    decision.action == "HOLD"
                    and quantity != 0
                    and decision.reason == "cost basis diverges implausibly from live price"
                ):
                    # Rare and serious enough to warrant its own periodic
                    # line rather than folding into gate_rejections' entry-
                    # side summary - see stock_price_sanity_percent.
                    last_warned = self.cost_sanity_warned_at.get(symbol, 0.0)
                    now_monotonic = time.monotonic()
                    if now_monotonic - last_warned >= 300:
                        self.cost_sanity_warned_at[symbol] = now_monotonic
                        log.warning(
                            "SANITY | %s | average_cost=%s diverges from "
                            "live price=%s by more than %s%% - broker data "
                            "looks wrong, skipping exit math until it "
                            "recovers",
                            symbol,
                            cost,
                            price,
                            self.config.stock_price_sanity_percent * 100,
                        )
                if quantity == 0:
                    self.pending_stock_exits.discard(symbol)
                    self.stop_exit_submitted.pop(symbol, None)
                    self.stop_loss_escalated.discard(symbol)
                    self.stop_condition_since.pop(symbol, None)
                    self.short_symbols.discard(symbol)
                    self.volatility_scalp_positions.discard(symbol)
                    self.volatility_scalp_average_down_count.pop(symbol, None)
                    self.last_volatility_average_down.pop(symbol, None)
                    self.volatility_scalp_last_buy_price.pop(symbol, None)
                if (
                    quantity == 0
                    and core_session_active
                    and symbol not in self.volatility_scalp_positions
                    and not self.has_pending_buy_order(key)
                    and symbol not in self.broker_conflict_symbols
                    and symbol not in self.entry_restricted_symbols
                    and len(self.volatility_scalp_positions)
                    < volatility_scalp_effective_max_concurrent
                    and open_count < self.config.max_open_positions
                    and not regime_gate_active
                ):
                    # Diagnostic-only pass, by request after live evidence
                    # of zero volatility-scalp entries over a multi-hour
                    # window despite individually-eligible candidates
                    # existing - unlike the general strategy's gate_
                    # rejections, this cohort's own entry gate never
                    # recorded WHY a candidate was rejected, out of ~10
                    # independently-narrow conditions stacked together
                    # (each new one added in a separate request, never
                    # tested for their compounding effect together).
                    # Purely additive: evaluates the same conditions in
                    # the same order as the real gate immediately below
                    # and records only the FIRST one that fails - never
                    # affects the real gate or submits anything itself.
                    for reason, ok in (
                        (
                            "scalp - core session closing soon",
                            not fresh_entry_blackout_active,
                        ),
                        (
                            "scalp - daily volatility window seed still in progress",
                            self.volatility_windows_seeded_date == self.resolved_date,
                        ),
                        (
                            "scalp - still in post-stop-loss cooldown",
                            self.post_stop_reentry_ready(symbol),
                        ),
                        (
                            "scalp - wash-sale blocked",
                            not self.wash_sales.blocked_until(symbol),
                        ),
                        (
                            "scalp - order-submission cooldown",
                            self.cooldown_ready(key),
                        ),
                        (
                            "scalp - reentry cooldown",
                            self.volatility_scalp_reentry_ready(key),
                        ),
                        (
                            "scalp - price-sanity cooldown",
                            self.price_sanity_cooldown_ready(symbol),
                        ),
                        (
                            "scalp - not eligible (stdev/dollar volume)",
                            self.strategy.is_volatility_scalp_eligible(symbol),
                        ),
                        (
                            "scalp - spread too wide",
                            self.strategy.volatility_scalp_entry_spread_ok(symbol),
                        ),
                        (
                            "scalp - against the daily SMA trend",
                            self.strategy.sma_trend_supports_entry(
                                symbol, price, "BUY"
                            ),
                        ),
                        (
                            "scalp - below session VWAP",
                            self.strategy.volatility_scalp_vwap_supports_entry(
                                symbol, price
                            ),
                        ),
                        (
                            "scalp - recent momentum breaking down",
                            self.strategy.recent_momentum_supports_entry(
                                symbol, "BUY"
                            ),
                        ),
                        (
                            "scalp - no dip/reversal trigger",
                            (
                                (
                                    self.strategy.volatility_scalp_dip_signal(
                                        symbol, price
                                    )
                                    and self.strategy.volatility_scalp_micro_exhaustion_confirmed(
                                        symbol, price, batch_moment
                                    )
                                )
                                or self.strategy.heikin_ashi_bullish_reversal_signal(
                                    symbol
                                )
                            ),
                        ),
                        (
                            "scalp - momentum still falling",
                            self.strategy.volatility_scalp_momentum_stalled_or_rising(
                                symbol, price
                            ),
                        ),
                    ):
                        if not ok:
                            self.gate_rejections[reason] += 1
                            break
                if (
                    quantity == 0
                    # By request, after pre-market losses: "no volatility
                    # scalp in extended hours." A hard, unconditional
                    # gate - unlike the earlier intensity-dampening
                    # approach this replaces, no fresh volatility-scalp
                    # entry fires at all outside core hours. Exits and
                    # position management for anything already held
                    # (opened during core hours, or held over from an
                    # earlier session) are completely unaffected - only
                    # fresh entries are blocked here.
                    and core_session_active
                    # By request, after live evidence (WNW/WKHS stopping
                    # out shortly after core hours ended): a fresh entry
                    # this close to the bell has almost no runway to
                    # reach its target before conditions change - see
                    # fresh_entry_blackout_active above. Averaging down
                    # on an already-open position is unaffected (a
                    # separate, later gate) - this only blocks a BRAND
                    # NEW commitment.
                    and not fresh_entry_blackout_active
                    # Condensed onto eligibility alone (any symbol
                    # currently volatile enough to qualify), not the
                    # narrower curated self.volatility_scalp_symbols
                    # cohort list - by request: "trade volatile stocks
                    # with high frequency," not just the top handful.
                    # is_volatility_scalp_eligible is the real "is this
                    # volatile enough" test; self.volatility_scalp_symbols
                    # remains a separate, smaller priority list used only
                    # for prioritized batch scanning/dashboard display.
                    # Live incident: GAUZ compounded into a 200-share
                    # position (double the intended fixed 100) after the
                    # cohort started being force-scanned every cycle -
                    # `quantity == 0` alone isn't enough, since positions
                    # only comes from account_state()'s cache (refreshed
                    # every ACCOUNT_REFRESH_SECONDS, ~2s), and a second
                    # entry could fire against that same stale "flat"
                    # snapshot before the first order's fill ever shows
                    # up in it. volatility_scalp_positions is updated
                    # synchronously, in-process, the instant an order is
                    # placed below - a race-free second guard.
                    #
                    # Live incident (this bug, caught from production
                    # logs): with volatility_scalp_reentry_cooldown_
                    # seconds zeroed and trade_cooldown_seconds already
                    # 0, this in-process set was the ONLY thing standing
                    # between one cycle and the next - and the quantity
                    # == 0 cleanup block right above discards a symbol
                    # from it EVERY cycle the account's cached position
                    # snapshot still shows flat, which is true the
                    # entire time a resting BUY order hasn't filled yet
                    # (quantity genuinely IS 0 - no shares owned, just a
                    # pending order). That reopened the exact race this
                    # set exists to close: MTNB got 5 separate 100-share
                    # BUY orders stacked within ~70s, all at the same
                    # price, because the guard was wiped and re-armed
                    # every single cycle while the first order just sat
                    # resting. self.has_pending_buy_order(key) checks
                    # self.working_orders directly instead - true for as
                    # long as an uncancelled BUY order for this symbol
                    # actually exists, regardless of what the (up to
                    # ACCOUNT_REFRESH_SECONDS-stale) position snapshot
                    # says - a real fix, not another cooldown.
                    and symbol not in self.volatility_scalp_positions
                    and not self.has_pending_buy_order(key)
                    and symbol not in self.broker_conflict_symbols
                    and symbol not in self.entry_restricted_symbols
                    and len(self.volatility_scalp_positions)
                    < volatility_scalp_effective_max_concurrent
                    and open_count < self.config.max_open_positions
                    # By explicit request: keep buying this cohort's
                    # dips continuously, multiple times a minute, EVEN
                    # THROUGH a losing stretch - unlike every other
                    # entry path, this deliberately does NOT check
                    # guard_active (account-wide stop-loss guard),
                    # symbol_quarantined (recent-loss pause), or
                    # rate_capped (hourly trade cap), since those exist
                    # specifically to slow down or pause trading after
                    # losses - exactly what this strategy is meant to
                    # keep doing anyway. Only cooldown_ready (a cross-
                    # order-submission race guard, not a loss-driven
                    # pause - trade_cooldown_seconds defaults to 0) and
                    # volatility_scalp_reentry_ready (zeroed by request -
                    # "orders can be made as frequently as possible
                    # without a cooldown") still gate timing.
                    #
                    # REVERSED, by request ("does the wash sale block
                    # actually fulfill its purpose"): a wash-sale block
                    # WAS on this same bypass list until today - live
                    # evidence showed CLGN stop out and get a wash-sale
                    # block written 5 separate times in one session
                    # (11:11, 11:56, 12:05, 12:12, 12:58), and every
                    # single one was a no-op, since this path never
                    # checked it. Re-added as a real gate here - a
                    # symbol that just stopped out via THIS path can't
                    # be re-bought via THIS path either for wash_sale_
                    # block_days. Averaging down on an already-open
                    # position is a separate, later gate and is
                    # unaffected either way - this only blocks a BRAND
                    # NEW fresh entry.
                    and not self.wash_sales.blocked_until(symbol)
                    and not regime_gate_active
                    # Deliberately does NOT check symbol_regime here (a
                    # Kaufman-Efficiency-Ratio "trending vs ranging" gate
                    # exists and IS applied to the general momentum path
                    # below) - live evidence, and a bug, not just a
                    # design choice: it produced zero volatility-scalp
                    # entries for a full session. It directly contradicts
                    # an earlier, more specific, already-documented
                    # design decision - volatility_scalp_dip_signal's own
                    # docstring explicitly wants to dip-buy a stock
                    # "trending hard in one direction all day" (live
                    # example: HOWL, up ~100% intraday) - exactly the
                    # case a high efficiency ratio (TRENDING) describes.
                    # This mean-reversion path's whole thesis is buying
                    # pullbacks WITHIN a move, trending or not; only the
                    # general EMA-crossover path actually needs to know
                    # whether a symbol is trending.
                    # By request, after the DAIC incident (3 stop-losses
                    # in ~9 minutes on one symbol during a fast decline,
                    # erasing the day's gains): unlike every other loss-
                    # driven gate above, deliberately kept even for this
                    # cohort's "trade through losses" design - it's
                    # narrow (pauses only the ONE symbol that just
                    # stopped out, not the whole strategy) and doesn't
                    # conflict with "keep buying dips through a losing
                    # stretch" elsewhere. Without it, nothing stopped an
                    # immediate re-entry into the exact same falling
                    # knife seconds after being stopped out of it.
                    and self.post_stop_reentry_ready(symbol)
                    and self.cooldown_ready(key)
                    and self.volatility_scalp_reentry_ready(key)
                    and self.price_sanity_cooldown_ready(symbol)
                    and self.strategy.is_volatility_scalp_eligible(symbol)
                    # By request: "make sure the algo plays around in
                    # the spread while ensuring a profit, or a
                    # profitable entry" - buying into an absurdly wide
                    # spread sets up a losing trade before it even
                    # starts (the exit still has to clear the same wide
                    # spread to reach a real profit).
                    and self.strategy.volatility_scalp_entry_spread_ok(symbol)
                    # By request, after "why is it selecting stocks at
                    # such wrong times, having to sell majority for
                    # losses": the scalp entry path never checked the
                    # higher-timeframe daily trend at all, unlike the
                    # general strategy (which already has this exact
                    # filter). It would happily dip-buy a stock in a
                    # real, sustained daily downtrend, where each "dip"
                    # is just continuation, not a bounce setup - live
                    # incident: AIRE averaged down once and stopped out
                    # a minute later. Reuses the same sma_trend_
                    # supports_entry infrastructure the general strategy
                    # already relies on (refreshed once daily from real
                    # daily-bar closes, see AutoTrader.refresh_sma_trend)
                    # - only lets a dip-buy fire in the direction of (or
                    # with no data on) the larger trend, not against it.
                    and self.strategy.sma_trend_supports_entry(symbol, price, "BUY")
                    # By request, after an end-of-day retrospective ("we
                    # just kept buying at the wrong time"): the SMA
                    # filter above only catches a MULTI-DAY downtrend -
                    # nothing for a stock simply having a bad DAY today
                    # specifically, which is what repeated same-day
                    # losses on one symbol (BTCT, three times in one
                    # session) actually looks like. A stock trading
                    # meaningfully below its own session VWAP is real
                    # intraday weakness, not just a normal dip.
                    and self.strategy.volatility_scalp_vwap_supports_entry(
                        symbol, price
                    )
                    # By request: "look at tickers in the last 10 mins
                    # for momentum... to analyze the upcoming trend."
                    # The two checks above cover the historical (SMA)
                    # and whole-day (VWAP) trend - this completes the
                    # chain with the one timeframe in between. Only
                    # blocks a fast, real breakdown over the last few
                    # minutes (see recent_momentum_supports_entry's own
                    # docstring) - a normal, moderate dip still passes.
                    and self.strategy.recent_momentum_supports_entry(
                        symbol, "BUY"
                    )
                    # By request: "also include not only short term
                    # patterns like 5-10 mins, but also 1 day and 5 day
                    # and month." Completes the timeframe chain with
                    # real daily-bar-derived 1-day/5-day/~month checks
                    # - see multi_day_momentum_supports_entry's own
                    # docstring for why it's deliberately more
                    # permissive at longer horizons than the short-term
                    # checks above.
                    and self.strategy.multi_day_momentum_supports_entry(
                        symbol, "BUY", price
                    )
                    # TWO independent, OR'd entry triggers: the dip-buy
                    # signal (gated behind micro-exhaustion confirmation
                    # below) and a Heikin-Ashi confirmed bullish reversal
                    # candle.
                    #
                    # Live incident (this bug): the Dual-Thrust-style
                    # opening-range breakout trigger that used to sit
                    # here as a third OR'd path had NO follow-through
                    # confirmation at all - unlike dip-signal (gated
                    # behind volatility_scalp_micro_exhaustion_confirmed
                    # below), it fired the INSTANT price crossed the
                    # breakout level, with no check that the breakout
                    # actually held. ORBS: bought at $0.9589, one tick
                    # before the local peak ($0.9594), then fell ~4.2%
                    # from there - a textbook failed breakout, buying
                    # right at the top of a run-up that immediately
                    # reversed. By explicit request, after confirming
                    # this: removed entirely rather than adding a
                    # confirmation gate - dip-signal (with its real
                    # exhaustion confirmation) and reversal already
                    # cover this cohort's real setups.
                    and (
                        (
                            self.strategy.volatility_scalp_dip_signal(symbol, price)
                            and self.strategy.volatility_scalp_micro_exhaustion_confirmed(
                                symbol, price, batch_moment
                            )
                        )
                        or self.strategy.heikin_ashi_bullish_reversal_signal(symbol)
                    )
                    # By request: "we don't want to buy when there is
                    # downward momentum... buy when the dip is stalled
                    # or at the bottom, or even when the momentum
                    # starts to go up." An AND gate on top of all three
                    # triggers above, not a fourth alternative - clearing
                    # the dip-percent/breakout/HA-reversal bar doesn't
                    # matter if price is still actively falling the
                    # instant it does.
                    and self.strategy.volatility_scalp_momentum_stalled_or_rising(
                        symbol, price
                    )
                ):
                    scalp_quantity = self.strategy.volatility_scalp_share_count(
                        price,
                        buying_power=buying_power,
                        intensity=volatility_scalp_intensity,
                    )
                    if scalp_quantity > 0 and price * Decimal(scalp_quantity) * Decimal(
                        "1.03"
                    ) > buying_power:
                        scalp_quantity = 0
                    if scalp_quantity > 0 and not self.volatility_scalp_position_value_ok(
                        0, scalp_quantity, price
                    ):
                        scalp_quantity = 0
                    if scalp_quantity > 0 and not self.volatility_scalp_total_exposure_ok(
                        positions, price * Decimal(scalp_quantity)
                    ):
                        scalp_quantity = 0
                    if scalp_quantity > 0:
                        order_id = self.place_stock_scaled(
                            symbol,
                            "BUY",
                            scalp_quantity,
                            key,
                            quote,
                            limit_price_override=self.volatility_scalp_entry_price(
                                quote
                            ),
                        )
                        if order_id is not None:
                            self.record_trade(
                                key,
                                order_id,
                                "BUY",
                                entry_price=self.volatility_scalp_entry_price(quote),
                                quantity=scalp_quantity,
                                # Doesn't reset the general strategy's
                                # idle-cash relaxation clock - see
                                # record_trade's docstring note.
                                counts_toward_idle_cash_ramp=False,
                            )
                            self.volatility_scalp_positions.add(symbol)
                            self.volatility_scalp_last_buy_price[symbol] = price
                            buffered_price = price * Decimal("1.03")
                            buying_power = max(
                                Decimal("0"),
                                buying_power - buffered_price * scalp_quantity,
                            )
                            positions.append(
                                {
                                    "instrument_type": "EQUITY",
                                    "symbol": symbol,
                                    "quantity": str(scalp_quantity),
                                }
                            )
                            open_count += 1
                            log.info(
                                "SCALP  | %-8s | dip entry | qty=%s | price=%s",
                                symbol,
                                scalp_quantity,
                                price,
                            )
                # By request: bound worst-case per-symbol exposure from
                # averaging down (research: "doubling down three times
                # can turn a 7% position into an 18% loss"). Per-symbol,
                # not the flat global cap above - a small account's real
                # risk-fraction limit may bind before the configured
                # ceiling ever would. Estimated at this cycle's would-be
                # buy size/price (cheap, pure, no side effects - the
                # real buy is sized again, identically, below once this
                # gate has already passed).
                volatility_scalp_symbol_averaging_cap = volatility_scalp_effective_max_averaging
                if symbol in self.volatility_scalp_positions:
                    estimated_average_down_quantity = self.strategy.volatility_scalp_share_count(
                        price, buying_power=buying_power, intensity=volatility_scalp_intensity
                    )
                    per_buy_risk_dollars = (
                        price
                        * Decimal(estimated_average_down_quantity)
                        * self.config.volatility_scalp_hard_stop_percent
                    )
                    volatility_scalp_symbol_averaging_cap = self.strategy.averaging_down_capacity(
                        per_buy_risk_dollars,
                        buying_power,
                        self.config.volatility_scalp_max_symbol_risk_fraction,
                        volatility_scalp_effective_max_averaging,
                    )
                    # Diagnostic-only pass, by request to actually see
                    # which specific condition is blocking an averaging-
                    # down add on a given cycle instead of inferring it
                    # from aggregate counts - the fresh-entry gate above
                    # already has this (see "scalp - momentum still
                    # falling"), this block never did. Purely additive:
                    # evaluates the same conditions in the same order as
                    # the real gate immediately below and records only
                    # the FIRST one that fails - never affects the real
                    # gate or submits anything itself.
                    for reason, ok in (
                        (
                            "scalp avgdown - averaging cap reached",
                            self.volatility_scalp_average_down_count[symbol]
                            < volatility_scalp_symbol_averaging_cap,
                        ),
                        (
                            "scalp avgdown - reentry cooldown",
                            (
                                time.monotonic()
                                - self.last_volatility_average_down.get(symbol, 0.0)
                            )
                            >= float(self.config.volatility_scalp_reentry_cooldown_seconds),
                        ),
                        (
                            "scalp avgdown - not X% below average cost",
                            self.strategy.volatility_scalp_average_down_signal(
                                price,
                                cost,
                                level=self.volatility_scalp_average_down_count[symbol],
                            ),
                        ),
                        (
                            "scalp avgdown - not below last buy price",
                            (
                                symbol not in self.volatility_scalp_last_buy_price
                                or price < self.volatility_scalp_last_buy_price[symbol]
                            ),
                        ),
                        (
                            "scalp avgdown - momentum still falling",
                            self.strategy.volatility_scalp_momentum_stalled_or_rising(
                                symbol, price
                            ),
                        ),
                        (
                            "scalp avgdown - not statistically oversold (RSI)",
                            self.strategy.rsi_supports_entry(symbol),
                        ),
                    ):
                        if not ok:
                            self.avgdown_gate_rejections[reason] += 1
                            break
                if (
                    quantity > 0
                    # Same "no volatility scalp in extended hours" hard
                    # gate as the fresh-entry block above - averaging
                    # down is still a new BUY commitment, just against
                    # an existing position instead of a flat one.
                    and core_session_active
                    # symbol in self.volatility_scalp_positions is the
                    # real gate here (an open position this strategy
                    # itself opened, and is therefore eligible to average
                    # down on) - condensed off the narrower curated
                    # self.volatility_scalp_symbols cohort list, same as
                    # every other gate in this block.
                    and symbol in self.volatility_scalp_positions
                    # Same fix as the fresh-entry gate above -
                    # volatility_scalp_reentry_cooldown_seconds is
                    # zeroed by request, so the elapsed-time check just
                    # below is a no-op (0 >= 0 is always true one cycle
                    # later); has_pending_buy_order is the real guard
                    # against stacking a second averaging buy while the
                    # first is still resting unfilled.
                    and not self.has_pending_buy_order(key)
                    and symbol not in self.broker_conflict_symbols
                    and symbol not in self.entry_restricted_symbols
                    and self.volatility_scalp_average_down_count[symbol]
                    < volatility_scalp_symbol_averaging_cap
                    # Sanity-check fix: the fresh-entry gate above
                    # deliberately still checks regime_gate_active (a
                    # market-wide VIXY-spike gate is kept even though
                    # this cohort's own loss-driven gates are bypassed),
                    # but this averaging-down block had no such check -
                    # meaning a market-wide vol spike would block NEW
                    # dip-buys while still letting the bot add to an
                    # EXISTING losing position, backwards from what a
                    # risk-off signal should do.
                    and not regime_gate_active
                    # In-process, race-free throttle (same reasoning as
                    # the fresh-entry guard above) - without it, several
                    # cycles within one ACCOUNT_REFRESH_SECONDS window
                    # could each independently see the same still-low
                    # cost basis and fire a fresh averaging buy before
                    # the last one's fill ever updates it.
                    and (
                        time.monotonic()
                        - self.last_volatility_average_down.get(symbol, 0.0)
                    )
                    >= float(self.config.volatility_scalp_reentry_cooldown_seconds)
                    and self.strategy.volatility_scalp_average_down_signal(
                        price,
                        cost,
                        level=self.volatility_scalp_average_down_count[symbol],
                    )
                    # By request: "when you average down, you buy at a
                    # lower price, not the same price." The signal above
                    # only checks price against the BLENDED average cost,
                    # which a repeated buy at the same price barely
                    # moves - so the same price could keep re-qualifying
                    # as "X% below average cost" indefinitely without
                    # ever making a genuinely new, lower low. Requires
                    # strictly lower than the actual price of the last
                    # buy (fresh entry or a prior averaging-down) on this
                    # symbol.
                    and (
                        symbol not in self.volatility_scalp_last_buy_price
                        or price < self.volatility_scalp_last_buy_price[symbol]
                    )
                    # Reverses an earlier by-request decision ("not
                    # averaging down enough") that deliberately let this
                    # block skip the fresh-entry momentum-stall check,
                    # on the theory that averaging down should catch a
                    # dip "while it's still happening." Live evidence
                    # this backfired: CELU averaged down 5 times in ~6
                    # minutes and BTCT 6 times in ~35, each add landing
                    # at a still-lower price than the one before it,
                    # both eventually hard-stopping out and erasing the
                    # day's gains (+$6.97 peak -> -$0.26). By request:
                    # "average down when the declining momentum ends and
                    # wait for the uptrend to sell the stock then" - same
                    # stall check fresh entries already use (requires a
                    # genuine consecutive decline to have just stopped
                    # getting worse, fails open on too little history),
                    # applied here too so a level only gets added once
                    # THIS specific decline shows signs of stopping,
                    # instead of on every strictly-lower tick regardless
                    # of whether the fall is still accelerating.
                    and self.strategy.volatility_scalp_momentum_stalled_or_rising(
                        symbol, price
                    )
                    # By request: "these commonly seen patterns should
                    # also influence averaging down and stop loss...
                    # not just entries and exits." Same RSI oversold
                    # check fresh entries already require - don't add
                    # to a losing position just because price ticked
                    # lower, only when that lower price is ALSO a
                    # genuine statistical extreme, same historically-
                    # standard bar a fresh entry has to clear.
                    and self.strategy.rsi_supports_entry(symbol)
                ):
                    average_down_quantity = self.strategy.volatility_scalp_share_count(
                        price,
                        buying_power=buying_power,
                        intensity=volatility_scalp_intensity,
                    )
                    # By request: "even it is supposed to avg down it is
                    # not executing it" - the real averaging-down GATE
                    # (immediately above) has its own AVGDOWN diagnostic,
                    # but once the gate passes, THESE three checks can
                    # still silently zero the quantity out with no
                    # logging at all - a candidate could clear every
                    # AVGDOWN condition and still never actually place an
                    # order, invisibly. Logged here (not affecting the
                    # real zeroing logic itself) so a silent-execution-
                    # failure complaint can be diagnosed with real
                    # evidence instead of guesswork, same reasoning as
                    # the AVGDOWN diagnostic itself.
                    if average_down_quantity > 0 and price * Decimal(
                        average_down_quantity
                    ) * Decimal("1.03") > buying_power:
                        average_down_quantity = 0
                        self.avgdown_gate_rejections[
                            "scalp avgdown - sized qty not affordable"
                        ] += 1
                    if (
                        average_down_quantity > 0
                        and not self.volatility_scalp_position_value_ok(
                            quantity, average_down_quantity, price
                        )
                    ):
                        average_down_quantity = 0
                        self.avgdown_gate_rejections[
                            "scalp avgdown - would exceed per-symbol "
                            "position value cap"
                        ] += 1
                    if (
                        average_down_quantity > 0
                        and not self.volatility_scalp_total_exposure_ok(
                            positions, price * Decimal(average_down_quantity)
                        )
                    ):
                        average_down_quantity = 0
                        self.avgdown_gate_rejections[
                            "scalp avgdown - would exceed whole-cohort "
                            "exposure cap"
                        ] += 1
                    if average_down_quantity > 0:
                        # By request: "why is average down buying at
                        # the top of the spread?" - this used to share
                        # volatility_scalp_entry_price (the aggressive,
                        # ask-crossing price) with fresh entries. That
                        # urgency makes sense for a fresh entry (miss
                        # the fill, miss the move entirely), but works
                        # directly against averaging down's whole point
                        # - improving the blended cost - by paying the
                        # single worst available price on every add.
                        # The averaging-down signal already requires
                        # price to have dropped a real amount below the
                        # average cost first (not the same split-second
                        # urgency as a fresh momentum entry), so the
                        # general strategy's own passive bid/ask
                        # midpoint pricing (stock_limit_price) fits
                        # better here - falls back to the old
                        # aggressive price only if the quote's bid/ask
                        # is invalid, so this is never MORE likely to
                        # skip a fill than before, just cheaper when it
                        # succeeds.
                        try:
                            average_down_price = self.api.stock_limit_price(
                                quote, "BUY"
                            )
                        except Exception:
                            average_down_price = self.volatility_scalp_entry_price(
                                quote
                            )
                        order_id = self.place_stock_scaled(
                            symbol,
                            "BUY",
                            average_down_quantity,
                            key,
                            quote,
                            limit_price_override=average_down_price,
                        )
                        if order_id is not None:
                            self.record_trade(
                                key,
                                order_id,
                                "BUY",
                                entry_price=average_down_price,
                                quantity=average_down_quantity,
                                counts_toward_idle_cash_ramp=False,
                            )
                            self.volatility_scalp_average_down_count[symbol] += 1
                            self.last_volatility_average_down[symbol] = (
                                time.monotonic()
                            )
                            self.volatility_scalp_last_buy_price[symbol] = price
                            buying_power = max(
                                Decimal("0"),
                                buying_power
                                - price * Decimal("1.03") * average_down_quantity,
                            )
                            log.info(
                                "SCALP  | %-8s | average down | qty=%s | price=%s "
                                "| count=%s",
                                symbol,
                                average_down_quantity,
                                price,
                                self.volatility_scalp_average_down_count[symbol],
                            )
                        else:
                            # place_stock_scaled returns None silently
                            # (price-sanity cooldown or a fat-finger
                            # check) - same "even it is supposed to avg
                            # down it is not executing it" visibility
                            # gap as the three checks above.
                            self.avgdown_gate_rejections[
                                "scalp avgdown - order placement declined "
                                "(price-sanity check)"
                            ] += 1
                if decision.action == "BUY" and quantity == 0:
                    if symbol in self.entry_restricted_symbols:
                        continue
                    blocked_until = self.wash_sales.blocked_until(symbol)
                    if blocked_until:
                        if symbol not in self.wash_skip_logged:
                            self.wash_skip_logged.add(symbol)
                            log.info(
                                "WASH   | %-8s | entry blocked until %s",
                                symbol,
                                blocked_until.strftime("%Y-%m-%d"),
                            )
                        continue
                    self.wash_skip_logged.discard(symbol)
                    if guard_active:
                        self.gate_rejections[
                            "stop-loss guard active - too many recent stops"
                        ] += 1
                        continue
                    if self.symbol_quarantined(key):
                        self.gate_rejections[
                            "symbol quarantined - recent net losses on this symbol"
                        ] += 1
                        continue
                    if regime_gate_active:
                        self.gate_rejections[
                            "regime gate - VIXY elevated vs recent range"
                        ] += 1
                        continue
                    # By request: "regime-dependent" strategy switching -
                    # this general EMA-crossover path IS the momentum
                    # engine (a fresh cross/continuation signal), so it
                    # only opens a FRESH position when this specific
                    # symbol is actually trending - a momentum entry
                    # into a choppy, range-bound symbol is exactly the
                    # mismatched-thesis case research warns against.
                    # UNKNOWN (insufficient history) stays eligible,
                    # unchanged from before this gate existed - fails
                    # open, not closed, same convention as every other
                    # gate here.
                    if self.strategy.symbol_regime(symbol) == "RANGING":
                        self.gate_rejections[
                            "symbol regime is ranging - momentum entry "
                            "skipped, mean-reversion handles it instead"
                        ] += 1
                        continue
                    bucket = self.strategy.selection_bucket(symbol)
                    # By explicit request: "stop trading extended hours
                    # unless it is for closing out positions." Used to
                    # allow a fresh entry outside core hours for the
                    # POPULAR bucket only ("only trading established
                    # stocks... in extended hours") - now a hard block
                    # on every fresh entry outside core hours,
                    # regardless of bucket. Every exit path (profit
                    # target, stop-loss, EOD closeout, the extended-
                    # hours profit sweep) is completely unaffected -
                    # only opening a brand-new position is blocked here.
                    if not effective_core_session_active:
                        self.gate_rejections[
                            "extended hours - fresh entries disabled, "
                            "exits only"
                        ] += 1
                        continue
                    if fresh_entry_blackout_active:
                        self.gate_rejections[
                            "core session closing soon - no new fresh "
                            "entries this close to the bell"
                        ] += 1
                        continue
                    entry_budget = min(
                        buying_power,
                        bucket_remaining.get(bucket, Decimal("0")),
                        self.diversification_capped_entry_budget(
                            buying_power,
                            self.config.stock_max_position_fraction_of_buying_power,
                            self.config.fractional_shares_min_notional,
                        ),
                    )
                    fractional_supported = symbol not in self.fractional_unsupported_symbols
                    buy_quantity, buffered_price, fractional = self.size_stock_entry(
                        price,
                        entry_budget,
                        fractional_remaining,
                        whole_share_remaining,
                        core_session_active,
                        fractional_position_count < max_fractional_positions,
                        fractional_supported,
                        symbol=symbol,
                        buying_power=buying_power,
                    )
                    if (
                        buy_quantity == 0
                        and self.config.fractional_shares_enabled
                        and core_session_active
                        and self.fractional_trading_enabled
                        and fractional_supported
                        and fractional_position_count < max_fractional_positions
                    ):
                        fractional_quantity = self.strategy.fractional_stock_quantity(
                            price,
                            entry_budget,
                        )
                        if fractional_quantity > 0:
                            buy_quantity = fractional_quantity
                            buffered_price = price * Decimal("1.03")
                            fractional = True
                    if (
                        open_count < self.config.max_open_positions
                        and bucket_position_counts.get(bucket, 0)
                        < bucket_slot_limits.get(bucket, 0)
                        and buy_quantity > 0
                        and self.cooldown_ready(key)
                        and not self.rate_capped(key)
                        and self.reentry_cooldown_ready(key)
                        and self.price_sanity_cooldown_ready(symbol)
                        and self.strategy.obi_supports_entry(
                            self.obi_score_for(
                                symbol,
                                self.stock_categories.get(symbol, "US_STOCK"),
                                quote,
                            )
                        )
                    ):
                        order_id = self.place_stock_scaled(
                            symbol,
                            "BUY",
                            buy_quantity,
                            key,
                            quote,
                            fractional=fractional,
                        )
                        if order_id is None:
                            continue
                        self.record_trade(
                            key,
                            order_id,
                            "BUY",
                            entry_price=self.api.stock_limit_price(quote, "BUY"),
                            quantity=buy_quantity,
                        )
                        buying_power = max(
                            Decimal("0"),
                            buying_power - buffered_price * buy_quantity,
                        )
                        bucket_remaining[bucket] = max(
                            Decimal("0"),
                            bucket_remaining.get(bucket, Decimal("0"))
                            - buffered_price * buy_quantity,
                        )
                        if fractional:
                            fractional_remaining = max(
                                Decimal("0"),
                                fractional_remaining - buffered_price * buy_quantity,
                            )
                            fractional_position_count += 1
                        else:
                            whole_share_remaining = max(
                                Decimal("0"),
                                whole_share_remaining - buffered_price * buy_quantity,
                            )
                        self.position_buckets[symbol] = bucket
                        bucket_position_counts[bucket] += 1
                        positions.append(
                            {
                                "instrument_type": "EQUITY",
                                "symbol": symbol,
                                "quantity": str(buy_quantity),
                            }
                        )
                        open_count += 1
                if decision.action == "SHORT" and quantity == 0:
                    if symbol in self.entry_restricted_symbols:
                        continue
                    blocked_until = self.wash_sales.blocked_until(symbol)
                    if blocked_until:
                        if symbol not in self.wash_skip_logged:
                            self.wash_skip_logged.add(symbol)
                            log.info(
                                "WASH   | %-8s | short entry blocked until %s",
                                symbol,
                                blocked_until.strftime("%Y-%m-%d"),
                            )
                        continue
                    self.wash_skip_logged.discard(symbol)
                    if self.symbol_quarantined(key):
                        self.gate_rejections[
                            "symbol quarantined - recent net losses on this symbol"
                        ] += 1
                        continue
                    if regime_gate_active:
                        self.gate_rejections[
                            "regime gate - VIXY elevated vs recent range"
                        ] += 1
                        continue
                    if guard_active:
                        self.gate_rejections[
                            "stop-loss guard active - too many recent stops"
                        ] += 1
                        continue
                    # Same regime gate as the BUY entry path above -
                    # SHORT is the momentum engine's other direction.
                    if self.strategy.symbol_regime(symbol) == "RANGING":
                        self.gate_rejections[
                            "symbol regime is ranging - momentum entry "
                            "skipped, mean-reversion handles it instead"
                        ] += 1
                        continue
                    bucket = self.strategy.selection_bucket(symbol)
                    # Same "established/popular only" restriction as the
                    # BUY entry gate above, for the same reason.
                    if not effective_core_session_active and bucket != "POPULAR":
                        self.gate_rejections[
                            "extended hours - only established/popular "
                            "symbols trade outside core hours"
                        ] += 1
                        continue
                    if fresh_entry_blackout_active:
                        self.gate_rejections[
                            "core session closing soon - no new fresh "
                            "entries this close to the bell"
                        ] += 1
                        continue
                    entry_budget = min(
                        buying_power,
                        bucket_remaining.get(bucket, Decimal("0")),
                        self.diversification_capped_entry_budget(
                            buying_power,
                            self.config.stock_max_position_fraction_of_buying_power,
                            self.config.fractional_shares_min_notional,
                        ),
                    )
                    # Whole-share sizing only - Webull's fractional-share
                    # trading is a long-only retail feature, there's no
                    # confirmed fractional short order type.
                    short_quantity, buffered_price = self.strategy.stock_order_quantity(
                        price, entry_budget
                    )
                    if (
                        self.short_selling_supported
                        and open_count < self.config.max_open_positions
                        and bucket_position_counts.get(bucket, 0)
                        < bucket_slot_limits.get(bucket, 0)
                        and short_quantity > 0
                        and self.cooldown_ready(key)
                        and not self.rate_capped(key)
                        and self.reentry_cooldown_ready(key)
                        and self.price_sanity_cooldown_ready(symbol)
                        and self.strategy.obi_supports_entry(
                            self.obi_score_for(
                                symbol,
                                self.stock_categories.get(symbol, "US_STOCK"),
                                quote,
                            )
                        )
                    ):
                        order_id = self.place_stock_scaled(
                            symbol,
                            "SHORT",
                            short_quantity,
                            key,
                            quote,
                        )
                        if order_id is None:
                            continue
                        self.record_trade(
                            key,
                            order_id,
                            "SHORT",
                            entry_price=self.api.stock_limit_price(quote, "SHORT"),
                            quantity=short_quantity,
                        )
                        # Not exact margin accounting (Webull's actual short
                        # margin requirement isn't modeled here) - same
                        # rough capital-pool tracking the rest of this
                        # function already uses, just enough to stop
                        # multiple candidates in one cycle from each
                        # believing they have the full stale buying_power.
                        buying_power = max(
                            Decimal("0"),
                            buying_power - buffered_price * short_quantity,
                        )
                        bucket_remaining[bucket] = max(
                            Decimal("0"),
                            bucket_remaining.get(bucket, Decimal("0"))
                            - buffered_price * short_quantity,
                        )
                        self.position_buckets[symbol] = bucket
                        self.short_symbols.add(symbol)
                        bucket_position_counts[bucket] += 1
                        positions.append(
                            {
                                "instrument_type": "EQUITY",
                                "symbol": symbol,
                                "quantity": str(-short_quantity),
                            }
                        )
                        open_count += 1
                is_short_position = quantity < 0
                exit_quantity = -quantity if is_short_position else quantity
                exit_side = "BUY" if is_short_position else "SELL"
                if (
                    decision.action == "PROFIT"
                    and symbol not in self.pending_stock_exits
                    and self.cooldown_ready(key)
                ):
                    # A fractional-quantity position (bought via the core-
                    # session dollar-sizing path) can only be bought OR
                    # sold during core hours - Webull rejects any order on
                    # a non-integer quantity outside core hours regardless
                    # of order type. Retrying every cycle just spams the
                    # same rejection until the next core session, so skip
                    # (and count it like any other gate) instead. Shorts
                    # are always whole-share (see the SHORT entry branch
                    # above), so this is effectively a long-only check.
                    exit_is_fractional = self.is_fractional_quantity(exit_quantity)
                    if exit_is_fractional and not core_session_active:
                        self.gate_rejections[
                            "fractional position - exit waits for core hours"
                        ] += 1
                        continue
                    # Webull rejects ANY order (entry or exit, either side)
                    # under 100 shares while price sits in $0.10-$0.999,
                    # regardless of how many shares are actually held - a
                    # position smaller than that, caught in this band
                    # (e.g. price drifted down into it after entry), can't
                    # be exited by a normal order at all until price moves
                    # back out of the band. Retrying every cycle just spams
                    # the same rejection, so skip (and count it) instead.
                    if self.strategy.exit_blocked_by_lot_restriction(exit_quantity, price):
                        self.gate_rejections[
                            "sub-$1 lot-restricted band - exit waits for "
                            "price to clear it"
                        ] += 1
                        continue
                    target = decision.target_price
                    if target is None:
                        continue
                    # Live incident (this bug, caught from a real trade
                    # log): "PROFIT LEDS entry=2.00 exit=None pnl=-0.01"
                    # - a PROFIT-type decision that closed at a real
                    # loss with NO limit price at all, because
                    # should_force_market_exit had tripped after too
                    # many consecutive unfilled attempts and forced an
                    # actual, completely unprotected MARKET order. This
                    # is the same class of bug already fixed for the
                    # escalation (stop_loss_escalated) pathway just
                    # below - correct for a genuine stop-loss (guarantee
                    # execution even at a worse price bounds the loss),
                    # backwards for a profit-take (forces a fill "at any
                    # price," converting an intended profit into a
                    # guaranteed loss). PROFIT exits never force-market -
                    # a symbol that genuinely can't get a profitable
                    # fill just keeps waiting (the elif/else branches
                    # below already skip via `continue` when nothing
                    # fillable-and-profitable exists), same as this
                    # cohort's own "average down instead of forcing a
                    # bad exit" philosophy. should_force_market_exit
                    # stays fully in effect for the separate STOP-loss
                    # exit path elsewhere, where it's correct.
                    force_market = False
                    if is_short_position:
                        bid = self.api.quote_bid(quote)
                        # Mirror of the long case below: never cover above
                        # the target that triggered this, but also don't
                        # rest the limit above the current bid (that would
                        # be paying more than the market for no reason).
                        limit_price = (
                            self.api.stock_limit_price(quote, "COVER")
                            if symbol in self.stop_loss_escalated
                            else (min(bid, target) if bid else target)
                        )
                    elif (
                        self.strategy.is_volatility_scalp_eligible(symbol)
                        or symbol in self.volatility_scalp_positions
                    ):
                        # Condensed onto eligibility alone, not the
                        # narrower curated self.volatility_scalp_symbols
                        # cohort list - see the fresh-entry block above.
                        # Also keeps this pricing for an already-adopted
                        # cohort position even if it's no longer live-
                        # eligible this exact cycle (same reasoning as
                        # the exit-override gate above) - a position
                        # with real capital committed across several
                        # averaging buys shouldn't silently downgrade to
                        # the tight-spread-only general pricing right
                        # when it most needs the wider-spread-tolerant
                        # exit logic to find a fillable price.
                        # By request: "the sell price has to be
                        # reasonable" - live incident, GAUZ. Resting at
                        # the raw ask isn't actually "reasonable" on a
                        # wide-spread penny stock (GAUZ: bid=0.40,
                        # ask=0.43, a 7.5% spread) - that's the top of
                        # the book, not a price anyone's actually buying
                        # at, so the order just sits unfilled the same
                        # way a fixed target did. Reuses _stall_exit_
                        # price's already-correct logic instead: takes
                        # the bid immediately if it alone clears cost (a
                        # real, guaranteed-fill profit), only falls back
                        # to resting at the ask if the bid doesn't clear
                        # but the ask does AND the spread itself isn't
                        # absurdly wide (same spread-sanity check the
                        # stall-breaker already uses). Skips the cycle
                        # entirely (continue) rather than resting at an
                        # unreliable price if neither holds.
                        if exit_is_fractional:
                            fee_per_share = self.config.sell_fee_dollars
                        else:
                            fee_per_share = (
                                self.config.sell_fee_dollars / exit_quantity
                            )
                        min_profit = cost * self.config.volatility_scalp_target_percent
                        # Live incident (this bug, caught from a real
                        # trade log): "PROFIT"-labeled orders were
                        # closing at a REAL LOSS (LSTA: bought $1.59,
                        # sold $1.50; GOAI: bought $2.52, sold $2.42
                        # twice) - the escalation path used to switch to
                        # self.api.stock_limit_price(quote, "SELL"), a
                        # raw aggressive-cross price with NO floor at
                        # cost at all, once a symbol sat in self.
                        # stop_loss_escalated (15s unfilled). That
                        # tradeoff is correct for a genuine stop-loss
                        # (guarantee execution even at a worse price
                        # bounds the loss), but backwards for a
                        # PROFIT-take order - forcing a fill "at any
                        # price" converts an intended profit into a
                        # guaranteed loss, defeating the entire purpose
                        # of the order. ALWAYS use _stall_exit_price now,
                        # escalated or not - it already tries harder to
                        # find a fillable price (bid first, wider-
                        # tolerance ask fallback) without ever dropping
                        # below cost + min_profit + fee; if truly nothing
                        # fillable-and-profitable exists, the position
                        # just keeps waiting (continue below), which is
                        # exactly this cohort's own "average down
                        # instead of forcing a bad exit" philosophy.
                        limit_price = self._stall_exit_price(
                            quote,
                            cost,
                            min_profit,
                            fee_per_share,
                            max_spread_percent=(
                                self.config.volatility_scalp_max_exit_spread_percent
                            ),
                        )
                        if limit_price is None:
                            self.gate_rejections[
                                "volatility scalp - no reasonably fillable "
                                "profit price available yet"
                            ] += 1
                            continue
                    else:
                        ask = self.api.quote_ask(quote)
                        # ask can be below target - or even below cost - if
                        # the decision fired off a last-trade print
                        # (quote_price) that's already stale relative to
                        # the current book (the market moved down between
                        # the two reads). Never let a "profit-take"
                        # actually price below the target that triggered
                        # it, or it can silently execute at a real loss
                        # while still being logged as PROFIT.
                        #
                        # Same live incident/fix as the volatility-scalp
                        # branch above (LSTA/GOAI closed at a real loss
                        # while logged PROFIT): this comment already
                        # described the intended behavior correctly, but
                        # the code contradicted it - once escalated
                        # (self.stop_loss_escalated, 15s unfilled), it
                        # switched to a raw aggressive-cross price with
                        # NO floor at target/cost at all. Escalation
                        # should mean "try harder to fill," not "give up
                        # on price entirely" for an order whose whole
                        # purpose is realizing a profit. max(ask, target)
                        # unconditionally now - if the market genuinely
                        # can't offer a fillable price at or above
                        # target, the resting order (or the normal
                        # reprice_resting_exits cadence, which has its
                        # own "never chase below cost" guard) keeps
                        # waiting instead of forcing a loss.
                        limit_price = max(ask, target) if ask else target
                    # Live incident (WNW): a PROFIT exit escalated 5
                    # times over 6+ minutes, every attempt resubmitted
                    # at the EXACT same unfillable limit price (the
                    # bid/ask never actually crossed it) - "never
                    # force-market a profit-take" (see above) correctly
                    # avoids converting a stuck profit-take into an
                    # unprotected any-price market order, but had no
                    # give-up threshold at all, so it can wait forever
                    # even once it's clearly not a temporary stall. Also
                    # blocked the user's own manual sell override the
                    # whole time (see _manual_sell's "exit already
                    # pending" skip). By request: "it should be sold,
                    # maybe lower than the margin" - once
                    # should_force_market_exit's SAME threshold used for
                    # genuine stop-losses trips (consecutive_exit_
                    # failures, incremented once per escalation - see
                    # escalate_stalled_stop_losses), fall back to the
                    # current bid: still a real, currently-executable
                    # price (not a blind market order that could print
                    # far worse on a thin/illiquid name), just no longer
                    # gated on clearing the profit target.
                    if (
                        not is_short_position
                        and self.should_force_market_exit(
                            symbol, exit_is_fractional, core_session_active
                        )
                    ):
                        bid = self.api.quote_bid(quote)
                        if bid:
                            limit_price = bid
                    if force_market:
                        log.warning(
                            "ORDER  | %s | never filled %s times in a row - "
                            "forcing a market order to end the loop",
                            symbol,
                            self.consecutive_exit_failures.get(symbol, 0),
                        )
                    # Live incident (this bug): exits submitted here via
                    # the raw API call had NO fat-finger protection at
                    # all - price_sanity_ok (and its cooldown backoff)
                    # only ever got checked by callers going through
                    # place_stock_scaled, which entries use but exits
                    # never did. Cooldown checked FIRST (not after) so a
                    # symbol already known to be failing this check
                    # doesn't keep re-attempting and re-logging every
                    # single cycle - the exact "570 rejections in one
                    # day" pattern this fix targets. force_market is
                    # always False for PROFIT now (see the earlier fix
                    # removing it from this path), so limit_price is
                    # guaranteed a real price here, not None - safe to
                    # sanity-check.
                    if not self.price_sanity_cooldown_ready(symbol):
                        continue
                    if not self.price_sanity_ok(symbol, price, limit_price):
                        self.gate_rejections[
                            "volatility scalp - profit exit price failed "
                            "the sanity check"
                        ] += 1
                        continue
                    order_id = self.api.place_stock(
                        symbol,
                        exit_side,
                        exit_quantity,
                        limit_price=limit_price,
                        fractional=exit_is_fractional,
                        market=force_market,
                    )
                    self.pending_stock_exits.add(symbol)
                    self.stop_exit_submitted[symbol] = time.monotonic()
                    realized_price = limit_price if limit_price is not None else price
                    pnl = self.record_realized_exit(cost, realized_price, quantity)
                    self.record_trade(
                        key, order_id, "PROFIT", limit_price, pnl=pnl,
                        entry_price=cost, quantity=exit_quantity,
                    )
                if decision.action == "LOSS" and self.stop_ready_to_submit(key, symbol):
                    exit_is_fractional = self.is_fractional_quantity(exit_quantity)
                    if not self.stop_loss_confirmed(symbol):
                        self.gate_rejections[
                            "stop breach not yet confirmed - waiting out a "
                            "possible single-tick wick"
                        ] += 1
                        continue
                    if exit_is_fractional and not core_session_active:
                        self.gate_rejections[
                            "fractional position - exit waits for core hours"
                        ] += 1
                        continue
                    if self.strategy.exit_blocked_by_lot_restriction(exit_quantity, price):
                        self.gate_rejections[
                            "sub-$1 lot-restricted band - exit waits for "
                            "price to clear it"
                        ] += 1
                        continue
                    # Never price an initial stop-loss at the passive side -
                    # unlike a profit-take, a stop needs to fill fast to cap
                    # the loss, not rest passively hoping for a better price
                    # while the position keeps moving further away from it.
                    # stock_stop_exit_price (bid/ask midpoint) balances
                    # "don't overshoot the market" against "don't sit
                    # unfilled" for either direction - only escalation
                    # (after 15s unfilled) should cross the market harder
                    # than that.
                    force_market = self.should_force_market_exit(
                        symbol, exit_is_fractional, core_session_active
                    )
                    if force_market:
                        limit_price = None
                        log.warning(
                            "ORDER  | %s | never filled %s times in a row - "
                            "forcing a market order to end the loop",
                            symbol,
                            self.consecutive_exit_failures.get(symbol, 0),
                        )
                    else:
                        limit_price = (
                            self.api.stock_limit_price(
                                quote, "COVER" if is_short_position else "SELL"
                            )
                            if symbol in self.stop_loss_escalated
                            else self.api.stock_stop_exit_price(quote)
                        )
                    # Cooldown/sanity-check the stop's limit price just like
                    # the profit-exit path now does - but only when there IS
                    # a limit price. force_market's limit_price=None is a
                    # deliberate guaranteed-execution market order (after
                    # repeated unfilled attempts) and must never be blocked
                    # here - this guard exists to catch a bad LIMIT price,
                    # not to second-guess an intentional market order.
                    if limit_price is not None:
                        if not self.price_sanity_cooldown_ready(symbol):
                            continue
                        if not self.price_sanity_ok(symbol, price, limit_price):
                            self.gate_rejections[
                                "volatility scalp - stop exit price failed "
                                "the sanity check"
                            ] += 1
                            continue
                    order_id = self.api.place_stock(
                        symbol,
                        exit_side,
                        exit_quantity,
                        limit_price=limit_price,
                        fractional=exit_is_fractional,
                        market=force_market,
                    )
                    self.wash_sales.block(symbol, "stop-loss exit submitted")
                    self.pending_stock_exits.add(symbol)
                    self.stop_exit_submitted[symbol] = time.monotonic()
                    realized_price = limit_price if limit_price is not None else price
                    pnl = self.record_realized_exit(cost, realized_price, quantity)
                    self.record_trade(
                        key, order_id, "STOP", limit_price, pnl=pnl,
                        entry_price=cost, quantity=exit_quantity,
                    )
            except Exception as exc:
                self.stop_loss_escalated.discard(symbol)
                if isinstance(exc, QuoteUnavailableError):
                    continue
                if self.is_broker_position_conflict(exc):
                    self.handle_broker_conflict(symbol, exc)
                    continue
                if "BUYING_POWER_INSUFFICIENT" in str(exc):
                    buying_power = Decimal("0")
                    log.warning(
                        "FUNDS  | %s | buy skipped | insufficient buying power",
                        symbol,
                    )
                    continue
                if self.is_fractional_trading_not_enabled(exc):
                    self.handle_fractional_trading_not_enabled(exc)
                    continue
                if self.is_fractional_ticker_unsupported(exc):
                    self.handle_fractional_ticker_unsupported(symbol, exc)
                    continue
                if self.is_short_selling_unsupported(exc):
                    self.handle_short_selling_unsupported(exc)
                    continue
                if self.is_symbol_restricted_to_closing_only(exc):
                    self.handle_symbol_restricted_to_closing_only(symbol, exc)
                    continue
                log.error("STOCK  | %s | %s", symbol, exc)
        return buying_power

    def trade_pairs(self, positions: list[dict], buying_power: Decimal) -> Decimal:
        """Correlated-pairs mean reversion: long the relatively cheap leg,
        short the relatively expensive one, when the spread between two
        historically-correlated stocks stretches to a statistical extreme,
        and unwind when it reverts. See src/webull_bot/pairs.py. Its own
        capital slice (PAIRS_CAPITAL_FRACTION), carved out up front, so it
        never competes with the main scan's budget for the rest of the
        cycle.
        """
        if not PAIRS:
            return buying_power
        capital_budget = buying_power * PAIRS_CAPITAL_FRACTION
        per_pair_budget = (
            capital_budget / PAIRS_MAX_CONCURRENT if PAIRS_MAX_CONCURRENT else Decimal("0")
        )
        for pair in PAIRS:
            symbol_a, symbol_b = pair
            try:
                quote_a = self.api.stock_quote(
                    symbol_a, self.stock_categories.get(symbol_a, "US_STOCK")
                )
                quote_b = self.api.stock_quote(
                    symbol_b, self.stock_categories.get(symbol_b, "US_STOCK")
                )
                price_a = self.api.quote_price(quote_a)
                price_b = self.api.quote_price(quote_b)
            except Exception as exc:
                if isinstance(exc, QuoteUnavailableError):
                    continue
                log.error("PAIRS  | %s/%s | quote failed | %s", symbol_a, symbol_b, exc)
                continue
            self.pairs.update(pair, price_a, price_b)
            quote_by_symbol = {symbol_a: quote_a, symbol_b: quote_b}
            held = self.pairs_positions.get(pair)
            decision = self.pairs.decision(pair, is_open=held is not None)

            if held is None:
                if decision.action not in (
                    "ENTER_LONG_A_SHORT_B",
                    "ENTER_LONG_B_SHORT_A",
                ):
                    continue
                if not self.short_selling_supported:
                    continue
                if len(self.pairs_positions) >= PAIRS_MAX_CONCURRENT:
                    continue
                key_a, key_b = f"STOCK:{symbol_a}", f"STOCK:{symbol_b}"
                if not (
                    self.cooldown_ready(key_a)
                    and self.cooldown_ready(key_b)
                    and self.reentry_cooldown_ready(key_a)
                    and self.reentry_cooldown_ready(key_b)
                    and not self.rate_capped(key_a)
                    and not self.rate_capped(key_b)
                    and symbol_a not in self.broker_conflict_symbols
                    and symbol_b not in self.broker_conflict_symbols
                    and not self.wash_sales.blocked_until(symbol_a)
                    and not self.wash_sales.blocked_until(symbol_b)
                ):
                    continue
                existing_a, _ = self.api.stock_position(symbol_a, positions)
                existing_b, _ = self.api.stock_position(symbol_b, positions)
                if existing_a != 0 or existing_b != 0:
                    continue
                leg_budget = min(per_pair_budget, buying_power) / 2
                qty_a = int((leg_budget / price_a).to_integral_value(rounding=ROUND_DOWN))
                qty_b = int((leg_budget / price_b).to_integral_value(rounding=ROUND_DOWN))
                if qty_a <= 0 or qty_b <= 0:
                    continue
                if decision.action == "ENTER_LONG_A_SHORT_B":
                    long_symbol, long_qty = symbol_a, qty_a
                    short_symbol, short_qty = symbol_b, qty_b
                else:
                    long_symbol, long_qty = symbol_b, qty_b
                    short_symbol, short_qty = symbol_a, qty_a
                try:
                    long_order = self.place_stock_scaled(
                        long_symbol,
                        "BUY",
                        long_qty,
                        f"STOCK:{long_symbol}",
                        quote_by_symbol[long_symbol],
                    )
                except Exception as exc:
                    log.error(
                        "PAIRS  | %s/%s | long leg entry failed | %s",
                        symbol_a,
                        symbol_b,
                        exc,
                    )
                    continue
                if long_order is None:
                    continue
                try:
                    short_order = self.place_stock_scaled(
                        short_symbol,
                        "SHORT",
                        short_qty,
                        f"STOCK:{short_symbol}",
                        quote_by_symbol[short_symbol],
                    )
                except Exception as exc:
                    # The long leg is already working/filled with no short
                    # hedge behind it - unwind it immediately rather than
                    # leave a naked, unintended long. This is not a rare
                    # path on a sub-$2,000 account: Webull rejects every
                    # short with CAN_NOT_SELL_SHORT_FOR_LT_2K there, so
                    # this fires on every pairs entry attempt until equity
                    # clears that minimum.
                    if self.is_short_selling_unsupported(exc):
                        self.handle_short_selling_unsupported(exc)
                    elif self.is_fractional_ticker_unsupported(exc):
                        self.handle_fractional_ticker_unsupported(short_symbol, exc)
                    else:
                        log.error(
                            "PAIRS  | %s/%s | short leg entry failed | %s",
                            symbol_a,
                            symbol_b,
                            exc,
                        )
                    try:
                        self.api.place_stock(
                            long_symbol,
                            "SELL",
                            long_qty,
                            limit_price=self.api.stock_limit_price(
                                quote_by_symbol[long_symbol], "SELL"
                            ),
                        )
                    except Exception as unwind_exc:
                        log.error(
                            "PAIRS  | %s | failed to unwind orphaned long "
                            "leg after short leg rejection - check the "
                            "Webull app for a stuck naked position | %s",
                            long_symbol,
                            unwind_exc,
                        )
                    continue
                if short_order is None:
                    # The long leg is already working/filled with no
                    # short hedge behind it - unwind it immediately
                    # rather than leave a naked, unintended long.
                    self.api.place_stock(
                        long_symbol,
                        "SELL",
                        long_qty,
                        limit_price=self.api.stock_limit_price(
                            quote_by_symbol[long_symbol], "SELL"
                        ),
                    )
                    continue
                self.record_trade(
                    f"STOCK:{long_symbol}",
                    long_order,
                    "BUY",
                    entry_price=self.api.stock_limit_price(
                        quote_by_symbol[long_symbol], "BUY"
                    ),
                    quantity=long_qty,
                )
                self.record_trade(
                    f"STOCK:{short_symbol}",
                    short_order,
                    "BUY",
                    entry_price=self.api.stock_limit_price(
                        quote_by_symbol[short_symbol], "SHORT"
                    ),
                    quantity=short_qty,
                )
                self.position_buckets[long_symbol] = "PAIRS_LONG"
                self.position_buckets[short_symbol] = "PAIRS_SHORT"
                self.pairs_positions[pair] = {"long": long_symbol, "short": short_symbol}
                self.pairs.mark_entered(pair)
                buying_power = max(Decimal("0"), buying_power - leg_budget * 2)
                log.info(
                    "PAIRS  | %s/%s | entered | long=%s(%s) short=%s(%s) | z=%.2f",
                    symbol_a,
                    symbol_b,
                    long_symbol,
                    long_qty,
                    short_symbol,
                    short_qty,
                    decision.z_score,
                )
                continue

            if decision.action not in ("UNWIND", "STOP"):
                continue
            long_symbol, short_symbol = held["long"], held["short"]
            long_qty, long_cost = self.api.stock_position(long_symbol, positions)
            # A short position's quantity is reported negative (same
            # convention close_all_positions already relies on) - normalize
            # to a positive magnitude for order sizing/pnl below, but the
            # sign itself is what tells us whether the short is still open.
            short_position_qty, short_cost = self.api.stock_position(
                short_symbol, positions
            )
            short_qty = -short_position_qty if short_position_qty < 0 else Decimal("0")
            if long_qty <= 0 and short_qty <= 0:
                self.pairs_positions.pop(pair, None)
                self.pairs.mark_exited(pair)
                continue
            try:
                if long_qty > 0:
                    sell_price = self.api.stock_limit_price(
                        quote_by_symbol[long_symbol], "SELL"
                    )
                    order_id = self.api.place_stock(
                        long_symbol, "SELL", long_qty, limit_price=sell_price
                    )
                    pnl = self.record_realized_exit(long_cost, sell_price, long_qty)
                    self.record_trade(
                        f"STOCK:{long_symbol}",
                        order_id,
                        "PROFIT" if decision.action == "UNWIND" else "STOP",
                        sell_price,
                        pnl=pnl,
                        entry_price=long_cost,
                        quantity=long_qty,
                    )
                if short_qty > 0:
                    # Covering submits as a plain "BUY" order (Webull has
                    # no fourth order side), priced via the "COVER" pricing
                    # branch (crosses above the ask) so it fills with the
                    # same urgency any other forced exit gets.
                    cover_price = self.api.stock_limit_price(
                        quote_by_symbol[short_symbol], "COVER"
                    )
                    order_id = self.api.place_stock(
                        short_symbol, "BUY", short_qty, limit_price=cover_price
                    )
                    pnl = self.record_realized_exit(short_cost, cover_price, short_qty, multiplier=-1)
                    self.record_trade(
                        f"STOCK:{short_symbol}",
                        order_id,
                        "PROFIT" if decision.action == "UNWIND" else "STOP",
                        cover_price,
                        pnl=pnl,
                        entry_price=short_cost,
                        quantity=short_qty,
                    )
            except Exception as exc:
                log.error(
                    "PAIRS  | %s/%s | unwind failed | %s", symbol_a, symbol_b, exc
                )
                continue
            self.pairs_positions.pop(pair, None)
            self.pairs.mark_exited(pair)
            log.info(
                "PAIRS  | %s/%s | unwound (%s) | z=%.2f",
                symbol_a,
                symbol_b,
                decision.reason,
                decision.z_score,
            )
        return buying_power

    def trade_options(
        self,
        positions: list[dict],
        buying_power: Decimal,
    ) -> Decimal:
        """Direction-aware options entries: a call needs a bullish
        underlying, a put needs a bearish one - see
        strategy.option_direction_signal/option_entry_confirmed. Exit
        management (profit target, stop, DTE-forced close) is unrelated to
        direction and stays keyed off strategy.option_decision.
        """
        if not self.options_enabled:
            return buying_power
        open_count = self.strategy.open_position_count(positions)
        # See stop_loss_guard_active() / trade_stocks - same freqtrade-
        # style frequency-based entry pause, applied here too.
        guard_active = self.stop_loss_guard_active()
        # A fresh underlying quote per cycle, decoupled from whatever batch
        # the stock-scanning path happens to be covering this cycle - the
        # direction signal must never silently run on stale/absent state.
        # VIXY rides along in the same batched call (real VIX/CGIF index
        # data isn't reachable through the OpenAPI - confirmed live) to
        # track a market-wide volatility regime gate every cycle.
        #
        # By request ("still isn't buying options"): deliberately reads
        # from self.option_contracts (every discovered contract), NOT
        # the option-QUOTE rotation (hard-capped at 20 by Webull's own
        # per-call option-snapshot limit). Live incident: with 106
        # contracts discovered (~53 underlyings), option_direction_
        # signal's EMA(3/8) needs 9 price samples per underlying, but
        # each underlying was only getting ONE new sample every ~5
        # cycles (20 contracts / 2-per-underlying = 10 underlyings
        # covered per rotation) - meaning ~45+ cycles before ANY
        # underlying could even show a crossover, and that number only
        # gets worse as discovery finds more contracts. Stock quotes
        # have a much higher per-call batch limit than option quotes
        # (stock_quotes_resilient already chunks internally), so
        # there's no reason to starve direction-signal history at the
        # option-quote batch's much tighter pace - every known
        # underlying now gets a fresh sample every single cycle,
        # completely decoupled from the option-contract quote rotation.
        underlyings = sorted(
            {contract["underlying_symbol"] for contract in self.option_contracts}
        )
        quote_symbols = sorted(set(underlyings) | {OPTION_VIXY_SYMBOL})
        underlying_quote_by_symbol: dict[str, dict] = {}
        current_vixy: Decimal | None = None
        try:
            fetched_quotes, _ = self.api.stock_quotes_resilient(
                quote_symbols, "US_STOCK"
            )
            for fetched_quote in fetched_quotes:
                symbol = str(fetched_quote.get("symbol", "")).upper()
                if symbol == OPTION_VIXY_SYMBOL:
                    current_vixy = self.api.quote_price(fetched_quote)
                    self.vixy_history.append(current_vixy)
                else:
                    underlying_quote_by_symbol[symbol] = fetched_quote
        except Exception as exc:
            log.warning(
                "OPTIONS | underlying quote batch failed | new entries "
                "skipped this cycle, exits unaffected | %s", exc,
            )
        directions: dict[str, str] = {}
        for underlying, underlying_quote in underlying_quote_by_symbol.items():
            underlying_price = self.api.quote_price(underlying_quote)
            directions[underlying] = self.strategy.option_direction_signal(
                f"OPTU:{underlying}", underlying_price
            )
        # By request ("watch... why didn't you let me know"): direct
        # visibility into whether the direction-signal pipeline is
        # actually alive, instead of inferring health from the absence
        # of an order - a real CALL/PUT crossover being genuinely rare
        # on calm blue-chip names looks IDENTICAL, from the outside, to
        # something silently broken (a quote batch quietly returning
        # too few symbols, a history dict never actually accumulating).
        # Logged once/cycle regardless of outcome, same "state, not
        # just events" visibility SCAN/GATES already give the stock side.
        signal_counts: dict[str, int] = defaultdict(int)
        for value in directions.values():
            signal_counts[value] += 1
        log.info(
            "OPTIONS | direction signals | quoted=%s/%s | CALL=%s PUT=%s HOLD=%s",
            len(underlying_quote_by_symbol),
            len(underlyings),
            signal_counts.get("CALL", 0),
            signal_counts.get("PUT", 0),
            signal_counts.get("HOLD", 0),
        )
        # Live incident (this bug): the fix above made direction signals
        # fire constantly (CALL/PUT counts logged nonzero repeatedly),
        # yet option_gate_rejections NEVER recorded a single rejection
        # past "no direction signal for this underlying" - because the
        # per-CONTRACT gate-check loop below only ever evaluated a
        # blind round-robin rotation of option_batch_size (20) out of
        # the full, continuously-growing option_contracts list (136+
        # and climbing while discovery is still running). A contract
        # whose underlying briefly signals CALL/PUT this cycle has no
        # guarantee of being IN that cycle's 20-wide rotation - by the
        # time round-robin reaches it again, the fast EMA(3/8) signal
        # has often already reverted to HOLD. Signals were real; they
        # just almost never lined up with the narrow gate-check window.
        # Fix: put every contract whose underlying has a LIVE, matching
        # signal into this cycle's batch first (capped at option_batch_
        # size, since option_quotes hard-rejects a request over 20
        # symbols), then fill any remaining room from the normal
        # rotating cursor so non-signaling contracts still get their IV
        # history refreshed and stay in exit-management coverage.
        priority_contracts = [
            contract
            for contract in self.option_contracts
            if directions.get(contract["underlying_symbol"]) == contract.get(
                "option_type"
            )
        ][: self.config.option_batch_size]
        fill_size = max(
            0, self.config.option_batch_size - len(priority_contracts)
        )
        rotation, self.option_cursor = self.strategy.rotating_batch(
            self.option_contracts, self.option_cursor, fill_size
        )
        seen_symbols: set[str] = set()
        batch: list[dict] = []
        for contract in priority_contracts + rotation:
            symbol = contract["symbol"]
            if symbol not in seen_symbols:
                seen_symbols.add(symbol)
                batch.append(contract)
        batch = batch[: self.config.option_batch_size]
        try:
            quotes = self.api.option_quotes(
                [contract["symbol"] for contract in batch]
            )
        except Exception as exc:
            if isinstance(exc, MarketDataPermissionError):
                self.options_enabled = False
                log.warning(
                    "OPTIONS | disabled | OPRA OpenAPI quotes not subscribed"
                )
                return buying_power
            log.error("OPTIONS | quote batch failed | %s", exc)
            return buying_power
        quote_by_symbol = {
            str(quote.get("symbol", "")).upper(): quote for quote in quotes
        }
        today = date.today()
        for contract in batch:
            option_symbol = contract["symbol"]
            key = f"OPTION:{option_symbol}"
            if option_symbol in self.broker_conflict_symbols:
                continue
            try:
                quote = quote_by_symbol.get(option_symbol)
                if not quote:
                    continue
                price = self.api.quote_price(quote)
                quantity, cost = self.api.option_position(contract, positions)
                days_to_expiration = (
                    date.fromisoformat(contract["expiration_date"]) - today
                ).days
                current_iv = self.api.option_implied_vol(quote)
                if current_iv is not None:
                    self.option_iv_history[option_symbol].append(current_iv)
                if quantity == 0:
                    self.pending_option_exits.discard(option_symbol)
                    # By request ("scan through everything... figure out
                    # what you missed"): live evidence showed real CALL/
                    # PUT signals firing constantly all day (169/186
                    # cycles had at least one), yet zero option orders
                    # ever placed - something downstream of the signal
                    # was silently blocking every single one, with NO
                    # diagnostic visibility on any of these gates
                    # (unlike the stock side's GATES summary). Every
                    # rejection point below now counts into option_
                    # gate_rejections (a dedicated dict, same pattern as
                    # avgdown_gate_rejections, so it doesn't get
                    # crowded out of the shared gate_rejections summary
                    # by the far more numerous stock-side reasons) -
                    # logged periodically to actually see which gate is
                    # the real blocker instead of guessing.
                    if days_to_expiration <= self.config.option_min_hold_dte:
                        self.option_gate_rejections[
                            "too close to expiration"
                        ] += 1
                        continue
                    underlying = contract["underlying_symbol"]
                    direction = directions.get(underlying, "HOLD")
                    contract_type = contract.get("option_type")
                    # By explicit request, for a one-off diagnostic
                    # ("make sure it fires... no barrier, quickly sell
                    # it, and then change the option strategy again"):
                    # option_smoke_test_mode skips every entry-QUALITY
                    # gate below (direction signal, delta, IV
                    # percentile, market regime, wash-sale, stop-loss
                    # guard, quarantine) - structural checks (DTE,
                    # affordability/sizing, cooldown, rate cap, max
                    # open positions) still apply below, so this can't
                    # spam unlimited orders. Off by default; meant to
                    # be turned back off (and OPTION_TAKE_PROFIT_
                    # PERCENT restored) once a real end-to-end trade is
                    # confirmed.
                    if not self.config.option_smoke_test_mode:
                        if not (
                            (contract_type == "CALL" and direction == "CALL")
                            or (contract_type == "PUT" and direction == "PUT")
                        ):
                            self.option_gate_rejections[
                                "no direction signal for this underlying"
                            ] += 1
                            continue
                        # Live incident (this bug, found right after the
                        # option-batch-priority fix started actually
                        # letting real signals reach this loop): tick/
                        # order-flow confirmation was the dominant remaining
                        # blocker (1-5 rejections every cycle) - it re-
                        # checked the SAME "OPTU:" price series option_
                        # direction_signal's EMA cross just fired on, but
                        # only advances one sample per ~2-minute cycle, so
                        # its 10-sample window spans ~20 minutes and could
                        # easily disagree with a signal that just flipped
                        # THIS cycle. By explicit request: removed for
                        # options - trust the EMA direction signal on its
                        # own, same as the stock side already effectively
                        # does once tick_direction_ok's own separate check
                        # passes.
                        if not self.strategy.option_delta_ok(
                            self.api.option_delta(quote)
                        ):
                            self.option_gate_rejections["delta out of range"] += 1
                            continue
                        if not self.strategy.option_iv_percentile_ok(
                            self.option_iv_history[option_symbol], current_iv
                        ):
                            self.option_gate_rejections["IV percentile failed"] += 1
                            continue
                        if not self.strategy.option_market_regime_ok(
                            self.vixy_history, current_vixy
                        ):
                            self.option_gate_rejections[
                                "market regime (VIXY) gate active"
                            ] += 1
                            continue
                        blocked_until = self.wash_sales.blocked_until(underlying)
                        if blocked_until:
                            self.option_gate_rejections["wash-sale blocked"] += 1
                            if underlying not in self.wash_skip_logged:
                                self.wash_skip_logged.add(underlying)
                                log.info(
                                    "WASH   | %-8s | option entry blocked until %s",
                                    underlying,
                                    blocked_until.strftime("%Y-%m-%d"),
                                )
                            continue
                        self.wash_skip_logged.discard(underlying)
                        if guard_active:
                            self.gate_rejections[
                                "stop-loss guard active - too many recent stops"
                            ] += 1
                            self.option_gate_rejections[
                                "stop-loss guard active"
                            ] += 1
                            continue
                        if self.symbol_quarantined(key):
                            self.gate_rejections[
                                "symbol quarantined - recent net losses on "
                                "this symbol"
                            ] += 1
                            self.option_gate_rejections["symbol quarantined"] += 1
                            continue
                    limit_price = self.api.option_limit_price(quote, "BUY")
                    buy_quantity, contract_cost = (
                        self.strategy.option_order_quantity(
                            limit_price,
                            self.cached_option_buying_power,
                        )
                    )
                    if buy_quantity <= 0:
                        self.option_gate_rejections[
                            "sizing produced zero contracts (price/buying "
                            "power/risk cap)"
                        ] += 1
                    elif open_count >= self.config.max_open_positions:
                        self.option_gate_rejections["max open positions"] += 1
                    elif not self.cooldown_ready(key):
                        self.option_gate_rejections[
                            "order-submission cooldown"
                        ] += 1
                    elif self.rate_capped(key):
                        self.option_gate_rejections["hourly rate cap"] += 1
                    elif not self.reentry_cooldown_ready(key):
                        self.option_gate_rejections["reentry cooldown"] += 1
                    if (
                        open_count < self.config.max_open_positions
                        and buy_quantity > 0
                        and self.cooldown_ready(key)
                        and not self.rate_capped(key)
                        and self.reentry_cooldown_ready(key)
                    ):
                        order_id = self.api.place_option(
                            contract,
                            "BUY",
                            buy_quantity,
                            limit_price,
                            "BUY_TO_OPEN",
                        )
                        self.record_trade(
                            key,
                            order_id,
                            "BUY",
                            entry_price=limit_price,
                            quantity=buy_quantity,
                        )
                        buying_power = max(
                            Decimal("0"),
                            buying_power - contract_cost * buy_quantity,
                        )
                        positions.append(
                            {
                                "instrument_type": "OPTION",
                                "symbol": option_symbol,
                                "quantity": str(buy_quantity),
                            }
                        )
                        open_count += 1
                    continue
                decision = self.strategy.option_decision(
                    price,
                    quantity,
                    cost,
                    days_to_expiration,
                )
                if (
                    decision.action == "PROFIT"
                    and option_symbol not in self.pending_option_exits
                    and self.cooldown_ready(key)
                ):
                    if decision.target_price is None:
                        continue
                    target = decision.target_price.quantize(Decimal("0.01"))
                    limit_price = max(
                        target,
                        self.api.option_limit_price(quote, "SELL"),
                    )
                    order_id = self.api.place_option(
                        contract,
                        "SELL",
                        quantity,
                        limit_price,
                        "SELL_TO_CLOSE",
                    )
                    self.pending_option_exits.add(option_symbol)
                    pnl = self.record_realized_exit(cost, limit_price, quantity, multiplier=100)
                    self.record_trade(
                        key, order_id, "PROFIT", limit_price, pnl=pnl,
                        entry_price=cost, quantity=quantity,
                    )
                if (
                    decision.action == "LOSS"
                    and option_symbol not in self.pending_option_exits
                    and self.cooldown_ready(key)
                ):
                    limit_price = self.api.option_limit_price(quote, "SELL")
                    order_id = self.api.place_option(
                        contract,
                        "SELL",
                        quantity,
                        limit_price,
                        "SELL_TO_CLOSE",
                    )
                    self.wash_sales.block(
                        contract["underlying_symbol"],
                        "option stop-loss exit submitted",
                    )
                    self.pending_option_exits.add(option_symbol)
                    pnl = self.record_realized_exit(cost, limit_price, quantity, multiplier=100)
                    self.record_trade(
                        key, order_id, "STOP", limit_price, pnl=pnl,
                        entry_price=cost, quantity=quantity,
                    )
            except Exception as exc:
                if isinstance(exc, QuoteUnavailableError):
                    continue
                if self.is_broker_position_conflict(exc):
                    self.handle_broker_conflict(option_symbol, exc)
                    continue
                if "BUYING_POWER_INSUFFICIENT" in str(exc):
                    buying_power = Decimal("0")
                    log.warning(
                        "FUNDS  | %s | buy skipped | insufficient buying power",
                        option_symbol,
                    )
                    continue
                log.error("OPTION | %s | %s", option_symbol, exc)
        return buying_power

    def run(self) -> None:
        log.info(
            "START  | mode=%s | poll=%ss | cooldown=%ss",
            self.config.mode,
            self.config.poll_seconds,
            self.config.trade_cooldown_seconds,
        )
        threading.Thread(
            target=self._position_protection_loop, daemon=True
        ).start()
        while True:
            moment = self.now()
            if not self.is_trading_day(moment):
                time.sleep(60)
                continue

            market_open = self.session_moment(moment, self.config.market_open_time)
            closeout = self.session_moment(moment, self.config.eod_close_time)
            market_close = self.session_moment(moment, self.config.market_close_time)
            option_open = self.session_moment(
                moment,
                self.config.option_market_open_time,
            )
            option_closeout = self.session_moment(
                moment,
                self.config.option_eod_close_time,
            )
            option_close = self.session_moment(
                moment,
                self.config.option_market_close_time,
            )

            if moment < market_open:
                # By request: "get the top gainers before the day
                # starts" - fetched here specifically so it still runs
                # during the pre-market wait below, not skipped by the
                # early continue on this branch. Once-per-day guarded
                # internally (see refresh_premarket_gainers), so this
                # is a no-op on every later tick of the same wait.
                self.refresh_premarket_gainers(moment)
                self.refresh_agent_predicted_gainers(moment)
                time.sleep(min(60, max(1, (market_open - moment).total_seconds())))
                continue

            if closeout <= moment < market_close:
                finished = self.close_instruments(
                    {"EQUITY"}, apply_overnight_hold=True
                )
                time.sleep(60 if finished else self.config.eod_retry_seconds)
                continue

            if moment >= market_close:
                self.log_day_end_summary(moment)
                time.sleep(60)
                continue

            if option_closeout <= moment < option_close:
                self.close_instruments({"OPTION"})
                self.close_fractional_positions_before_core_close()

            opening_grace_active = option_open <= moment < option_open + timedelta(
                minutes=self.config.opening_grace_minutes
            )
            core_session_active = option_open <= moment < option_close
            # Read by _position_protection_loop (a separate thread) -
            # see its docstring. Plain attribute assignment is atomic
            # under the GIL, same convention already used for
            # stock_symbols/stock_categories in resolve_targets.
            self.cached_core_session_active = core_session_active
            if opening_grace_active and self.opening_grace_logged_date != moment.date():
                self.opening_grace_logged_date = moment.date()
                log.info(
                    "GATES  | opening grace window active for %s minutes | "
                    "spread/extension gates relaxed %sx/%sx near the bell",
                    self.config.opening_grace_minutes,
                    self.config.opening_grace_spread_multiplier,
                    self.config.opening_grace_extension_multiplier,
                )

            cycle_started = time.monotonic()
            try:
                self.resolve_targets(moment)
                # Fallback call site for a restart that lands AFTER
                # market_open (common - most deploys land mid-session,
                # not during the pre-market wait above) - without this,
                # refresh_premarket_gainers would never fire that day
                # at all. Its own internal once-per-day guard makes
                # this a no-op on every cycle after the first.
                self.refresh_premarket_gainers(moment)
                self.refresh_agent_predicted_gainers(moment)
                # monitor_working_orders/the repricers/escalate_stalled_
                # stop_losses now run on their own fast, dedicated
                # thread (see _position_protection_loop, started once
                # above) - NOT called here too, or they'd run twice
                # concurrently and double-cancel/double-reprice the
                # same working orders.
                self.process_iceberg_orders()
                if not core_session_active:
                    self.close_profitable_positions_during_extended_hours()
                self.select_volatility_scalp_symbols()
                self.reconcile_order_history()
                self.log_trade_events()
                buying_power, positions = self.account_state()
                buying_power = self.process_ui_commands(
                    positions, buying_power, core_session_active
                )
                circuit_active = self.handle_portfolio_circuit_breaker(
                    positions,
                    buying_power,
                )
                if not circuit_active:
                    circuit_active = self.handle_daily_loss_breaker()
                if not circuit_active:
                    # By explicit request: "I want priority to option
                    # trades, so make sure you buy options first."
                    # Options and stocks draw from completely separate
                    # buying-power pools (see account_state/
                    # cached_option_buying_power's own comment), so
                    # this reordering doesn't take capital away from
                    # stocks - it just means an option candidate gets
                    # first crack at this cycle's evaluation instead of
                    # being evaluated after (and therefore effectively
                    # de-prioritized behind) the general/scalp stock
                    # paths every single cycle.
                    if option_open <= moment < option_closeout:
                        self.discover_option_contracts()
                        buying_power = self.trade_options(positions, buying_power)
                    buying_power = self.trade_pairs(positions, buying_power)
                    buying_power = self.trade_stocks(
                        positions,
                        buying_power,
                        opening_grace_active,
                        core_session_active,
                    )
                    self.boost_stalled_positions(
                        positions,
                        option_open <= moment < option_closeout,
                        core_session_active,
                    )
                    self.cached_buying_power = buying_power
                    self.cached_positions = [dict(item) for item in positions]
                    self.submit_strategy_review(positions, buying_power)
                self.write_status_snapshot(positions, buying_power, circuit_active)
                if time.monotonic() - self.last_status_log >= 1:
                    self.last_status_log = time.monotonic()
                    log.info(
                        "SCAN   | stocks=%s/%s | options=%s/%s | positions=%s | "
                        "buying power=$%.2f | pnl today=$%.2f | watchlist=%s | paused=%s",
                        min(self.last_scan_batch_size, len(self.stock_symbols))
                        or min(self.config.stock_batch_size, len(self.stock_symbols)),
                        len(self.stock_symbols),
                        min(self.config.option_batch_size, len(self.option_contracts)),
                        len(self.option_contracts),
                        self.strategy.open_position_count(positions),
                        buying_power,
                        self.daily_realized_pnl,
                        len(self.user_watchlist),
                        "YES" if circuit_active else "NO",
                    )
                    if self.gate_rejections:
                        top_reasons = sorted(
                            self.gate_rejections.items(),
                            key=lambda item: item[1],
                            reverse=True,
                        )[:5]
                        log.info(
                            "GATES  | entries not yet firing because | %s",
                            " | ".join(
                                f"{reason}={count}" for reason, count in top_reasons
                            ),
                        )
                        self.gate_rejections.clear()
                    # By request: "it is not averaging down at all" -
                    # by request. Its own dedicated counter/summary
                    # (not the shared gate_rejections dict above) - the
                    # averaging-down diagnostic (see trade_stocks)
                    # otherwise gets crowded out of the top-5 GATES
                    # summary by the far more numerous fresh-entry
                    # rejection reasons every single cycle, leaving
                    # zero real visibility into why averaging down
                    # isn't firing despite being logged.
                    if self.avgdown_gate_rejections:
                        avgdown_top_reasons = sorted(
                            self.avgdown_gate_rejections.items(),
                            key=lambda item: item[1],
                            reverse=True,
                        )[:5]
                        log.info(
                            "AVGDOWN| not adding because | %s",
                            " | ".join(
                                f"{reason}={count}"
                                for reason, count in avgdown_top_reasons
                            ),
                        )
                        self.avgdown_gate_rejections.clear()
                    if self.option_gate_rejections:
                        option_top_reasons = sorted(
                            self.option_gate_rejections.items(),
                            key=lambda item: item[1],
                            reverse=True,
                        )[:5]
                        log.info(
                            "OPTIONS| not entering because | %s",
                            " | ".join(
                                f"{reason}={count}"
                                for reason, count in option_top_reasons
                            ),
                        )
                        self.option_gate_rejections.clear()
            except Exception as exc:
                if isinstance(exc, MarketDataPermissionError):
                    log.critical("STOP   | %s", exc)
                    return
                log.error("CYCLE  | failed | %s", exc)

            seconds_to_closeout = max(
                1.0,
                (closeout - self.now()).total_seconds(),
            )
            cycle_elapsed = time.monotonic() - cycle_started
            delay = max(0.0, float(self.config.poll_seconds) - cycle_elapsed)
            if delay:
                time.sleep(min(delay, seconds_to_closeout))


def force_close_all() -> None:
    config = settings()
    config.validate_connection(require_account=True)
    api = WebullAPI(config)
    timezone = ZoneInfo(config.trading_timezone)
    wash_sales = WashSaleTracker(
        config.wash_sale_state_file,
        config.wash_sale_block_days,
        timezone,
        log,
    )
    log.warning("MANUAL | cancelling orders and closing every account position")
    submitted = api.close_all_positions(loss_callback=wash_sales.block)
    remaining = [
        item
        for item in api.positions()
        if Decimal(str(item.get("quantity", "0"))) != 0
    ]
    log.warning(
        "MANUAL | submitted=%s | currently remaining=%s",
        len(submitted),
        len(remaining),
    )
