import logging
import threading
import time
from collections import defaultdict, deque
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_UP
from zoneinfo import ZoneInfo

from rich.logging import RichHandler

from webull_bot.analyst_data import AnalystDataService
from webull_bot.commands import CommandQueue
from webull_bot.config import settings
from webull_bot.daily_pnl import DailyPnlTracker
from webull_bot.errors.broker_conflict import is_broker_position_conflict
from webull_bot.errors.fractional_ticker import is_fractional_ticker_unsupported
from webull_bot.errors.otc_extended_hours import is_otc_extended_hours_unsupported
from webull_bot.errors.sell_with_no_position import is_sell_with_no_position
from webull_bot.errors.fractional_trading import is_fractional_trading_not_enabled
from webull_bot.errors.order_cancellation import is_order_not_cancelable
from webull_bot.errors.order_reverses_position import (
    is_order_reverses_existing_position,
)
from webull_bot.errors.short_selling import is_short_selling_unsupported
from webull_bot.errors.symbol_restrictions import is_symbol_restricted_to_closing_only
from webull_bot.invalid_symbols import InvalidSymbolTracker
from webull_bot.option_contracts_state import OptionContractsStateStore
from webull_bot.market_agent import MarketResearchAgent
from webull_bot.pairs import PairsStrategy
from webull_bot.risk.entry_blackout import fresh_entry_blackout_active
from webull_bot.risk.options_priority_window import options_priority_window_active
from webull_bot.risk.profit_target_multiplier import profit_target_multiplier
from webull_bot.risk.stock_total_exposure import stock_total_exposure_at_cap
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
from webull_bot.trading.handlers.otc_extended_hours_handler import (
    handle_otc_extended_hours_unsupported,
)
from webull_bot.trading.handlers.short_selling_handler import (
    handle_short_selling_unsupported,
)
from webull_bot.trading.handlers.symbol_restriction_handler import (
    handle_symbol_restricted_to_closing_only,
)
from webull_bot.trading.main_loop import run
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
from webull_bot.trading.options.option_entry_exit import (
    _evaluate_option_entry,
    _evaluate_option_exit,
)
from webull_bot.trading.options.option_scan_batch import _prepare_option_scan_batch
from webull_bot.trading.orders.close_instruments import close_instruments
from webull_bot.trading.orders.exit_failure_tracking import _note_exit_failure
from webull_bot.trading.orders.force_market_exit import should_force_market_exit
from webull_bot.trading.orders.held_exit_evaluation import evaluate_held_stock_exits
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
from webull_bot.trading.pairs.pair_trading import trade_pairs
from webull_bot.trading.quoting.batched_quotes import _batched_quotes
from webull_bot.trading.quoting.stall_equity_quotes import _stall_equity_quotes
from webull_bot.trading.quoting.stall_exit_price import _stall_exit_price
from webull_bot.trading.repricing.resting_entry_repricer import (
    reprice_resting_entries,
)
from webull_bot.trading.repricing.resting_exit_repricer import reprice_resting_exits
from webull_bot.trading.repricing.resting_option_entry_repricer import (
    reprice_resting_option_entries,
)
from webull_bot.trading.repricing.resting_option_exit_repricer import (
    reprice_resting_option_exits,
)
from webull_bot.trading.repricing.scalp_entry_repricer import (
    reprice_volatility_scalp_entries,
)
from webull_bot.trading.repricing.scalp_exit_repricer import (
    reprice_volatility_scalp_exits,
)
from webull_bot.trading.repricing.stop_loss_escalation import (
    escalate_stalled_stop_losses,
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
from webull_bot.trading.screeners.stock_scan_batch import _prepare_stock_scan_batch
from webull_bot.trading.screeners.strategy_review import submit_strategy_review
from webull_bot.trading.stocks.stock_symbol_processing import (
    _StockScanState,
    _process_stock_symbol,
)
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
    is_sell_with_no_position = staticmethod(is_sell_with_no_position)
    is_otc_extended_hours_unsupported = staticmethod(is_otc_extended_hours_unsupported)
    is_fractional_trading_not_enabled = staticmethod(is_fractional_trading_not_enabled)
    is_order_not_cancelable = staticmethod(is_order_not_cancelable)
    is_order_reverses_existing_position = staticmethod(
        is_order_reverses_existing_position
    )
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
    options_priority_window_active = staticmethod(options_priority_window_active)
    stock_total_exposure_at_cap = staticmethod(stock_total_exposure_at_cap)
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
    handle_otc_extended_hours_unsupported = handle_otc_extended_hours_unsupported
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
    reprice_resting_option_entries = reprice_resting_option_entries
    reprice_resting_option_exits = reprice_resting_option_exits
    escalate_stalled_stop_losses = escalate_stalled_stop_losses
    evaluate_held_stock_exits = evaluate_held_stock_exits
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
    _prepare_stock_scan_batch = _prepare_stock_scan_batch
    _process_stock_symbol = _process_stock_symbol
    # Iceberg/scaled order placement and its slice-processing follow-up -
    # moved out to trading/orders/.
    place_stock_scaled = place_stock_scaled
    process_iceberg_orders = process_iceberg_orders
    # Order-history reconciliation audit - moved out to trading/orders/.
    reconcile_order_history = reconcile_order_history
    # Core trade-lifecycle bookkeeping - moved out to trading/orders/.
    record_trade = record_trade
    trade_pairs = trade_pairs
    # Options-chain discovery - moved out to trading/options/.
    discover_option_contracts = discover_option_contracts
    _prepare_option_scan_batch = _prepare_option_scan_batch
    _evaluate_option_entry = _evaluate_option_entry
    _evaluate_option_exit = _evaluate_option_exit
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
    run = run

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
        # Options analog of the three dicts above - by request: "you can
        # also use averaging down... for options as well." Same
        # widening-ladder/strictly-lower-than-last-buy/reset-on-close
        # shape, keyed by option contract symbol instead of stock symbol.
        self.option_average_down_count: dict[str, int] = defaultdict(int)
        self.last_option_average_down: dict[str, float] = {}
        self.option_last_buy_price: dict[str, Decimal] = {}
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
        # By request: "is there a way to save these option contracts" -
        # restores whatever discover_option_contracts had already found
        # in a prior session instead of paying the whole discovery
        # ramp-up cost again on every restart. See
        # OptionContractsStateStore's docstring.
        self.option_contracts_state = OptionContractsStateStore(
            self.config.option_contracts_state_file, log
        )
        restored_contracts, restored_attempted, restored_averaging = (
            self.option_contracts_state.load()
        )
        if restored_contracts:
            self.option_contracts = restored_contracts
            self.option_discovery_attempted = restored_attempted
        # By request ("do a full on options sanity check") - restores
        # each held position's averaging-down ladder (count + last
        # buy price) too, so a position that already used some/all of
        # its allowed averaging-down buys before a restart doesn't
        # get a fresh allotment after one. See OptionContractsState
        # Store's docstring for the full incident this fixes.
        for symbol, entry in restored_averaging.items():
            self.option_average_down_count[symbol] = entry["count"]
            self.option_last_buy_price[symbol] = entry["last_buy_price"]
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
        self.last_option_reprice = 0.0
        self.last_option_entry_reprice = 0.0
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
        # By request: "first we want an option trade to occur, and the
        # stock trading should start later" - see options_priority_
        # window_active/record_trade. Reset daily alongside the other
        # once-per-day counters in _resolve_targets_work_body.
        self.option_entry_occurred_today = False
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
        self.otc_extended_hours_unsupported_symbols: set[str] = set()
        self.short_selling_supported = True
        self.iceberg_orders: dict[str, dict] = {}
        self.order_error_times: deque = deque()
        self.pairs = PairsStrategy()
        self.pairs_positions: dict[tuple[str, str], dict] = {}
        self.last_pairs_sample = 0.0

    def trade_stocks(
        self,
        positions: list[dict],
        buying_power: Decimal,
        opening_grace_active: bool = False,
        core_session_active: bool = False,
    ) -> Decimal:
        prepared = self._prepare_stock_scan_batch(
            positions, buying_power, opening_grace_active, core_session_active
        )
        if prepared is None:
            return buying_power
        (
            batch,
            quote_by_symbol,
            batch_moment,
            open_count,
            guard_active,
            regime_gate_active,
            idle_relaxation_multiplier,
            idle_relaxation_amount,
            profit_target_multiplier,
            stop_tighten_multiplier,
            fresh_entry_blackout_active,
            effective_core_session_active,
            bucket_remaining,
            bucket_slot_limits,
            bucket_position_counts,
            fractional_remaining,
            whole_share_remaining,
            max_fractional_positions,
            fractional_position_count,
            volatility_scalp_effective_max_concurrent,
            volatility_scalp_effective_max_averaging,
            volatility_scalp_intensity,
        ) = prepared
        state = _StockScanState(
            buying_power=buying_power,
            open_count=open_count,
            bucket_remaining=bucket_remaining,
            bucket_position_counts=bucket_position_counts,
            fractional_remaining=fractional_remaining,
            whole_share_remaining=whole_share_remaining,
            fractional_position_count=fractional_position_count,
        )
        for symbol in batch:
            if symbol in self.broker_conflict_symbols:
                continue
            self._process_stock_symbol(
                symbol,
                state,
                positions,
                opening_grace_active,
                core_session_active,
                quote_by_symbol,
                batch_moment,
                guard_active,
                regime_gate_active,
                idle_relaxation_multiplier,
                idle_relaxation_amount,
                profit_target_multiplier,
                stop_tighten_multiplier,
                fresh_entry_blackout_active,
                effective_core_session_active,
                bucket_slot_limits,
                max_fractional_positions,
                volatility_scalp_effective_max_concurrent,
                volatility_scalp_effective_max_averaging,
                volatility_scalp_intensity,
            )
        return state.buying_power

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
        prepared = self._prepare_option_scan_batch(positions)
        if prepared is None:
            return buying_power
        open_count, guard_active, directions, batch, quote_by_symbol, today, current_vixy = prepared
        for contract in batch:
            option_symbol = contract["symbol"]
            key = f"OPTION:{option_symbol}"
            if option_symbol in self.broker_conflict_symbols:
                continue
            try:
                quote = quote_by_symbol.get(option_symbol)
                if not quote:
                    # By request ("check now for uber") - live
                    # evidence: a held position's own broker-reported
                    # last_price kept updating in the status snapshot
                    # (a completely separate positions() API call)
                    # while this loop's OWN option_quotes() batch
                    # never surfaced a quote for it, silently skipping
                    # PROFIT/LOSS/averaging-down every single cycle
                    # with zero visibility - same silent-skip failure
                    # mode the held-contract backfill logging above
                    # was built to catch, just one step further down
                    # the pipeline. Logged for a HELD position only
                    # (quantity > 0 in the broker's own position list)
                    # - a flat/never-bought candidate missing a quote
                    # is routine and not worth logging every cycle.
                    if any(
                        item.get("symbol") == option_symbol
                        and Decimal(str(item.get("quantity", "0") or "0")) > 0
                        for item in positions
                        if item.get("instrument_type") == "OPTION"
                    ):
                        log.warning(
                            "OPTIONS | %-8s | held position has no quote "
                            "in this cycle's option_quotes batch - "
                            "exit management skipped",
                            option_symbol,
                        )
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
                    open_count, buying_power = self._evaluate_option_entry(
                        contract,
                        option_symbol,
                        key,
                        quote,
                        price,
                        days_to_expiration,
                        current_iv,
                        directions,
                        guard_active,
                        current_vixy,
                        open_count,
                        buying_power,
                        positions,
                    )
                    continue
                buying_power = self._evaluate_option_exit(
                    contract,
                    option_symbol,
                    key,
                    quote,
                    price,
                    quantity,
                    cost,
                    days_to_expiration,
                    buying_power,
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
