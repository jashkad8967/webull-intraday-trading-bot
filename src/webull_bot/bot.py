import logging
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from zoneinfo import ZoneInfo

from rich.logging import RichHandler

from webull_bot.analyst_data import AnalystDataService
from webull_bot.commands import CommandQueue
from webull_bot.config import settings
from webull_bot.daily_pnl import DailyPnlTracker
from webull_bot.invalid_symbols import InvalidSymbolTracker
from webull_bot.market_agent import MarketResearchAgent
from webull_bot.pairs import (
    PAIRS,
    PAIRS_CAPITAL_FRACTION,
    PAIRS_MAX_CONCURRENT,
    PairsStrategy,
)
from webull_bot.status import StatusWriter
from webull_bot.strategy import OBI_DEPTH_LEVELS, OPTION_VIXY_SYMBOL, TradingStrategy
from webull_bot.trade_events import TradeEventStreamService
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

# Execution/risk constants below are hardcoded rather than config: they're
# fixed institutional-style execution/guardrail behavior, not per-account
# tuning knobs like the .env-driven strategy thresholds.

# Iceberg / scaled order execution (see place_stock_scaled).
ICEBERG_MIN_SHARES = Decimal("50")
ICEBERG_SLICE_SHARES = 10
ICEBERG_SLICE_INTERVAL_SECONDS = 3

# Automated risk guardrails.
PRICE_SANITY_TOLERANCE = Decimal("0.05")
HARD_ORDER_NOTIONAL_CEILING = Decimal("2000")
CONSECUTIVE_ORDER_ERROR_LIMIT = 5
ORDER_ERROR_WINDOW_SECONDS = 60
# Webull's own hard minimum account equity for short selling
# (OAUTH_OPENAPI_NEW_NO_POSITION_MARGIN_ACCOUNT_CAN_NOT_SELL_SHORT_FOR_LT_2K)
# - see AutoTrader.account_state's proactive check, which disables short
# entries the moment equity is seen below this, instead of spending a
# live order attempt (and its own share of the "order" rate budget) on
# a short that's certain to be rejected every single time.
SHORT_SELLING_MIN_EQUITY = Decimal("2000")

# Hold non-intraday stock positions overnight instead of always flattening
# at EOD_CLOSE_TIME. Buckets in ALWAYS_FLATTEN_BUCKETS stay same-day-only
# regardless of this flag, since pairs is an intraday-only strategy by
# design.
OVERNIGHT_HOLD_ENABLED = True
ALWAYS_FLATTEN_BUCKETS = frozenset({"PAIRS_LONG", "PAIRS_SHORT"})

# Deterministic market context (Webull's own gainers/losers/most-active
# screeners - no LLM involved) refreshed on a slow, fixed cadence
# independent of the ~4x/second poll loop. Feeds the research agent's
# STATE as fixed-size context instead of asking it to discover movers via
# open-ended web search, and feeds agent_popular_symbols directly so that
# signal keeps working even if the agent is disabled or a request fails.
MARKET_PULSE_REFRESH_SECONDS = 120


def _working_orders_lock(bot) -> object:
    """getattr fallback (module-level, not a self-method - see below)
    so working_orders touches can be locked without breaking the many
    existing unit tests that bind an AutoTrader method directly onto a
    bare SimpleNamespace fixture (AutoTrader.foo.__get__(fake_bot)).
    A self-method here would itself need to be looked up as
    self._working_orders_lock, which fails on a fixture that never
    bound it - a plain module-level function taking bot as an argument
    has no such requirement. Falls back to a no-op context manager
    when bot has no working_orders_lock attribute at all (every
    existing test fixture), so those tests run unchanged, single-
    threaded, with no behavior change - only the real AutoTrader
    (which sets a real threading.Lock in __init__) actually
    serializes against the position-protection thread (see
    AutoTrader._position_protection_loop).
    """
    lock = getattr(bot, "working_orders_lock", None)
    return lock if lock is not None else nullcontext()


def _rekey_working_order(bot, old_order_id: str, new_order_id: str, entry: dict) -> None:
    """Swaps a cancel-and-replace repricer's working_orders entry
    atomically under the lock - every repricer (reprice_resting_
    exits/entries, reprice_volatility_scalp_exits/entries) does this
    exact pop-old/set-new pair, and each one needs it locked now that
    the position-protection thread runs concurrently with record_trade
    on the main thread. Module-level for the same test-fixture-
    compatibility reason as _working_orders_lock above.
    """
    with _working_orders_lock(bot):
        bot.working_orders.pop(old_order_id, None)
        bot.working_orders[new_order_id] = entry


def _is_rate_limited(exc: Exception) -> bool:
    """True for Webull's 429 TOO_MANY_REQUESTS rejection - live evidence
    this session: CLOSE (fractional pre-close sweep) and RECON (order
    history reconciliation) both hit it right after a restart's initial
    burst of setup calls.
    """
    text = str(exc).upper()
    return "429" in text or "TOO_MANY_REQUESTS" in text


def _retry_once_on_rate_limit(fn, *args, delay: float = 0.3, **kwargs):
    """By request: "if there is any 429, make sure to refire that order
    asap" - a single quick retry (not an unbounded loop, which would
    itself contribute to the rate limit it's trying to recover from)
    after a brief pause, specifically for the order-placement/
    cancellation calls in the position-protection loop where a missed
    action costs real money/opportunity (unlike a quote/position
    lookup, which already fails soft and just retries next cycle
    regardless). Re-raises whatever the second attempt raises (a non-
    429 exception immediately, or the 429 again after the one retry) -
    callers keep their own existing try/except handling unchanged.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        if not _is_rate_limited(exc):
            raise
        time.sleep(delay)
        return fn(*args, **kwargs)


# By request: "do not wait for the response to fire another request" -
# scoped to the position-protection loop only (not the universe scan,
# which already hit live 429 TOO_MANY_REQUESTS rate-limit errors - see
# the CLOSE/RECON incidents this same session). Each repricer's per-
# candidate cancel+place (and stock_position lookup) previously ran
# ONE order at a time, each waiting out a full network round-trip
# before the next candidate's requests even started - with N stale
# orders needing action in the same cycle, that's N sequential round-
# trips instead of ~1. Bounded worker count (not unbounded) so a cycle
# with many candidates still can't multiply the account's real request
# rate past what a human clicking through the same N actions by hand
# would generate.
_POSITION_PROTECTION_MAX_WORKERS = 4


def _dispatch_concurrently(items: list, worker) -> None:
    """Runs worker(item) for every item without waiting for one to
    finish before starting the next (bounded by
    _POSITION_PROTECTION_MAX_WORKERS) - worker is expected to handle
    its own exceptions internally (every caller's per-candidate body
    already does, via its own try/except), same as the sequential
    for-loop this replaces. A single item's exception here would
    otherwise only surface (and stop the whole batch) when its future
    is collected - re-raising defeats "one bad candidate shouldn't
    block the rest," so any exception a worker doesn't catch itself is
    logged and swallowed here instead.
    """
    if not items:
        return
    with ThreadPoolExecutor(
        max_workers=min(_POSITION_PROTECTION_MAX_WORKERS, len(items))
    ) as pool:
        futures = [pool.submit(worker, item) for item in items]
        for future in futures:
            try:
                future.result()
            except Exception as exc:  # pragma: no cover - workers self-handle
                log.error("PROTECT| concurrent dispatch worker failed | %s", exc)


class AutoTrader:
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
        self.last_trade: dict[str, float] = {}
        self.last_exit_at: dict[str, float] = {}
        # See post_stop_reentry_ready - keyed by bare symbol (not the
        # "STOCK:SYMBOL" key), stamped only on a STOP-type record_trade.
        self.last_volatility_stop_loss_at: dict[str, float] = {}
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
        self.last_entry_reprice = 0.0
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
        self.cached_raw_buying_power = Decimal("0")
        self.cached_positions: list[dict] = []
        # Read by _position_protection_loop's background thread - see
        # its docstring and run()'s "self.cached_core_session_active =
        # core_session_active" assignment.
        self.cached_core_session_active = False
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

    def now(self) -> datetime:
        return datetime.now(self.timezone)

    def is_trading_day(self, moment: datetime) -> bool:
        return (
            moment.weekday() < 5
            and moment.date().isoformat() not in self.config.holidays()
        )

    def session_moment(self, moment: datetime, value: str) -> datetime:
        return datetime.combine(
            moment.date(),
            self.config.session_time(value),
            tzinfo=self.timezone,
        )

    def filter_by_historical_volatility(self, symbols: list[str]) -> list[str]:
        if (
            not self.config.historical_volatility_filter_enabled
            or not symbols
        ):
            return symbols
        floor = float(self.config.min_historical_volatility_percent)
        log.info(
            "VOLFILT | scoring %s symbols | lookback=%sd | floor=%.2f%%",
            len(symbols),
            self.config.historical_volatility_days,
            floor,
        )
        try:
            scores = self.api.historical_volatility(
                symbols,
                self.config.historical_volatility_days,
            )
        except Exception as exc:
            log.warning("VOLFILT | disabled this cycle | %s", exc)
            return symbols
        covered = [symbol for symbol in symbols if symbol in scores]
        if len(covered) < max(1, len(symbols) // 2):
            log.warning(
                "VOLFILT | insufficient coverage (%s/%s) | keeping full universe",
                len(covered),
                len(symbols),
            )
            return symbols
        qualifying = [symbol for symbol in covered if scores[symbol] >= floor]
        if not qualifying:
            log.warning(
                "VOLFILT | no symbols cleared floor | keeping full universe"
            )
            return symbols
        ordered = sorted(
            qualifying,
            key=lambda symbol: scores[symbol],
            reverse=True,
        )
        log.info(
            "VOLFILT | kept %s of %s | top=%s",
            len(ordered),
            len(symbols),
            ",".join(
                f"{symbol}:{scores[symbol]:.1f}%" for symbol in ordered[:5]
            ),
        )
        return ordered

    def refresh_sma_trend(self, symbols: list[str]) -> None:
        """Once-daily higher-timeframe trend reference (see
        TradingStrategy.sma_trend_supports_entry) - a real daily-bar SMA,
        not something derivable from the bot's own few-second tick polls.
        Merges into the existing cache rather than replacing it outright,
        so a partial/failed refresh degrades to yesterday's (still roughly
        valid) SMA instead of going empty and disabling the filter.
        """
        if not self.config.sma_trend_filter_enabled or not symbols:
            return
        try:
            sma = self.api.sma_trend(symbols, self.config.sma_trend_days)
        except Exception as exc:
            log.warning("SMA    | trend refresh failed this cycle | %s", exc)
            return
        if not sma:
            log.warning("SMA    | no coverage this cycle | keeping prior values")
            return
        self.strategy.sma_trend.update(
            {symbol: Decimal(str(value)) for symbol, value in sma.items()}
        )
        log.info(
            "SMA    | trend reference refreshed | %s/%s symbols | lookback=%sd",
            len(sma),
            len(symbols),
            self.config.sma_trend_days,
        )

    def filter_with_popular_reinstated(self, candidates: list[str]) -> list[str]:
        """Volatility-filter candidates, then re-add any configured popular
        symbol the filter cut - so well-known names aren't silently dropped
        just because their historical amplitude sits under the floor.
        """
        filtered = self.filter_by_historical_volatility(candidates)
        available = set(candidates)
        kept = set(filtered)
        reinstated = [
            symbol
            for symbol in self.config.popular_stocks()
            if symbol in available and symbol not in kept
        ]
        if reinstated:
            log.info(
                "LOAD   | reinstated %s popular symbols the volatility filter "
                "would have dropped | %s",
                len(reinstated),
                ",".join(reinstated),
            )
            filtered = list(dict.fromkeys(reinstated + filtered))
        return filtered

    @staticmethod
    def exclude_pairs_symbols(symbols: list[str]) -> tuple[list[str], list[str]]:
        """A pairs leg can be short (a negative broker-reported quantity),
        and stock_decision treats any non-positive quantity as "flat,
        eligible to BUY" - left in the main scan, the EMA/OBI strategy would
        try to buy into a position trade_pairs is deliberately holding
        short.
        """
        pairs_symbols = {symbol for pair in PAIRS for symbol in pair}
        if not pairs_symbols:
            return symbols, []
        excluded = [symbol for symbol in symbols if symbol in pairs_symbols]
        if not excluded:
            return symbols, []
        remaining = [symbol for symbol in symbols if symbol not in pairs_symbols]
        return remaining, excluded

    def safe_top_gainers(self, limit: int, page_size: int) -> dict[str, dict]:
        """top_gainers() hits a live Webull endpoint during the once-daily
        universe rebuild; a screener hiccup here must never be allowed to
        crash the whole trading loop, so failures are logged and treated as
        "no gainers this cycle" instead of propagating.
        """
        try:
            return self.api.top_gainers(limit, page_size)
        except Exception as exc:
            log.warning("LOAD   | top-gainers screener failed this cycle | %s", exc)
            return {}

    def safe_premarket_gainers(self, limit: int, page_size: int) -> dict[str, dict]:
        """Same screener as safe_top_gainers, but Webull's own
        rank_type="PRE_MARKET" (today's biggest movers in the
        pre-market session specifically) instead of the default
        DAY_1/regular-session ranking safe_top_gainers already feeds
        into the daily universe rebuild - see refresh_premarket_
        gainers. A screener hiccup here must never crash the trading
        loop either.
        """
        try:
            return self.api.top_gainers(limit, page_size, rank_type="PRE_MARKET")
        except Exception as exc:
            log.warning(
                "LOAD   | pre-market gainers screener failed | %s", exc
            )
            return {}

    def safe_top_losers(self, limit: int, page_size: int) -> dict[str, dict]:
        try:
            return self.api.top_losers(limit, page_size)
        except Exception as exc:
            log.warning("LOAD   | top-losers screener failed this cycle | %s", exc)
            return {}

    def safe_market_pulse_active(self, limit: int, page_size: int) -> dict[str, dict]:
        """Distinct from safe_top_active_stocks: that method's failure
        fallback is the prior day's whole trading universe (right for a
        once-daily universe rebuild), which would blow up market_pulse's
        small-fixed-size guarantee. This falls back to empty instead.
        """
        try:
            return self.api.top_active_stocks(limit, page_size)
        except Exception as exc:
            log.warning("LOAD   | most-active screener failed this cycle | %s", exc)
            return {}

    @staticmethod
    def _market_pulse_entries(data: dict[str, dict]) -> list[dict]:
        return [
            {
                "symbol": symbol,
                "chg": AutoTrader._compact_number(
                    item.get("change_ratio", 0) * 100, 2
                ),
                "vol": AutoTrader._compact_number(item.get("volume", 0)),
            }
            for symbol, item in data.items()
        ]

    def refresh_market_pulse(self) -> None:
        """Small, fixed-size, fully deterministic market context (Webull's
        own gainers/losers/most-active screeners) refreshed on a slow,
        fixed cadence independent of the poll loop - this replaces asking
        the research agent to discover movers via open-ended web search,
        which was the actual source of unpredictable request size (and the
        occasional Groq 413). Each of the three lists is capped at
        AGENT_MARKET_PULSE_SYMBOLS, so the payload this feeds downstream
        never grows with market conditions. Uses its own small-list
        fallback (empty, not the prior universe) on a screener failure -
        this is market color, not the trading universe.
        """
        now = time.monotonic()
        if now - self.last_market_pulse_refresh < MARKET_PULSE_REFRESH_SECONDS:
            return
        self.last_market_pulse_refresh = now
        limit = self.config.agent_market_pulse_symbols
        gainers = self.safe_top_gainers(limit, limit)
        losers = self.safe_top_losers(limit, limit)
        most_active = self.safe_market_pulse_active(limit, limit)
        self.market_pulse_cache = {
            "gainers": self._market_pulse_entries(gainers),
            "losers": self._market_pulse_entries(losers),
            "most_active": self._market_pulse_entries(most_active),
        }
        # Feeds a direct priority_score bonus (see
        # TradingStrategy.most_active_priority_bonus) - distinct from
        # agent_popular_symbols below, which just marks a symbol eligible
        # for the POPULAR bucket without weighting most-active names any
        # higher than a gainer/loser inside it.
        self.strategy.most_active_symbols = {
            str(symbol).upper() for symbol in most_active
        }

    def refresh_premarket_gainers(self, moment: datetime) -> None:
        """By request: "get the top gainers before the day starts and
        look to invest in that for quick profit." Distinct from
        safe_top_gainers (Webull's default DAY_1/regular-session
        ranking, already folded anonymously into the once-daily full
        universe download) - this uses Webull's own rank_type=
        "PRE_MARKET" screener, fetched once per day as early as this
        method gets called (well before market_open in practice, since
        run() calls it before its own market_open gate below), so
        today's actual pre-market movers get a head start instead of
        competing for attention with the other several thousand
        symbols in the general scan rotation.

        Feeds seed_popular_symbols (POPULAR-bucket eligible, so these
        can trade in extended hours too - see the "only established/
        popular symbols trade outside core hours" gate in trade_
        stocks) and gets merged into stock_symbols/stock_categories
        directly (a symbol might not already be in the fast initial
        universe load) so trade_stocks' own force_scan mechanism
        (already used for the volatility-scalp cohort) can pick it up
        below.
        """
        if self.premarket_gainers_date == moment.date():
            return
        if self.config.premarket_gainers_limit <= 0:
            self.premarket_gainers_date = moment.date()
            return
        gainers = self.safe_premarket_gainers(
            self.config.premarket_gainers_limit,
            self.config.stock_universe_page_size,
        )
        self.premarket_gainers_date = moment.date()
        if not gainers:
            return
        self.premarket_gainers = {str(symbol).upper() for symbol in gainers}
        self.seed_popular_symbols |= self.premarket_gainers
        new_to_universe = [
            symbol
            for symbol in self.premarket_gainers
            if symbol not in self.stock_categories
        ]
        for symbol in new_to_universe:
            self.stock_categories[symbol] = "US_STOCK"
        if new_to_universe:
            self.stock_symbols = self.stock_symbols + new_to_universe
        log.info(
            "LOAD   | pre-market gainers | %s symbols (%s new to the "
            "universe) | %s",
            len(self.premarket_gainers),
            len(new_to_universe),
            ",".join(sorted(self.premarket_gainers)[:10]),
        )

    def resolve_targets(self, moment: datetime) -> None:
        """Kicks off the once-daily universe/VOLFILT/SMA refresh on a
        background thread and returns immediately - never blocks the
        caller. Live incident: this used to run synchronously inline in
        the main loop, meaning EVERY protective mechanism (stop-loss
        checks, order-fill monitoring, repricing) was unavailable for
        however long the whole sequence took - under a minute at the
        old 500-symbol universe, but 15-20 minutes at today's 5000-
        symbol universe. Confirmed live: real open positions (down as
        much as -7.6%) sat completely unmonitored through that entire
        window on every restart. See _resolve_targets_work for the
        actual (unchanged) slow work; this wrapper only adds the
        non-blocking dispatch.
        """
        if self.resolved_date == moment.date():
            return
        if self._resolve_targets_in_progress_for == moment.date():
            return
        self._resolve_targets_in_progress_for = moment.date()
        threading.Thread(
            target=self._resolve_targets_work,
            args=(moment,),
            daemon=True,
        ).start()

    def _resolve_targets_work(self, moment: datetime) -> None:
        try:
            self._resolve_targets_work_body(moment)
        except Exception as exc:
            log.error("LOAD   | resolve_targets failed | %s", exc)
        finally:
            # Cleared on both success and failure - a failure retries
            # on the very next cycle instead of being permanently
            # stuck for the rest of the day (resolved_date is only
            # ever set on success, at the end of the body below).
            self._resolve_targets_in_progress_for = None

    def _download_and_filter_universe(
        self, limit: int, pool: int
    ) -> tuple[dict[str, str], list[str], list[str]]:
        """The "STOCK_SYMBOLS=ALL" universe download+filter pipeline,
        extracted as a pure(ish) helper (reads self.invalid_symbols/
        self.config only, never mutates self.stock_symbols/
        self.stock_categories itself) so it can run more than once per
        day at different sizes - see _resolve_targets_work_body (the
        fast initial pass) and _grow_stock_universe (the background
        continuation toward the full universe). Returns (categories,
        stock_symbols, reserve_symbols).
        """
        log.info(
            "LOAD   | downloading stocks and ETFs | limit=%s | pool=%s",
            limit,
            pool,
        )
        categories = self.api.stock_universe(
            lambda category, count, category_limit: log.info(
                "LOAD   | %-8s | %s/%s",
                category,
                count,
                category_limit or "ALL",
            ),
            limit=pool,
        )
        preferred = self.config.popular_stocks()
        preferred_categories = self.api.stock_categories(preferred)
        added = 0
        for symbol in preferred:
            if symbol not in categories and symbol in preferred_categories:
                categories[symbol] = preferred_categories[symbol]
                added += 1
        if added:
            log.info(
                "LOAD   | added %s popular symbols outside directory cap",
                added,
            )
        if self.config.top_gainers_limit > 0:
            gainers = self.safe_top_gainers(
                self.config.top_gainers_limit,
                self.config.stock_universe_page_size,
            )
            gainers_added = 0
            for symbol in gainers:
                if symbol not in categories:
                    categories[symbol] = "US_STOCK"
                    gainers_added += 1
            if gainers_added:
                log.info(
                    "LOAD   | added %s top-gainer symbols outside directory cap",
                    gainers_added,
                )
        if self.config.exclude_etfs:
            etfs = [
                symbol
                for symbol, category in categories.items()
                if category == "US_ETF"
            ]
            for symbol in etfs:
                categories.pop(symbol, None)
            if etfs:
                log.info("LOAD   | excluded %s ETFs", len(etfs))
        for symbol in self.invalid_symbols.symbols:
            categories.pop(symbol, None)
        eligible = [
            symbol for symbol in categories if symbol not in self.invalid_symbols
        ]
        eligible = self.filter_with_popular_reinstated(eligible)
        return categories, eligible[:limit], eligible[limit:]

    def _grow_stock_universe(self, moment: datetime) -> None:
        """Continues growing today's universe toward the full
        MAX_SYMBOLS in the background, after _resolve_targets_work_body's
        fast initial pass has already unblocked trading. By request,
        after live evidence: at a large MAX_SYMBOLS, downloading and
        VOLFILT-scoring the WHOLE universe before AutoTrader.stock_
        symbols was populated at all took 15-20 minutes - position
        protection never blocked on this (see resolve_targets), but no
        NEW entry could fire the entire time either, since trade_stocks
        had nothing to scan. Only ever called once per day, right after
        the initial pass, from the same background thread - never blocks
        the main loop either, same as resolve_targets itself.

        Re-downloads at a progressively larger limit each step (simpler
        and safer than trying to resume Webull's own pagination cursor
        across separate calls) and MERGES newly-discovered symbols into
        the already-active self.stock_symbols/self.stock_categories -
        never replaces or resets what's already scanning, only adds to
        it. Stops once the configured MAX_SYMBOLS is reached, or the
        universe genuinely has no more symbols to add.
        """
        full_limit = self.config.stock_universe_limit()
        initial_limit = min(full_limit, self.config.stock_universe_initial_limit)
        if full_limit <= initial_limit:
            return
        pool = self.config.stock_universe_pool()
        current_limit = initial_limit
        batch = self.config.stock_universe_growth_batch_size
        interval = self.config.stock_universe_growth_interval_seconds
        while current_limit < full_limit and self.resolved_date == moment.date():
            time.sleep(interval)
            current_limit = min(full_limit, current_limit + batch)
            try:
                categories, symbols, reserve = self._download_and_filter_universe(
                    current_limit, pool
                )
            except Exception as exc:
                log.error("LOAD   | universe growth step failed | %s", exc)
                continue
            if self.resolved_date != moment.date():
                # A new trading day started (or a fresh resolve_targets
                # kicked off) while this growth step was in flight -
                # abandon it rather than merge stale-day data into a
                # new day's universe.
                return
            existing = set(self.stock_symbols)
            new_symbols = [s for s in symbols if s not in existing]
            if new_symbols:
                self.stock_categories.update(categories)
                self.stock_symbols = self.stock_symbols + new_symbols
                self.reserve_symbols = reserve
                log.info(
                    "LOAD   | universe grown | +%s symbols | total=%s/%s",
                    len(new_symbols),
                    len(self.stock_symbols),
                    full_limit,
                )
            if current_limit >= full_limit or len(symbols) < current_limit:
                # Reached the configured cap, or the real universe is
                # simply smaller than the cap - nothing more to grow.
                return

    def _resolve_targets_work_body(self, moment: datetime) -> None:
        requested_stocks = self.config.stocks()
        if requested_stocks == ["ALL"]:
            full_limit = self.config.stock_universe_limit()
            pool = self.config.stock_universe_pool()
            # By request: start with a small, fast initial universe so
            # trading can begin almost immediately, then grow toward
            # the full MAX_SYMBOLS in the background (see
            # _grow_stock_universe, kicked off at the end of this
            # function) instead of blocking every new entry on
            # downloading and VOLFILT-scoring the whole universe first.
            limit = min(full_limit, self.config.stock_universe_initial_limit)
            initial_pool = min(pool, max(limit, self.config.stock_universe_page_size))
            self.stock_categories, self.stock_symbols, self.reserve_symbols = (
                self._download_and_filter_universe(limit, initial_pool)
            )
        else:
            log.info("LOAD   | resolving %s configured symbols", len(requested_stocks))
            requested_stocks = [
                symbol
                for symbol in requested_stocks
                if symbol not in self.invalid_symbols
            ]
            self.stock_symbols = (
                requested_stocks
                if self.config.max_symbols == 0
                else requested_stocks[: self.config.max_symbols]
            )
            self.reserve_symbols = []
            self.stock_categories = self.api.stock_categories(self.stock_symbols)
            for symbol in self.stock_symbols:
                self.stock_categories.setdefault(symbol, "US_STOCK")
            if self.config.exclude_etfs:
                self.stock_symbols = [
                    symbol
                    for symbol in self.stock_symbols
                    if self.stock_categories.get(symbol) != "US_ETF"
                ]
        self.stock_symbols, pairs_excluded = self.exclude_pairs_symbols(
            self.stock_symbols
        )
        self.reserve_symbols, _ = self.exclude_pairs_symbols(self.reserve_symbols)
        if pairs_excluded:
            log.info(
                "LOAD   | excluded %s pairs-strategy symbols from the main "
                "universe scan (managed separately) | %s",
                len(pairs_excluded),
                ",".join(pairs_excluded),
            )
        missing_watchlist = [
            symbol for symbol in self.user_watchlist if symbol not in self.stock_symbols
        ]
        if missing_watchlist:
            uncategorized = [
                symbol
                for symbol in missing_watchlist
                if symbol not in self.stock_categories
            ]
            if uncategorized:
                # A single batched lookup (stock_categories chunks internally)
                # instead of one throttled call per symbol - this list can be
                # 100+ symbols long (the default watchlist alone), and that
                # throttle is ~3.3s/call, so doing it one at a time would
                # stall startup for minutes.
                try:
                    categories = self.api.stock_categories(uncategorized)
                except Exception as exc:
                    log.error(
                        "LOAD   | watchlist category lookup failed | %s",
                        exc,
                    )
                    categories = {}
                for symbol in uncategorized:
                    self.stock_categories[symbol] = categories.get(symbol, "US_STOCK")
            self.stock_symbols.extend(missing_watchlist)
            log.info(
                "LOAD   | reinstated %s user-watchlist symbols | %s",
                len(missing_watchlist),
                ",".join(missing_watchlist),
            )
        self.refresh_sma_trend(self.stock_symbols)
        self.option_contracts = self.api.resolve_options()
        self.discover_all_options = "ALL" in self.config.option_roots()
        self.strategy.clear_market_state()
        self.volatility_scalp_recently_eligible.clear()
        if (
            self.config.volatility_scalp_enabled
            and self.config.volatility_scalp_bar_seed_enabled
        ):
            # By request: pick the volatility-scalp cohort very early in
            # the day, not whenever organic scanning happens to reach
            # enough symbols - left to the normal per-batch seeding
            # alone, the first cohort selection would only ever see
            # whichever ~100 of 300+ symbols happened to be scanned
            # first (a rotating batch, not the whole universe), biasing
            # it toward scan order instead of genuine volatility. Bar-
            # seeding the WHOLE day's candidate pool once, right here,
            # means the very first select_volatility_scalp_symbols()
            # call (right after this function returns) already has full
            # visibility across every symbol, not a scan-order-biased
            # slice of it.
            log.info(
                "SCALP  | seeding volatility windows for %s symbols ahead "
                "of the day's first cohort selection",
                len(self.stock_symbols),
            )
            self.seed_volatility_windows(self.stock_symbols)
        self.stock_cursor = 0
        self.option_cursor = 0
        self.option_discovery_cursor = 0
        self.option_discovery_attempted.clear()
        self.invalid_stock_symbols.clear()
        self.resolved_date = moment.date()
        available = set(self.stock_symbols)
        self.seed_popular_symbols = set(self.config.popular_stocks()) & available
        self.agent_popular_symbols.clear()
        self.daily_realized_loss = Decimal("0")
        self.daily_realized_pnl = Decimal("0")
        self.daily_pnl.reset()
        self.daily_loss_breaker_triggered = False
        self.submitted_order_ids_today.clear()
        self.reconciliation_flagged_order_ids.clear()
        if self.broker_conflict_symbols:
            log.info(
                "CONFLICT | daily reset | resuming automated action on | %s",
                ",".join(sorted(self.broker_conflict_symbols)),
            )
        self.broker_conflict_symbols.clear()
        log.info(
            "READY  | stocks=%s | popular seeds=%s | options=%s | option scan=%s",
            len(self.stock_symbols),
            len(self.seed_popular_symbols),
            len(self.option_contracts),
            "ON" if self.discover_all_options else "OFF",
        )
        # Trading is already unblocked at this point (stock_symbols is
        # populated, resolved_date is set) - continue growing toward
        # the full universe on this same background thread, still
        # never blocking the main loop. No-ops immediately if
        # STOCK_SYMBOLS isn't "ALL" or the initial pass already covered
        # the full configured size.
        if requested_stocks == ["ALL"]:
            self._grow_stock_universe(moment)

    def discover_option_contracts(self) -> None:
        if (
            not self.options_enabled
            or not self.discover_all_options
            or not self.stock_symbols
        ):
            return
        if (
            time.monotonic() - self.last_option_discovery
            < float(self.config.option_discovery_seconds)
        ):
            return
        self.last_option_discovery = time.monotonic()
        discovered = {item["underlying_symbol"] for item in self.option_contracts}
        attempts = 0
        examined = 0
        while (
            attempts < self.config.option_discovery_per_cycle
            and examined < len(self.stock_symbols)
        ):
            underlying = self.stock_symbols[self.option_discovery_cursor]
            self.option_discovery_cursor = (
                self.option_discovery_cursor + 1
            ) % len(self.stock_symbols)
            examined += 1
            if (
                underlying in self.option_discovery_attempted
                or underlying in discovered
                or underlying not in self.strategy.prices
            ):
                continue
            self.option_discovery_attempted.add(underlying)
            attempts += 1
            try:
                contracts = self.api.select_atm_options(
                    underlying,
                    self.strategy.prices[underlying],
                )
                self.option_contracts.extend(contracts)
                discovered.add(underlying)
                log.info(
                    "OPTIONS | %s | found=%s | progress=%s/%s",
                    underlying,
                    ",".join(contract["symbol"] for contract in contracts),
                    len(self.option_discovery_attempted),
                    len(self.stock_symbols),
                )
            except Exception as exc:
                if len(self.option_discovery_attempted) % 100 == 0:
                    log.info(
                        "OPTIONS | progress=%s/%s | latest=%s | %s",
                        len(self.option_discovery_attempted),
                        len(self.stock_symbols),
                        underlying,
                        exc,
                    )

    def cooldown_ready(self, key: str) -> bool:
        elapsed = time.monotonic() - self.last_trade.get(key, float("-inf"))
        return elapsed >= float(self.config.trade_cooldown_seconds)

    @staticmethod
    def cap_batch_to_snapshot_limit(
        batch: list[str],
        unmanaged_held: list[str],
        limit: int = WebullAPI.STOCK_SNAPSHOT_MAX_SYMBOLS,
    ) -> list[str]:
        """Caps a scan batch at `limit` (defaults to WebullAPI's own
        hard 100-symbol snapshot limit for a single quote-fetch call -
        see trade_stocks, which passes a multiple of that when firing
        several concurrent quote batches this cycle - see
        stock_scan_concurrent_batches), always keeping every currently-
        held position first (a real position losing quote coverage -
        see the "fell out of the scanned universe" GUARD warning just
        above this call site - is the more severe failure mode) and
        only trimming the lower-priority remainder. Without this,
        force-injecting the curated cohort/eligible-symbol set on top
        of an already-full batch could push the combined size past
        what this cycle's quote fetch(es) can cover, losing price data
        for every symbol past the limit, not just the extra ones.
        """
        if len(batch) <= limit:
            return batch
        held_set = set(unmanaged_held)
        prioritized = [symbol for symbol in batch if symbol in held_set]
        rest = [symbol for symbol in batch if symbol not in held_set]
        room = max(0, limit - len(prioritized))
        return prioritized + rest[:room]

    def has_pending_buy_order(self, key: str) -> bool:
        """True while an uncancelled BUY order for this key is still
        resting in self.working_orders - independent of the account's
        own (up to ACCOUNT_REFRESH_SECONDS-stale) position snapshot,
        which still reads "flat" (quantity 0) the entire time a BUY
        order hasn't filled yet. Live incident: without this, the
        volatility-scalp fresh-entry gate's only guard against
        double-buying (self.volatility_scalp_positions) was being wiped
        every single cycle by the quantity == 0 cleanup while a resting
        order was still live, stacking repeated duplicate BUY orders
        for the same symbol (MTNB: 5 orders in ~70s, same price, no
        fill or cancel in between) with no cooldown left to stop it.
        """
        with _working_orders_lock(self):
            orders = list(self.working_orders.values())
        return any(
            order.get("key") == key
            and order.get("action") == "BUY"
            and order.get("cancel_requested_at") is None
            for order in orders
        )

    def volatility_scalp_entry_price(self, quote: dict) -> Decimal | None:
        """Aggressive, cross-the-spread BUY price for the volatility-
        scalp cohort - by request: "a lot of the orders are being
        cancelled... ensure the initial order itself is likely to be
        filled." The general stock_limit_price(quote, "BUY") used
        everywhere else prices passively at the bid/ask midpoint - fine
        for the normal strategy's slower entries, but for a strategy
        whose whole point is fast, repeated round trips, a passive mid
        that the market has to fall back down to before it ever fills
        just sits for the full ORDER_TIMEOUT_SECONDS (120s) and gets
        cancelled without ever entering the position (live incident:
        several BUY orders cancelled "unfilled after 120s" in a row).
        Crosses at the ask instead - a real cost (paying the spread
        instead of resting inside it), but guarantees the order can
        actually fill immediately in virtually all cases, which is the
        whole point of a high-frequency strategy that depends on
        actually being in the position to catch the next move.
        reprice_volatility_scalp_entries still lowers this toward a
        falling market afterward, same as before.
        """
        ask = self.api.quote_ask(quote)
        if ask is None:
            return None
        return ask.quantize(self.api.price_tick_size(ask), rounding=ROUND_UP)

    def reentry_cooldown_ready(self, key: str) -> bool:
        elapsed = time.monotonic() - self.last_exit_at.get(key, float("-inf"))
        return elapsed >= float(self.config.stock_reentry_cooldown_seconds)

    def volatility_scalp_reentry_ready(self, key: str) -> bool:
        """A much shorter reentry cooldown than the normal trend-entry
        path's (STOCK_REENTRY_COOLDOWN_SECONDS, 180s default) - the whole
        point of volatility-scalp is cycling the same volatile symbol's
        capital back in as soon as it dips again, not waiting out a
        cooldown sized for a slower, trend-following re-entry.
        """
        elapsed = time.monotonic() - self.last_exit_at.get(key, float("-inf"))
        return elapsed >= float(self.config.volatility_scalp_reentry_cooldown_seconds)

    def volatility_scalp_position_value_ok(
        self,
        current_quantity,
        additional_quantity,
        price: Decimal,
    ) -> bool:
        """True as long as a cohort symbol's total position value
        (existing + a prospective new buy, fresh entry or averaging-
        down) would stay within VOLATILITY_SCALP_MAX_POSITION_FRACTION
        of total account value. Live incident: GAUZ alone grew to ~66%
        of a small account's total value - averaging is still allowed
        up to the separate VOLATILITY_SCALP_MAX_AVERAGING_BUYS cap, but
        never to the point of concentrating most of the account in one
        name. Fails open (True) if account value isn't known yet - a
        missing/stale account-value read should never itself block
        trading.
        """
        account_value = self.cached_account_value
        if account_value is None or account_value <= 0:
            return True
        projected_value = (
            Decimal(str(current_quantity)) + Decimal(str(additional_quantity))
        ) * price
        cap = account_value * self.config.volatility_scalp_max_position_fraction
        return projected_value <= cap

    def volatility_scalp_total_exposure_ok(
        self,
        positions: list[dict],
        additional_value: Decimal,
    ) -> bool:
        """True as long as the WHOLE cohort's total position value
        (every currently-held volatility-scalp symbol combined, plus a
        prospective new buy) would stay within
        VOLATILITY_SCALP_MAX_TOTAL_EXPOSURE_FRACTION of account value.

        volatility_scalp_position_value_ok only bounds one symbol at a
        time - up to VOLATILITY_SCALP_MAX_CONCURRENT_POSITIONS symbols
        could each independently satisfy that cap while the account as a
        whole is almost entirely concentrated in this cohort during a
        correlated selloff (these are explicitly the most volatile names,
        selected together, so correlation during a broad move is likely
        rather than a tail case). Fails open (True) if account value
        isn't known yet, same as the per-symbol check.
        """
        account_value = self.cached_account_value
        if account_value is None or account_value <= 0:
            return True
        total = additional_value
        for position in positions:
            symbol = str(position.get("symbol", "")).upper()
            # self.volatility_scalp_positions (every symbol this
            # strategy has an in-process-tracked open position in), not
            # the narrower curated self.volatility_scalp_symbols cohort
            # list - entries now open for any eligible symbol, not just
            # the curated top handful, so exposure has to be summed
            # against the same broadened set.
            if symbol not in self.volatility_scalp_positions:
                continue
            quantity = Decimal(str(position.get("quantity", "0") or "0"))
            cost_price = Decimal(str(position.get("cost_price") or "0"))
            total += quantity * cost_price
        cap = account_value * self.config.volatility_scalp_max_total_exposure_fraction
        return total <= cap

    def stop_ready_to_submit(self, key: str, symbol: str) -> bool:
        """An escalated stop must resubmit immediately after its cancel, not
        wait out the normal trade cooldown - that cooldown was timed from
        the original (now-cancelled) submission, so honoring it here would
        leave the position with no working stop order for several more
        seconds while price keeps moving against it.
        """
        if symbol in self.pending_stock_exits:
            return False
        return symbol in self.stop_loss_escalated or self.cooldown_ready(key)

    def stop_loss_confirmed(self, symbol: str) -> bool:
        """True once price has sat continuously at/below the stop level for
        STOP_LOSS_CONFIRMATION_SECONDS - see stop_condition_since. An
        escalated stop (already submitted, just resubmitting at a more
        aggressive price after sitting unfilled) skips this: the breach
        was already confirmed once, and escalation is itself a response to
        elapsed time, not a fresh signal that could be a single bad tick.

        Also skips it for the volatility-scalp cohort. Live incident:
        MYND sat well past its stop level (11%+ underwater against a
        ~1.5% max stop) for many minutes without ever stopping out -
        "GATES | stop breach not yet confirmed" kept firing intermittently,
        meaning price ticked back above the stop line often enough that
        the 2s confirmation window never completed. That grace exists to
        filter a single-tick wick; for a symbol whose entire selection
        criterion IS being unusually choppy, the same real, sustained
        loss can cross the stop/un-cross it fast enough to never confirm
        at all, indefinitely deferring real protection on exactly the
        positions most likely to need it fast.
        """
        if (
            not self.config.stop_loss_confirmation_enabled
            or symbol in self.stop_loss_escalated
            or self.strategy.is_volatility_scalp_eligible(symbol)
        ):
            return True
        since = self.stop_condition_since.get(symbol)
        if since is None:
            return False
        return (
            time.monotonic() - since
            >= float(self.config.stop_loss_confirmation_seconds)
        )

    def should_force_market_exit(
        self, symbol: str, exit_is_fractional: bool, core_session_active: bool
    ) -> bool:
        """True once a symbol's exit has failed to fill
        CONSECUTIVE_EXIT_FAILURE_MARKET_THRESHOLD times in a row (see
        consecutive_exit_failures) - the next attempt should use a
        genuine MARKET order instead of another limit, guaranteed to
        fill and end the loop. Same MARKET-order eligibility constraints
        as a manual sell: whole-share, core hours, account supports it.
        """
        return (
            self.consecutive_exit_failures.get(symbol, 0)
            >= self.config.consecutive_exit_failure_market_threshold
            and core_session_active
            and not exit_is_fractional
            and self.fractional_trading_enabled
        )

    @staticmethod
    def is_fractional_trading_not_enabled(exc: Exception) -> bool:
        """True for Webull's OAUTH_OPENAPI_OPENAPI_FRACT_VERSION2_ACCOUNT_
        NOT_TRADE rejection - the account itself hasn't agreed to Webull's
        fractional-trading terms (a one-time click-through at a URL Webull
        includes in the error), so every fractional order will keep failing
        identically until that happens. Retrying changes nothing here.
        """
        return "FRACT_VERSION2_ACCOUNT_NOT_TRADE" in str(exc).upper()

    @staticmethod
    def is_broker_position_conflict(exc: Exception) -> bool:
        """True for Webull's "this order would reverse an existing
        position" rejection - a sign our local view of the position is out
        of sync with the broker's (a stale quantity, a partially-filled
        order, or account state from outside the bot). No amount of
        retrying with the same (wrong) assumption will fix this - it needs
        the account state to actually resolve, so the caller should stop
        hammering the symbol instead of just backing off and trying again.
        """
        return "REVERSE" in str(exc).upper()

    def handle_broker_conflict(self, symbol: str, exc: Exception) -> None:
        self.broker_conflict_symbols.add(symbol)
        self.pending_stock_exits.discard(symbol)
        self.pending_option_exits.discard(symbol)
        self.stop_exit_submitted.pop(symbol, None)
        self.stop_loss_escalated.discard(symbol)
        self.stop_condition_since.pop(symbol, None)
        log.error(
            "CONFLICT | %-8s | broker rejected order as a position reverse "
            "- our view of this position doesn't match the account. Pausing "
            "automated action on it for the rest of the day; check the "
            "Webull app for a stuck order or unexpected position on %s. | %s",
            symbol,
            symbol,
            exc,
        )

    def handle_fractional_trading_not_enabled(self, exc: Exception) -> None:
        if not self.fractional_trading_enabled:
            return
        self.fractional_trading_enabled = False
        log.error(
            "FRACT  | fractional orders rejected - this Webull account "
            "hasn't agreed to fractional trading yet. Falling back to "
            "whole-share sizing for the rest of this run; open the "
            "agreement link below in the Webull app/website once, then "
            "restart the bot to re-enable dollar-sized core-session "
            "entries and the fractional-shares fallback. | %s",
            exc,
        )

    @staticmethod
    def is_fractional_ticker_unsupported(exc: Exception) -> bool:
        """True for Webull's OAUTH_OPENAPI_FRACT_TICKER_DONT_SUPPORT_TRADE
        rejection - unlike FRACT_VERSION2_ACCOUNT_NOT_TRADE (an account-
        wide agreement gate), this is a per-security restriction: some
        tickers just aren't fractional-eligible on Webull regardless of
        account status, and every other symbol is unaffected. Retrying
        the same symbol changes nothing; retrying a different one is fine.
        """
        return "FRACT_TICKER_DONT_SUPPORT_TRADE" in str(exc).upper()

    def handle_fractional_ticker_unsupported(self, symbol: str, exc: Exception) -> None:
        if symbol in self.fractional_unsupported_symbols:
            return
        self.fractional_unsupported_symbols.add(symbol)
        log.warning(
            "FRACT  | %-8s | this security doesn't support fractional "
            "trading - falling back to whole-share sizing for %s for the "
            "rest of this run | %s",
            symbol,
            symbol,
            exc,
        )

    @staticmethod
    def fresh_entry_blackout_active(
        minutes_until_close: float,
        blackout_minutes: float,
        core_session_active: bool,
    ) -> bool:
        """By request, after live evidence (WNW/WKHS stopping out
        shortly after core hours ended): true once fewer than
        blackout_minutes remain in the core session - blocks FRESH
        entries only (not averaging down, not any exit), since a
        brand-new position opened this close to the bell has almost no
        runway to reach its target before conditions change. Always
        False once core_session_active is already False - the existing
        "only established/popular symbols trade outside core hours"
        gate already covers that case, and a negative minutes_until_
        close (core hours already ended) shouldn't itself trigger this
        for a symbol that gate already lets through.
        """
        return (
            core_session_active
            and 0 <= minutes_until_close < blackout_minutes
        )

    @staticmethod
    def is_order_not_cancelable(exc: Exception) -> bool:
        """True for Webull's OPENAPI_ORDER_CAN_NOT_CANCEL rejection - a
        benign race, not a fault: the order is already filling or has
        just filled by the time a repricer/escalator tries to cancel
        it. The working order will resolve itself (fill and drop out
        of open_ids, or genuinely still be cancelable) on the next
        monitor_working_orders poll, so this is a WARNING, same
        "expected, not a fault" convention as QuoteUnavailableError -
        not an ERROR needing investigation.
        """
        return "ORDER_CAN_NOT_CANCEL" in str(exc).upper()

    @staticmethod
    def is_short_selling_unsupported(exc: Exception) -> bool:
        """True for Webull's OAUTH_OPENAPI_NEW_NO_POSITION_MARGIN_ACCOUNT_
        CAN_NOT_SELL_SHORT_FOR_LT_2K rejection - short selling requires at
        least $2,000 in account equity (a standard margin-account
        minimum, not something specific to one security), so every short
        attempt keeps failing identically until equity grows past that
        threshold. Retrying changes nothing here, same reasoning as
        is_fractional_trading_not_enabled.
        """
        return "CAN_NOT_SELL_SHORT_FOR_LT_2K" in str(exc).upper()

    def handle_short_selling_unsupported(self, exc: Exception) -> None:
        if not self.short_selling_supported:
            return
        self.short_selling_supported = False
        log.error(
            "SHORT  | short selling rejected - this account is under "
            "Webull's $2,000 equity minimum for short selling. Disabling "
            "new short entries for the rest of this run; restart the bot "
            "once equity clears that minimum to re-enable them. | %s",
            exc,
        )

    @staticmethod
    def is_symbol_restricted_to_closing_only(exc: Exception) -> bool:
        """True for Webull's OAUTH_OPENAPI_CAN_NOT_CREATE_A_OPEN_ORDER
        rejection ("This symbol is restricted to closing orders only") -
        a per-security, broker-side restriction (e.g. a halt, an
        emergency SSR-style curb, being pulled from tradability) that
        deterministically rejects every single opening order for this
        symbol, exactly the same way, for as long as it's in effect.
        Live incident: RFAI. Retrying changes nothing, same reasoning as
        is_short_selling_unsupported/is_fractional_ticker_unsupported -
        letting it accumulate toward the generic order-error-rate
        blacklist (5 errors in a shared, cross-symbol window) wastes
        several futile attempts and API calls first, and risks that
        window tripping on account of an unrelated symbol instead.
        """
        return "CAN_NOT_CREATE_A_OPEN_ORDER" in str(exc).upper()

    def handle_symbol_restricted_to_closing_only(
        self, symbol: str, exc: Exception
    ) -> None:
        if symbol in self.entry_restricted_symbols:
            return
        self.entry_restricted_symbols.add(symbol)
        log.warning(
            "GUARD  | %-8s | restricted to closing orders only by the "
            "broker - blocking new entries for %s for the rest of the "
            "day (existing positions still exit normally) | %s",
            symbol,
            symbol,
            exc,
        )

    def rate_capped(self, key: str) -> bool:
        limit = self.config.stock_max_trades_per_hour
        if limit <= 0:
            return False
        now = time.monotonic()
        times = self.trade_times[key]
        while times and now - times[0] > 3600.0:
            times.popleft()
        return len(times) >= limit

    def record_trade(
        self,
        key: str,
        order_id: str,
        action: str,
        limit_price: Decimal | None = None,
        pnl: Decimal | None = None,
        entry_price: Decimal | None = None,
        quantity: Decimal | None = None,
        counts_toward_idle_cash_ramp: bool = True,
    ) -> None:
        submitted_at = time.monotonic()
        self.last_trade[key] = submitted_at
        self.submitted_order_ids_today.add(order_id)
        if action in ("PROFIT", "STOP", "MANUAL_SELL"):
            self.last_exit_at[key] = submitted_at
            self.position_opened_at.pop(key, None)
            if pnl is not None:
                # Feeds symbol_quarantined() - every realized exit's P&L,
                # partitioned per-key so one bad symbol can't drag down
                # another's entry eligibility.
                self.symbol_pnl_history[key].append((submitted_at, pnl))
        if action == "STOP":
            # Feeds stop_loss_guard_active() - a real stop-loss fill (not a
            # manual sell or a profit-take), tracked regardless of symbol.
            self.recent_stop_losses.append(submitted_at)
            # Feeds post_stop_reentry_ready() - see its docstring for
            # the DAIC incident this guards against.
            if key.startswith("STOCK:"):
                self.last_volatility_stop_loss_at[key.split(":", 1)[1]] = submitted_at
        if (
            action in ("BUY", "SHORT", "MANUAL_BUY")
            and counts_toward_idle_cash_ramp
        ):
            # Resets the idle-cash gate-relaxation ramp (see
            # idle_cash_ramp_progress) - capital just got deployed, so
            # quality gates snap back to their normal strictness until
            # cash sits idle above MIN_CASH_RESERVE_DOLLARS again.
            #
            # Live incident (this bug, caught while investigating "not
            # investing all the capital"): the ramp is only ever read
            # by the GENERAL strategy's own entry gates (see the
            # idle_relaxation_multiplier/amount passed to stock_
            # decision in trade_stocks) - it has no effect on
            # volatility-scalp's own, separate entry conditions. But a
            # volatility-scalp BUY/average-down was resetting this same
            # clock anyway, since both paths call record_trade with the
            # same "BUY" action. With scalp trading firing every few
            # minutes, the general strategy's idle-cash grace/ramp
            # timer effectively never advanced, keeping ITS gates
            # (spread, VWAP, SMA) at full strictness indefinitely even
            # while real buying power sat unused for hours - scalp
            # capital being deployed doesn't mean the general
            # strategy's own capital pool isn't idle. Callers opt out
            # via counts_toward_idle_cash_ramp=False (the volatility-
            # scalp entry/averaging-down call sites) so only a genuine
            # general-strategy deployment resets this clock.
            self.last_capital_deployed_at = submitted_at
        if action in ("BUY", "SHORT"):
            # Feeds TradingStrategy.adaptive_stop_percent's time-aware
            # widen window - see position_opened_at.
            self.position_opened_at[key] = submitted_at
            # A fresh position starts with a clean exit-failure count -
            # see consecutive_exit_failures.
            if key.startswith("STOCK:"):
                self.consecutive_exit_failures.pop(key.split(":", 1)[1], None)
        self.trade_times[key].append(submitted_at)
        # Position-protection now runs on its own background thread
        # (see _position_protection_loop) - lock the dict write itself
        # since that thread's repricers/escalator also create/replace
        # working_orders entries concurrently.
        with _working_orders_lock(self):
            self.working_orders[order_id] = {
                "submitted_at": submitted_at,
                "key": key,
                "action": action,
                "cancel_requested_at": None,
                "limit_price": limit_price,
                "pnl": pnl,
                # Needed to resubmit a like-for-like replacement order when
                # actively repricing a resting entry - see
                # reprice_volatility_scalp_entries.
                "quantity": quantity,
            }
        instrument_type, symbol = key.split(":", 1)
        self.status.record_trade(
            instrument_type,
            symbol,
            action,
            limit_price,
            order_id,
            pnl,
            entry_price=entry_price,
            quantity=quantity,
        )
        limit_text = (
            f" | limit={limit_price}"
            if limit_price is not None
            else ""
        )
        log.info(
            "ORDER  | %-11s | %-6s | %-8s%s | id=%s",
            instrument_type,
            action,
            symbol,
            limit_text,
            order_id,
        )

    def _release_pending_order(self, order: dict) -> None:
        key = str(order.get("key") or "")
        action = str(order.get("action") or "")
        if action not in {"PROFIT", "STOP"} or ":" not in key:
            return
        instrument_type, symbol = key.split(":", 1)
        if instrument_type == "STOCK":
            self.pending_stock_exits.discard(symbol)
        elif instrument_type == "OPTION":
            self.pending_option_exits.discard(symbol)

    def _note_exit_failure(self, key: str) -> None:
        """Tracks a confirmed never-filled exit attempt - see
        consecutive_exit_failures and CONSECUTIVE_EXIT_FAILURE_MARKET_
        THRESHOLD. Stock-only: this class of endless-retry loop was seen
        for stocks specifically, and options' own defined-risk sizing
        already bounds the exposure a stuck options exit represents
        differently enough that folding it into the same counter isn't
        clearly right without its own evidence.
        """
        if not key.startswith("STOCK:"):
            return
        symbol = key.split(":", 1)[1]
        if symbol:
            self.consecutive_exit_failures[symbol] += 1

    def _reverse_if_never_filled(
        self, order_id: str, order: dict, pnl: Decimal
    ) -> None:
        """An exit order that dropped out of the open-orders list is
        usually a fill, but it can also be a cancel/reject the broker
        processed on its own (e.g. a fat-finger price, a stale quote) -
        record_realized_exit already counted its pnl as if it filled, at
        submission time. Confirm via order_detail before trusting that;
        only reverse on an explicit CANCELLED/FAILED status, and fail
        open (assume filled, leave the pnl as-is) on any fetch error or
        an unrecognized/missing status field, since the field name isn't
        confirmed against a live payload yet - a false reversal would be
        worse than an occasional unconfirmed phantom.
        """
        try:
            detail = self.api.order_detail(order_id)
            status = self.api.order_status(detail)
        except Exception as exc:
            log.error(
                "ORDER  | could not confirm fill status | id=%s | %s",
                order_id,
                exc,
            )
            return
        if status in ("CANCELLED", "FAILED"):
            self.reverse_phantom_exit(pnl, order_id)
            self._note_exit_failure(order.get("key", ""))
            log.warning(
                "ORDER  | %s | never filled (%s) - reversing $%s phantom "
                "realized pnl | id=%s",
                order.get("key", ""),
                status,
                pnl,
                order_id,
            )

    def monitor_working_orders(self) -> None:
        now = time.monotonic()
        if (
            now - self.last_order_monitor
            < float(self.config.order_monitor_seconds)
        ):
            return
        self.last_order_monitor = now
        groups = self.api.open_orders()
        open_ids = set(self.api.open_order_ids(groups))

        with _working_orders_lock(self):
            known_order_ids = set(self.working_orders)

        for order_id in open_ids:
            if order_id in self.submitted_order_ids_today:
                # A bot-submitted order, just not currently in
                # working_orders - live incident: the fast volatility-
                # scalp entry/exit repricers cancel-and-replace roughly
                # every second, and there's a real window right after
                # cancel() where the OLD order_id can still show up in
                # open_orders() (broker-side latency) even though
                # working_orders already dropped it in favor of the new
                # replacement order_id. Without this check, that window
                # got misread as "the bot's own order is unrecognized -
                # must be manual," mislabeling normal repricing as a
                # manual action. submitted_order_ids_today (already
                # maintained for reconcile_order_history) never shrinks
                # intraday, so it reliably distinguishes "ours, just
                # untracked right now" from "genuinely never ours."
                continue
            if order_id not in known_order_ids:
                # An order the bot never submitted itself and doesn't
                # already know about - almost always a manual action
                # taken directly in the Webull app (a dashboard-driven
                # manual buy/sell already calls record_trade immediately,
                # so it would already be in working_orders by the time
                # this runs). By request: don't just log an opaque
                # order_id - fetch the real symbol/side/quantity and run
                # it through the same record_trade tracking a bot-driven
                # trade gets, so it actually factors into last_trade/
                # last_exit_at, symbol_pnl_history, the idle-cash ramp,
                # and the dashboard's trade log, instead of sitting
                # invisible to all of that.
                symbol = ""
                side = ""
                quantity = None
                try:
                    detail = self.api.order_detail(order_id)
                    orders = detail.get("orders") or []
                    first = (
                        orders[0]
                        if orders and isinstance(orders[0], dict)
                        else detail
                    )
                    symbol = str(first.get("symbol") or "").upper()
                    side = str(first.get("side") or "").upper()
                    raw_quantity = first.get("total_quantity")
                    if raw_quantity not in (None, ""):
                        quantity = Decimal(str(raw_quantity))
                except Exception as exc:
                    log.warning(
                        "ORDER  | could not fetch detail for an "
                        "unrecognized order | id=%s | %s",
                        order_id,
                        exc,
                    )
                if symbol:
                    action = "MANUAL_SELL" if side in ("SELL", "COVER") else "MANUAL_BUY"
                    self.record_trade(
                        f"STOCK:{symbol}", order_id, action, quantity=quantity
                    )
                    log.info(
                        "ORDER  | monitoring manual order | %-8s | side=%s "
                        "| id=%s",
                        symbol,
                        side or "?",
                        order_id,
                    )
                else:
                    with _working_orders_lock(self):
                        self.working_orders[order_id] = {
                            "submitted_at": now,
                            "key": "",
                            "action": "UNKNOWN",
                            "cancel_requested_at": None,
                        }
                    log.info(
                        "ORDER  | monitoring broker order | id=%s",
                        order_id,
                    )

        with _working_orders_lock(self):
            snapshot = list(self.working_orders.items())

        for order_id, order in snapshot:
            if order_id not in open_ids:
                self._release_pending_order(order)
                with _working_orders_lock(self):
                    self.working_orders.pop(order_id, None)
                self.last_account_refresh = 0.0
                pnl = order.get("pnl")
                if pnl:
                    self._reverse_if_never_filled(order_id, order, pnl)
                continue

            age = now - float(order["submitted_at"])
            if age < float(self.config.order_timeout_seconds):
                continue
            last_cancel = order.get("cancel_requested_at")
            if last_cancel is not None and now - float(last_cancel) < 30:
                continue
            try:
                _retry_once_on_rate_limit(self.api.cancel, order_id)
                with _working_orders_lock(self):
                    live_order = self.working_orders.get(order_id)
                    if live_order is not None:
                        live_order["cancel_requested_at"] = now
                log.warning(
                    "CANCEL | unfilled after %ss | id=%s",
                    self.config.order_timeout_seconds,
                    order_id,
                )
            except Exception as exc:
                log.error("CANCEL | id=%s | %s", order_id, exc)

    def _batched_quotes(self, symbols: list[str]) -> dict[str, dict]:
        """One (or a few, split by category) batched snapshot call for
        multiple symbols, instead of a caller looping and calling
        self.api.stock_quote(symbol) once per symbol. Each individual
        call is a full separate network round-trip - with several
        repricers/sweeps each doing this once per open position or
        working order, every single main-loop cycle, this was a real
        contributor to cycles taking far longer than poll_seconds (live
        evidence: ~40s between scan cycles despite a 0.25s poll target).

        Grouped by category (stock_quotes requires one category per
        call) and capped at WebullAPI.STOCK_SNAPSHOT_MAX_SYMBOLS per
        group. A group's failure only drops that group's symbols from
        the result - callers already treat a missing symbol as "no
        quote yet, try again next cycle," the same as any other quote
        failure.
        """
        unique_symbols = list(dict.fromkeys(symbols))
        if not unique_symbols:
            return {}
        by_category: dict[str, list[str]] = defaultdict(list)
        for symbol in unique_symbols:
            by_category[self.stock_categories.get(symbol, "US_STOCK")].append(symbol)
        quotes: dict[str, dict] = {}
        for category, group in by_category.items():
            for start in range(0, len(group), WebullAPI.STOCK_SNAPSHOT_MAX_SYMBOLS):
                chunk = group[start : start + WebullAPI.STOCK_SNAPSHOT_MAX_SYMBOLS]
                try:
                    fetched, _invalid = self.api.stock_quotes_resilient(
                        chunk, category
                    )
                except Exception as exc:
                    log.warning(
                        "REPRICE| batched quote fetch failed | %s | %s",
                        ",".join(chunk),
                        exc,
                    )
                    continue
                for quote in fetched:
                    symbol = str(quote.get("symbol", "")).upper()
                    if symbol:
                        quotes[symbol] = quote
        return quotes

    def reprice_resting_exits(
        self, positions: list[dict], core_session_active: bool = False
    ) -> None:
        """Continuously re-quote a resting stock PROFIT sell order to track
        the current ask - the top of the spread - for as long as it stays
        unfilled and unescalated ("keep modifying to stay in the spread
        until sold"). Once a symbol is escalated, this stops chasing the ask
        for it and leaves resubmission to the normal escalation path. Also
        skips any symbol currently volatility-scalp eligible - see
        reprice_volatility_scalp_exits, which handles those on a much
        faster, dedicated cadence instead.

        PROFIT only, deliberately - not STOP. A stop-loss needs to fill
        fast to cap a loss; chasing the ask means chasing a price *above*
        the market, and if the stock is actively falling, repeatedly
        cancelling and re-resting a stop above a falling ask can leave it
        unfilled for the whole 15s until escalation, during which the loss
        keeps growing. Only escalate_stalled_stop_losses should ever move a
        stop's price, and only towards a guaranteed-fill crossing price.

        This cancels and replaces the working order *directly* - it does
        not touch pending_stock_exits or stop_exit_submitted, and does not
        call record_trade/record_realized_exit again. Those already ran
        once at the original PROFIT submission; calling them again here on
        every re-quote would record the same realized P&L multiple times
        for one logical exit.
        """
        now = time.monotonic()
        if now - self.last_reprice < float(self.config.order_monitor_seconds):
            return
        self.last_reprice = now
        candidates: list[tuple[str, str, dict]] = []
        with _working_orders_lock(self):
            snapshot = list(self.working_orders.items())
        for order_id, order in snapshot:
            action = order.get("action")
            key = str(order.get("key") or "")
            if action != "PROFIT" or not key.startswith("STOCK:"):
                continue
            if order.get("cancel_requested_at") is not None:
                continue
            symbol = key.split(":", 1)[1]
            if symbol in self.stop_loss_escalated:
                continue
            if (
                self.strategy.is_volatility_scalp_eligible(symbol)
                or symbol in self.volatility_scalp_positions
            ):
                # Handled by the faster reprice_volatility_scalp_exits
                # cadence instead - condensed onto eligibility alone
                # (not the narrower curated self.volatility_scalp_symbols
                # cohort list), so this skip applies to ANY symbol
                # currently volatile enough to qualify, matching the
                # entry side below. Also keeps deferring for an already-
                # adopted cohort position even once it's no longer
                # live-eligible this cycle, so the two repricers never
                # both try to manage the same resting order at once.
                continue
            candidates.append((order_id, symbol, order))
        if not candidates:
            return
        # One batched snapshot call for every symbol this cycle needs,
        # instead of one self.api.stock_quote(symbol) round-trip per
        # candidate - see _batched_quotes.
        quotes = self._batched_quotes([symbol for _, symbol, _ in candidates])

        def _reprice_one(candidate: tuple[str, str, dict]) -> None:
            order_id, symbol, order = candidate
            key = str(order.get("key") or "")
            action = order.get("action")
            try:
                quote = quotes.get(symbol)
                if quote is None:
                    return
                ask = self.api.quote_ask(quote)
                if ask is None or ask == order.get("limit_price"):
                    return
                quantity, cost = self.api.stock_position(symbol, positions)
                if quantity <= 0:
                    return
                # Same fractional/core-hours constraint as trade_stocks'
                # PROFIT exit: cancel-and-replace can't succeed on a
                # fractional quantity outside core hours either, so leave
                # the existing resting order alone rather than cancelling
                # it for a replacement that will just get rejected.
                if self.is_fractional_quantity(quantity) and not core_session_active:
                    return
                if cost > 0 and ask < cost:
                    # Never chase the ask down below entry cost - the
                    # existing resting order was already validly priced at
                    # or above the profit target when submitted; repricing
                    # to a falling ask here could reprice a profit-take
                    # into a loss. Leave it resting and let escalation (or
                    # the ask recovering) handle it instead.
                    return
                _retry_once_on_rate_limit(self.api.cancel, order_id)
                new_order_id = _retry_once_on_rate_limit(
                    self.api.place_stock,
                    symbol,
                    "SELL",
                    quantity,
                    limit_price=ask,
                )
                _rekey_working_order(
                    self,
                    order_id,
                    new_order_id,
                    {
                        "submitted_at": now,
                        "key": key,
                        "action": action,
                        "cancel_requested_at": None,
                        "limit_price": ask,
                        # Carry the pnl already recorded at the original PROFIT
                        # submission forward - this is the same logical exit,
                        # not a new one, so if the repriced order itself never
                        # fills it's still the correct amount to reverse.
                        "pnl": order.get("pnl"),
                    },
                )
                # The dashboard's trade-log entry is still filed under the
                # cancelled order_id - repoint it, or a later reversal
                # (which only ever learns new_order_id) can't find it to
                # discard, leaving a cancelled order's phantom profit
                # visible forever. See StatusWriter.rekey_trade.
                self.status.rekey_trade(order_id, new_order_id)
                log.info(
                    "REPRICE| %-8s | %-6s | ask=%s | id=%s",
                    symbol,
                    action,
                    ask,
                    new_order_id,
                )
            except Exception as exc:
                log.error("REPRICE| %s | %s", symbol, exc)

        # By request: "do not wait for the response to fire another
        # request" - fires every candidate's cancel+place concurrently
        # instead of waiting out each one's full round-trip before the
        # next candidate even starts. See _dispatch_concurrently.
        _dispatch_concurrently(candidates, _reprice_one)

    def reprice_volatility_scalp_exits(
        self, positions: list[dict], core_session_active: bool = False
    ) -> None:
        """The volatility-scalp equivalent of reprice_resting_exits, on
        its own much faster VOLATILITY_SCALP_REPRICE_SECONDS cadence
        (default 1s vs the generic ORDER_MONITOR_SECONDS, default 5s) -
        "actively reprice within the spread to capture the profit, cent
        by cent" only makes sense at a cadence this strategy is actually
        meant to run at. Same cancel-and-replace-toward-the-current-ask
        logic as reprice_resting_exits, just scoped to symbols currently
        volatility-scalp eligible instead of everything else.
        """
        now = time.monotonic()
        if now - self.last_volatility_reprice < float(
            self.config.volatility_scalp_reprice_seconds
        ):
            return
        self.last_volatility_reprice = now
        candidates: list[tuple[str, str, dict]] = []
        with _working_orders_lock(self):
            snapshot = list(self.working_orders.items())
        for order_id, order in snapshot:
            action = order.get("action")
            key = str(order.get("key") or "")
            if action != "PROFIT" or not key.startswith("STOCK:"):
                continue
            if order.get("cancel_requested_at") is not None:
                continue
            symbol = key.split(":", 1)[1]
            if symbol in self.stop_loss_escalated:
                continue
            # Also keeps repricing an already-adopted cohort position
            # even if it's no longer live-eligible this exact cycle -
            # same reasoning as the exit-override/pricing gates in
            # trade_stocks (a position with real capital committed
            # across several averaging buys shouldn't lose active
            # management just because a transient stdev recalculation
            # dipped under the eligibility bar for one cycle).
            if (
                not self.strategy.is_volatility_scalp_eligible(symbol)
                and symbol not in self.volatility_scalp_positions
            ):
                continue
            candidates.append((order_id, symbol, order))
        if not candidates:
            return
        quotes = self._batched_quotes([symbol for _, symbol, _ in candidates])

        def _reprice_one(candidate: tuple[str, str, dict]) -> None:
            order_id, symbol, order = candidate
            key = str(order.get("key") or "")
            action = order.get("action")
            try:
                quote = quotes.get(symbol)
                if quote is None:
                    return
                quantity, cost = self.api.stock_position(symbol, positions)
                if quantity <= 0:
                    return
                exit_is_fractional = self.is_fractional_quantity(quantity)
                if exit_is_fractional and not core_session_active:
                    return
                # Live incident (this bug, caught from a live report):
                # "some sell orders are not repricing down in the
                # spread." The old check here was a blunt "ask < cost ->
                # skip entirely," which correctly avoided ever repricing
                # below cost, but as a side effect froze the resting
                # order completely stuck at a stale price the instant
                # the ask dipped below cost - even when the BID still
                # cleared a genuinely profitable fill. Reuses the same
                # _stall_exit_price logic the initial PROFIT placement
                # and the escalation fix both already use (bid first if
                # it clears cost + min_profit + fee, else a spread-
                # sanity-checked ask fallback) - this can still reprice
                # DOWN toward a lower, still-profitable price, or fill
                # immediately at the bid, instead of freezing.
                if exit_is_fractional:
                    fee_per_share = self.config.sell_fee_dollars
                else:
                    fee_per_share = self.config.sell_fee_dollars / quantity
                min_profit = cost * self.config.volatility_scalp_target_percent
                limit_price = self._stall_exit_price(
                    quote,
                    cost,
                    min_profit,
                    fee_per_share,
                    max_spread_percent=(
                        self.config.volatility_scalp_max_exit_spread_percent
                    ),
                )
                if limit_price is None or limit_price == order.get("limit_price"):
                    return
                _retry_once_on_rate_limit(self.api.cancel, order_id)
                new_order_id = _retry_once_on_rate_limit(
                    self.api.place_stock,
                    symbol,
                    "SELL",
                    quantity,
                    limit_price=limit_price,
                )
                _rekey_working_order(
                    self,
                    order_id,
                    new_order_id,
                    {
                        "submitted_at": now,
                        "key": key,
                        "action": action,
                        "cancel_requested_at": None,
                        "limit_price": limit_price,
                        "pnl": order.get("pnl"),
                    },
                )
                self.status.rekey_trade(order_id, new_order_id)
                log.info(
                    "SCALP  | %-8s | reprice | limit=%s | id=%s",
                    symbol,
                    limit_price,
                    new_order_id,
                )
            except Exception as exc:
                if self.is_order_not_cancelable(exc):
                    log.warning(
                        "SCALP  | %s | reprice skipped | order already "
                        "resolving | %s",
                        symbol,
                        exc,
                    )
                else:
                    log.error("SCALP  | %s | reprice failed | %s", symbol, exc)

        _dispatch_concurrently(candidates, _reprice_one)

    def reprice_volatility_scalp_entries(self) -> None:
        """Actively re-quotes a resting cohort BUY order toward the
        current market instead of waiting passively for price to come
        back up to the original limit - by request: "do not wait at
        all until the order gets filled, because the price may not
        reach that point, you may have to lower a little." Only ever
        lowers the limit (tracks a further decline), never raises it -
        chasing the price up would mean paying more for the same dip-
        buy, defeating the point. Same fast dedicated cadence as
        reprice_volatility_scalp_exits.
        """
        now = time.monotonic()
        if now - self.last_volatility_entry_reprice < float(
            self.config.volatility_scalp_reprice_seconds
        ):
            return
        self.last_volatility_entry_reprice = now
        candidates: list[tuple[str, str, dict]] = []
        with _working_orders_lock(self):
            snapshot = list(self.working_orders.items())
        for order_id, order in snapshot:
            action = order.get("action")
            key = str(order.get("key") or "")
            if action != "BUY" or not key.startswith("STOCK:"):
                continue
            if order.get("cancel_requested_at") is not None:
                continue
            symbol = key.split(":", 1)[1]
            # Also keeps repricing an already-adopted cohort position's
            # resting BUY (e.g. an averaging-down order) even if it's no
            # longer live-eligible this exact cycle - same reasoning as
            # the other volatility-scalp gates.
            if (
                not self.strategy.is_volatility_scalp_eligible(symbol)
                and symbol not in self.volatility_scalp_positions
            ):
                continue
            quantity = order.get("quantity")
            if not quantity or quantity <= 0:
                continue
            candidates.append((order_id, symbol, order))
        if not candidates:
            return
        quotes = self._batched_quotes([symbol for _, symbol, _ in candidates])

        def _reprice_one(candidate: tuple[str, str, dict]) -> None:
            order_id, symbol, order = candidate
            key = str(order.get("key") or "")
            action = order.get("action")
            quantity = order.get("quantity")
            try:
                quote = quotes.get(symbol)
                if quote is None:
                    return
                limit_price = self.api.stock_limit_price(quote, "BUY")
                current_limit = order.get("limit_price")
                if (
                    limit_price is None
                    or current_limit is None
                    or limit_price >= current_limit
                ):
                    return
                _retry_once_on_rate_limit(self.api.cancel, order_id)
                new_order_id = _retry_once_on_rate_limit(
                    self.api.place_stock,
                    symbol,
                    "BUY",
                    quantity,
                    limit_price=limit_price,
                )
                _rekey_working_order(
                    self,
                    order_id,
                    new_order_id,
                    {
                        "submitted_at": now,
                        "key": key,
                        "action": action,
                        "cancel_requested_at": None,
                        "limit_price": limit_price,
                        "pnl": order.get("pnl"),
                        "quantity": quantity,
                    },
                )
                self.status.rekey_trade(order_id, new_order_id)
                log.info(
                    "SCALP  | %-8s | reprice entry | limit=%s | id=%s",
                    symbol,
                    limit_price,
                    new_order_id,
                )
            except QuoteUnavailableError as exc:
                # A momentarily missing/crossed bid-ask (thin/low-volume
                # penny stock, a quote glitch) is expected and already
                # fully handled - the loop just moves on to the next
                # order next cycle. WARNING, not ERROR - this isn't a
                # fault, it's the same "no data -> don't act" convention
                # every other entry gate in this strategy already uses.
                log.warning("SCALP  | %s | entry reprice skipped | %s", symbol, exc)
            except Exception as exc:
                if self.is_order_not_cancelable(exc):
                    log.warning(
                        "SCALP  | %s | entry reprice skipped | order "
                        "already resolving | %s",
                        symbol,
                        exc,
                    )
                else:
                    log.error(
                        "SCALP  | %s | entry reprice failed | %s", symbol, exc
                    )

        _dispatch_concurrently(candidates, _reprice_one)

    def reprice_resting_entries(self, core_session_active: bool) -> None:
        """Continuously re-quotes a resting general (non-volatility-
        scalp) BUY/SHORT entry order to cross further into the spread
        the longer it sits unfilled, instead of resting passively at
        the original mid-price until the hard order_timeout_seconds
        cancel gives up on it entirely with no attempt to improve the
        price first. Live incident: IBRX (a DISCOVERY-bucket long
        entry) got cancelled for never filling 4 separate times in
        ~15 minutes, always at the same passive mid-price, because
        nothing ever moved the resting order closer to a fillable
        price in between. By request: covers both entry directions - a
        BUY chases up toward the current ask, a SHORT chases down
        toward the current bid.

        Skips any symbol currently volatility-scalp eligible (or
        already adopted into that cohort) - reprice_volatility_scalp_
        entries handles those on its own, much faster, dedicated
        cadence instead. Skips a fractional BUY outside core hours -
        fractional cancel-and-replace can't succeed there either, same
        constraint reprice_resting_exits already respects.
        """
        now = time.monotonic()
        if now - self.last_entry_reprice < float(self.config.order_monitor_seconds):
            return
        self.last_entry_reprice = now
        candidates: list[tuple[str, str, dict]] = []
        with _working_orders_lock(self):
            snapshot = list(self.working_orders.items())
        for order_id, order in snapshot:
            action = order.get("action")
            key = str(order.get("key") or "")
            if action not in ("BUY", "SHORT") or not key.startswith("STOCK:"):
                continue
            if order.get("cancel_requested_at") is not None:
                continue
            symbol = key.split(":", 1)[1]
            if (
                self.strategy.is_volatility_scalp_eligible(symbol)
                or symbol in self.volatility_scalp_positions
            ):
                continue
            quantity = order.get("quantity")
            if not quantity or quantity <= 0:
                continue
            if (
                action == "BUY"
                and self.is_fractional_quantity(quantity)
                and not core_session_active
            ):
                continue
            candidates.append((order_id, symbol, order))
        if not candidates:
            return
        quotes = self._batched_quotes([symbol for _, symbol, _ in candidates])

        def _reprice_one(candidate: tuple[str, str, dict]) -> None:
            order_id, symbol, order = candidate
            key = str(order.get("key") or "")
            action = order.get("action")
            quantity = order.get("quantity")
            try:
                quote = quotes.get(symbol)
                if quote is None:
                    return
                current_limit = order.get("limit_price")
                if action == "BUY":
                    target_price = self.api.quote_ask(quote)
                    improved = (
                        target_price is not None
                        and current_limit is not None
                        and target_price > current_limit
                    )
                else:
                    target_price = self.api.quote_bid(quote)
                    improved = (
                        target_price is not None
                        and current_limit is not None
                        and target_price < current_limit
                    )
                if not improved:
                    return
                _retry_once_on_rate_limit(self.api.cancel, order_id)
                new_order_id = _retry_once_on_rate_limit(
                    self.api.place_stock,
                    symbol,
                    action,
                    quantity,
                    limit_price=target_price,
                )
                _rekey_working_order(
                    self,
                    order_id,
                    new_order_id,
                    {
                        "submitted_at": now,
                        "key": key,
                        "action": action,
                        "cancel_requested_at": None,
                        "limit_price": target_price,
                        "pnl": order.get("pnl"),
                        "quantity": quantity,
                    },
                )
                self.status.rekey_trade(order_id, new_order_id)
                log.info(
                    "REPRICE| %-8s | %-6s | limit=%s | id=%s",
                    symbol,
                    action,
                    target_price,
                    new_order_id,
                )
            except QuoteUnavailableError as exc:
                log.warning(
                    "REPRICE| %s | entry reprice skipped | %s", symbol, exc
                )
            except Exception as exc:
                if self.is_order_not_cancelable(exc):
                    log.warning(
                        "REPRICE| %s | entry reprice skipped | order "
                        "already resolving | %s",
                        symbol,
                        exc,
                    )
                else:
                    log.error(
                        "REPRICE| %s | entry reprice failed | %s", symbol, exc
                    )

        _dispatch_concurrently(candidates, _reprice_one)

    def account_state(self) -> tuple[Decimal, list[dict]]:
        now = time.monotonic()
        if (
            now - self.last_account_refresh
            >= float(self.config.account_refresh_seconds)
        ):
            # MIN_CASH_RESERVE_DOLLARS is subtracted right here, once, at
            # the fresh broker read - not at every call site that reads
            # cached_buying_power. This value stays cached (and this
            # already-reduced) for ACCOUNT_REFRESH_SECONDS; subtracting
            # the reserve again on every cache hit within that window
            # would compound each cycle and drive spendable capital to
            # zero almost immediately.
            balance = self.api.balance()
            # Raw, before MIN_CASH_RESERVE_DOLLARS - the dashboard should
            # show the account's real buying power (what Webull's own app
            # shows), not the internally-reserved figure trading logic
            # actually sizes against below. The reserve is a real safety
            # margin for order sizing, not something the display should
            # silently subtract and show as a gap against Webull's app.
            self.cached_raw_buying_power = self.api.buying_power_from_balance(
                balance
            )
            self.cached_buying_power = max(
                Decimal("0"),
                self.cached_raw_buying_power - self.config.min_cash_reserve_dollars,
            )
            self.cached_account_day_pnl = self.api.account_day_pnl_from_balance(
                balance
            )
            self.cached_account_value = self.api.account_value_from_balance(balance)
            self.cached_positions = self.api.positions()
            self.last_account_refresh = now
            if (
                self.short_selling_supported
                and self.cached_account_value is not None
                and self.cached_account_value < SHORT_SELLING_MIN_EQUITY
            ):
                # Same threshold Webull's own rejection enforces - catch
                # it here, proactively, instead of spending a live order
                # attempt (certain to fail) to discover it. Once equity
                # clears the minimum on a later refresh, no code re-
                # enables this automatically (matches handle_short_
                # selling_unsupported's existing "restart to re-enable"
                # behavior) - intentionally conservative rather than
                # flapping short-selling on and off around the threshold.
                self.short_selling_supported = False
                log.warning(
                    "SHORT  | account equity ($%s) is under Webull's $%s "
                    "minimum for short selling - disabling new short "
                    "entries for the rest of this run",
                    self.cached_account_value,
                    SHORT_SELLING_MIN_EQUITY,
                )
        return self.cached_buying_power, [dict(item) for item in self.cached_positions]

    def idle_cash_ramp_progress(self, buying_power: Decimal) -> Decimal:
        """0..1 - how far along the idle-cash gate-relaxation ramp the bot
        currently is. Keeping buying_power (already net of
        MIN_CASH_RESERVE_DOLLARS) deployed outranks entry quality, so the
        longer it sits unspent, the more entry_spread_ok/entry_extension_ok/
        vwap_supports_entry/tick_direction_ok loosen - see their
        idle_relaxation_multiplier parameter. Resets to 0 the moment
        record_trade() sees a new BUY/SHORT/MANUAL_BUY fill that counts
        toward this ramp - volatility-scalp fills deliberately don't (see
        record_trade's counts_toward_idle_cash_ramp), since this ramp
        only ever loosens the GENERAL strategy's own gates and scalp
        activity firing every few minutes was otherwise starving it
        from ever advancing, even while real buying power sat unused
        for hours.
        """
        if not self.config.idle_cash_relaxation_enabled or buying_power <= 0:
            return Decimal("0")
        idle_seconds = time.monotonic() - self.last_capital_deployed_at
        grace = float(self.config.idle_cash_grace_seconds)
        if idle_seconds <= grace:
            return Decimal("0")
        ramp = float(self.config.idle_cash_ramp_seconds)
        return Decimal(str(min(1.0, (idle_seconds - grace) / ramp)))

    def agent_assessment(self, symbol: str) -> dict | None:
        """The agent no longer scores individual symbols (see
        MarketResearchAgent - it now reviews account-wide performance,
        not per-symbol setups). Kept as an always-None stub so
        prioritized_stock_batch/stock_decision's existing "no
        assessment" handling doesn't need to change.
        """
        return None

    @staticmethod
    def _quote_size(quote: dict, *fields: str) -> Decimal | None:
        for field in fields:
            value = quote.get(field)
            if value in (None, ""):
                continue
            try:
                size = Decimal(str(value))
            except Exception:
                continue
            if size.is_finite() and size >= 0:
                return size
        return None

    def obi_score_for(self, symbol: str, category: str, quote: dict) -> Decimal | None:
        """Order-book-imbalance score for a symbol that's otherwise about
        to fire a BUY. Only ever called for that one symbol right before
        order placement - fetching L2 depth for every scanned symbol every
        cycle would badly overrun the "market" request-rate budget (a
        single depth call per symbol vs. today's one snapshot call per
        whole batch), so this stays a final, on-demand gate rather than a
        per-cycle metric like everything else in strategy.metrics.
        """
        depth = self.api.stock_depth(symbol, category)
        score = self.api.depth_imbalance(depth, OBI_DEPTH_LEVELS)
        if score is not None:
            return score
        bid_size = self._quote_size(quote, "bid_size", "bidSize", "bid_volume")
        ask_size = self._quote_size(quote, "ask_size", "askSize", "ask_volume")
        if bid_size is None or ask_size is None:
            return None
        total = bid_size + ask_size
        return bid_size / total if total > 0 else None

    def size_stock_entry(
        self,
        price: Decimal,
        entry_budget: Decimal,
        fractional_remaining: Decimal,
        whole_share_remaining: Decimal,
        core_session_active: bool,
        fractional_slot_available: bool = True,
        fractional_supported: bool = True,
        symbol: str = "",
        buying_power: Decimal | None = None,
    ) -> tuple[Decimal, Decimal, bool]:
        """Splits capital between fractional and whole-share entry sizing
        instead of one style claiming every candidate during core hours.

        fractional_remaining/whole_share_remaining are each computed ONCE
        per trade_stocks cycle (buying_power * their respective fraction)
        and decremented by the caller as buys land - passing a live,
        already-shrinking buying_power in here instead would let fractional
        sizing succeed for nearly every candidate (its own cap barely
        shrinks relative to total buying power), leaving whole-share
        sizing's larger capital slice essentially unreachable during core
        hours. fractional_slot_available additionally gates fractional
        sizing on a reserved position-count budget (see trade_stocks) - a
        fractional position can't be exited outside core hours, so
        fractional entries alone filling every MAX_OPEN_POSITIONS slot
        would strand the account with no room for entries of any style for
        the rest of the day. fractional_supported is False for a specific
        symbol Webull has already rejected with
        FRACT_TICKER_DONT_SUPPORT_TRADE (see
        handle_fractional_ticker_unsupported) - a per-security
        restriction, distinct from fractional_trading_enabled's
        account-wide one.
        Returns (quantity, buffered_price, is_fractional).
        """
        if (
            core_session_active
            and self.fractional_trading_enabled
            and fractional_supported
            and fractional_slot_available
            and fractional_remaining > 0
        ):
            target_notional = min(
                fractional_remaining,
                entry_budget,
                self.config.max_order_notional,
            )
            quantity, buffered_price = self.strategy.dollar_stock_quantity(
                price, target_notional
            )
            if quantity > 0:
                return quantity, buffered_price, True
        whole_share_budget = (
            min(entry_budget, whole_share_remaining)
            if core_session_active
            else entry_budget
        )
        quantity, buffered_price = self.strategy.stock_order_quantity(
            price, whole_share_budget
        )
        # stock_order_quantity is typed -> tuple[int, Decimal] (its own
        # affordability math is integer-based) - normalize to Decimal
        # here since every downstream caller of this whole-share
        # quantity (is_fractional_quantity, record_trade's
        # working_orders["quantity"], etc.) expects one. Without this,
        # a plain int leaking into working_orders["quantity"] crashed
        # a later is_fractional_quantity(quantity) call with "'int'
        # object has no attribute 'to_integral_value'" - live evidence,
        # traced to this line.
        quantity = Decimal(quantity)
        # By request: risk-based position sizing (the professional 1-2%
        # rule, adapted for this account's size - see
        # stock_risk_per_trade_fraction) - an ADDITIONAL cap layered on
        # top of the affordability/notional caps above, not a
        # replacement for them. Sizes against the account's real total
        # buying power (not the bucket-allocated entry_budget slice
        # above), same as how the professional rule is normally stated
        # ("risk 1% of the account"), using the stop distance
        # stock_decision itself will use for this symbol.
        if quantity > 0 and symbol and buying_power is not None:
            stop_percent = self.strategy.adaptive_stop_percent(symbol)
            stop_price = price * (Decimal("1") - stop_percent)
            risk_cap = self.strategy.risk_based_share_count(
                price,
                stop_price,
                buying_power,
                self.config.stock_risk_per_trade_fraction,
            )
            if risk_cap < quantity:
                quantity = Decimal(risk_cap)
                min_lot = self.strategy.minimum_lot_size(price)
                if 0 < quantity < min_lot:
                    # The risk cap alone can't afford even the exchange-
                    # mandated minimum lot for this price band - skip
                    # rather than place an order the broker would
                    # reject, same convention stock_order_quantity
                    # itself already uses.
                    quantity = Decimal("0")
        return quantity, buffered_price, False

    def refresh_agent_discoveries(self) -> None:
        """Sourced from the deterministic market_pulse screener data, not
        the research agent - this keeps working (and keeps priority
        scanning pointed at today's actual movers) even if AGENT_ENABLED
        is false or a Groq request fails.
        """
        self.refresh_market_pulse()
        available = set(self.stock_symbols)
        pulse_symbols = {
            entry["symbol"]
            for bucket in self.market_pulse_cache.values()
            for entry in bucket
        }
        self.agent_popular_symbols = {
            symbol for symbol in pulse_symbols if symbol in available
        }

    def submit_strategy_review(
        self,
        positions: list[dict],
        buying_power: Decimal,
        force: bool = False,
        event: str = "ROUTINE_REVIEW",
    ) -> None:
        """Sends the agent a compact snapshot of real account performance
        (holdings, today's pnl, recent trades) - not per-symbol setups -
        and lets it assess whether the CURRENT strategy is working. See
        MarketResearchAgent.submit_strategy_review's docstring: this is
        review-gated, the result is only ever a logged/dashboard
        suggestion, never applied automatically.
        """
        if not self.market_agent or not self.config.strategy_review_enabled:
            return
        held = [
            {
                "symbol": str(item.get("symbol", "")).upper(),
                "type": item.get("instrument_type"),
                "qty": self._compact_number(item.get("quantity")),
                "cost": self._compact_number(item.get("cost_price"), 2),
                "unrealized_pnl": self._compact_number(
                    self.strategy.position_unrealized_pnl(item), 2
                ),
                "day_pnl": self._compact_number(
                    self.strategy.position_day_pnl(item), 2
                ),
            }
            for item in positions
            if Decimal(str(item.get("quantity", "0"))) != 0
        ]
        recent_trades = [
            {
                "symbol": trade.get("symbol"),
                "action": trade.get("action"),
                "entry": trade.get("entry_price"),
                "exit": trade.get("limit_price"),
                "qty": trade.get("quantity"),
                "pnl": trade.get("pnl"),
            }
            for trade in list(self.status.trades)[
                : self.config.strategy_review_trade_history_limit
            ]
        ]
        self.market_agent.submit_strategy_review(
            {
                "event": event,
                "buying_power": self._compact_number(buying_power, 0),
                "holdings": held,
                # Same reconciliation StatusWriter's own dashboard total
                # uses - Webull's own account_day_pnl when available
                # (ground truth), not just the bot's local estimate. The
                # agent should review real performance, not the same
                # drifting number this session's earlier fixes addressed.
                "pnl_today": StatusWriter.pnl_today_payload(
                    self.daily_realized_pnl,
                    sum(
                        (Decimal(str(item["day_pnl"])) for item in held),
                        Decimal("0"),
                    ),
                    self.cached_account_day_pnl,
                ),
                "recent_trades": recent_trades,
            },
            force=force,
        )

    @staticmethod
    def _compact_number(value, digits: int | None = None):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0
        if digits is None:
            return int(number) if number == int(number) else round(number, 4)
        rounded = round(number, digits)
        return int(rounded) if digits == 0 else rounded

    def stop_loss_guard_active(self) -> bool:
        """freqtrade-style StoplossGuard: pause NEW entries (stock and
        short, see trade_stocks) if STOP_LOSS_GUARD_TRADE_LIMIT or more
        stop-losses have fired within the trailing STOP_LOSS_GUARD_
        LOOKBACK_SECONDS window - a frequency-based signal that the
        strategy is currently whipsawing in a bad regime, distinct from
        the dollar/equity-based breakers in handle_portfolio_circuit_
        breaker/handle_daily_loss_breaker (which can take much longer to
        trip, and which liquidate everything when they do). This never
        liquidates anything - existing positions keep being managed
        normally; it just declines to add new risk until the recent stop
        rate cools off, then resumes on its own (no restart needed).
        """
        if not self.config.stop_loss_guard_enabled:
            return False
        now = time.monotonic()
        if now < self.stop_loss_guard_until:
            return True
        lookback = float(self.config.stop_loss_guard_lookback_seconds)
        while self.recent_stop_losses and now - self.recent_stop_losses[0] > lookback:
            self.recent_stop_losses.popleft()
        if len(self.recent_stop_losses) < self.config.stop_loss_guard_trade_limit:
            return False
        self.stop_loss_guard_until = now + float(
            self.config.stop_loss_guard_cooldown_seconds
        )
        log.warning(
            "GUARD  | stop-loss guard tripped | %s stops in the last %ss | "
            "pausing new entries for %ss",
            len(self.recent_stop_losses),
            self.config.stop_loss_guard_lookback_seconds,
            self.config.stop_loss_guard_cooldown_seconds,
        )
        return True

    def symbol_quarantined(self, key: str) -> bool:
        """freqtrade-style LowProfitPairs: same shape as stop_loss_guard_
        active but scoped to one symbol's own recent realized P&L instead
        of an account-wide stop count. A symbol that's been a net loser
        lately pauses new entries on just that symbol - via
        symbol_pnl_history, fed by record_trade on every PROFIT/STOP/
        MANUAL_SELL exit - while every other symbol keeps trading
        normally. Never liquidates the symbol's existing position, if any.
        """
        if not self.config.symbol_quarantine_enabled:
            return False
        now = time.monotonic()
        if now < self.symbol_quarantine_until.get(key, 0.0):
            return True
        history = self.symbol_pnl_history.get(key)
        if not history:
            return False
        lookback = float(self.config.symbol_quarantine_lookback_seconds)
        while history and now - history[0][0] > lookback:
            history.popleft()
        if len(history) < self.config.symbol_quarantine_min_trades:
            return False
        total_pnl = sum((pnl for _, pnl in history), Decimal("0"))
        if total_pnl > -self.config.symbol_quarantine_loss_dollars:
            return False
        self.symbol_quarantine_until[key] = now + float(
            self.config.symbol_quarantine_cooldown_seconds
        )
        log.warning(
            "GUARD  | symbol quarantined | %s | net $%s over %s trades in "
            "the last %ss | pausing entries for %ss",
            key,
            total_pnl,
            len(history),
            self.config.symbol_quarantine_lookback_seconds,
            self.config.symbol_quarantine_cooldown_seconds,
        )
        return True

    def handle_portfolio_circuit_breaker(
        self,
        positions: list[dict],
        buying_power: Decimal,
    ) -> bool:
        if not self.config.loss_circuit_breaker_enabled:
            return False

        now = time.monotonic()
        if self.entries_paused:
            old_enough = (
                now - self.circuit_breaker_time
                >= self.config.loss_reevaluation_seconds
            )
            if old_enough:
                self.entries_paused = False
                log.warning(
                    "CIRCUIT | resumed after %ss reevaluation pause",
                    self.config.loss_reevaluation_seconds,
                )
                return False
            if (
                self.market_agent
                and now - self.last_circuit_research
                >= self.config.loss_reevaluation_seconds
            ):
                self.last_circuit_research = now
                self.submit_strategy_review(
                    positions,
                    buying_power,
                    force=True,
                    event="POST_LIQUIDATION_REEVALUATION",
                )
            return True

        states = []
        for position in positions:
            if Decimal(str(position.get("quantity", "0"))) == 0:
                continue
            symbol = str(position.get("symbol", "")).upper()
            states.append(
                {
                    "symbol": symbol,
                    "unrealized_pnl": self.strategy.position_unrealized_pnl(
                        position
                    ),
                }
            )
        decision = self.strategy.portfolio_decision(
            states,
            self.config.loss_spree_position_count,
            self.config.loss_spree_total_dollars,
        )
        if decision.action != "LIQUIDATE":
            return False

        log.critical(
            "CIRCUIT | LIQUIDATE | losers=%s | loss=$%.2f | %s",
            decision.losing_positions,
            decision.total_loss,
            decision.reason,
        )
        submitted = self.api.close_all_positions(
            loss_callback=self.wash_sales.block,
        )
        log.warning("CIRCUIT | close orders submitted=%s | entries paused", len(submitted))
        self.entries_paused = True
        self.circuit_breaker_time = now
        self.last_circuit_research = now
        self.last_account_refresh = 0.0
        self.submit_strategy_review(
            positions,
            buying_power,
            force=True,
            event="LOSS_CIRCUIT_BREAKER_LIQUIDATION",
        )
        return True

    def handle_daily_loss_breaker(self) -> bool:
        """Halt entries for the rest of the day once realized stop-loss
        exits alone (not counting the expected EOD closeout) add up past
        daily_max_loss_fraction of account equity. The per-position stop
        already bounds any single loss; this bounds how many of those a
        bad day can rack up before the bot stops opening new positions.

        By request, after finding this circuit breaker disabled both by
        code default and on the live host: enabled by default now, and
        the threshold is a fraction of account equity (the researched
        3-5% daily-drawdown convention) instead of a flat dollar amount
        that doesn't scale with account size - $50 used to be 25% of
        this account's equity, nowhere near a real daily limit. Falls
        back to daily_realized_loss never tripping (rather than raising)
        if account value isn't cached yet - same "no data -> don't
        block" convention as every other gate, applied to a circuit
        breaker's own inputs.
        """
        if not self.config.daily_loss_circuit_breaker_enabled:
            return False
        if self.daily_loss_breaker_triggered:
            return True
        if not self.cached_account_value or self.cached_account_value <= 0:
            return False
        max_loss_dollars = (
            self.cached_account_value * self.config.daily_max_loss_fraction
        )
        if self.daily_realized_loss < max_loss_dollars:
            return False
        log.critical(
            "CIRCUIT | DAILY LOSS LIMIT | realized=$%.2f >= limit=$%.2f "
            "(%.0f%% of $%.2f equity) | halting new entries for the "
            "rest of the trading day",
            self.daily_realized_loss,
            max_loss_dollars,
            self.config.daily_max_loss_fraction * 100,
            self.cached_account_value,
        )
        submitted = self.api.close_all_positions(loss_callback=self.wash_sales.block)
        log.warning(
            "CIRCUIT | close orders submitted=%s | entries halted until "
            "tomorrow's session",
            len(submitted),
        )
        self.daily_loss_breaker_triggered = True
        self.last_account_refresh = 0.0
        return True

    def record_realized_exit(
        self,
        average_cost: Decimal,
        exit_price: Decimal,
        quantity: Decimal,
        multiplier: int = 1,
    ) -> Decimal:
        """Track today's realized P&L from a submitted exit's limit price.

        This is an estimate (actual fill price can differ slightly), which
        is fine for a dashboard total and the daily-loss circuit breaker -
        both care about the running picture, not cent-perfect accounting.
        Returns the estimated pnl so callers can show it on the trade log.
        """
        pnl = (exit_price - average_cost) * quantity * multiplier - self.config.sell_fee_dollars
        self.daily_realized_pnl += pnl
        if pnl < 0:
            self.daily_realized_loss += -pnl
        self.daily_pnl.record(self.daily_realized_pnl, self.daily_realized_loss)
        return pnl

    def reverse_phantom_exit(
        self, pnl: Decimal | None, order_id: str | None = None
    ) -> None:
        """Undo a realized-exit pnl that was recorded at order SUBMISSION
        time (see record_realized_exit) once it's confirmed the order
        never actually filled - either it was cancelled/failed outright,
        or it was deliberately abandoned mid-flight (escalation cancels
        the gentle order and lets a fresh one fire its own pnl next
        cycle). Without this, an exit that never fills still permanently
        inflates the daily realized total as if it had.

        Also discards the matching entry from the dashboard's trade log
        (see StatusWriter.discard_trade) - record_trade wrote it
        optimistically at the same submission time as the phantom pnl, so
        without this a cancelled order stays visible on Recent Trades
        forever, labeled as a completed profit that never happened.
        """
        if order_id:
            self.status.discard_trade(order_id)
        if not pnl:
            return
        self.daily_realized_pnl -= pnl
        if pnl < 0:
            self.daily_realized_loss = max(
                Decimal("0"), self.daily_realized_loss - (-pnl)
            )
        self.daily_pnl.record(self.daily_realized_pnl, self.daily_realized_loss)

    @staticmethod
    def is_fractional_quantity(quantity: Decimal) -> bool:
        return quantity != quantity.to_integral_value()

    @staticmethod
    def max_fractional_position_slots(
        max_open_positions: int,
        fractional_fraction: Decimal,
        whole_share_fraction: Decimal,
    ) -> int:
        """Caps how many concurrently-open fractional-quantity stock
        positions there can be, reserved in the same proportion as
        fractional's capital share. A fractional position can't be exited
        outside core hours (Webull constraint - see is_fractional_quantity
        gating in trade_stocks), so letting fractional entries alone fill
        every MAX_OPEN_POSITIONS slot during core hours would strand the
        account with an unexitable, maxed-out position count for the rest
        of the day - no new entries of any style until the next core
        session. At least 1 slot is always reserved when fractional
        capital is allocated at all.
        """
        capital_split = fractional_fraction + whole_share_fraction
        if capital_split <= 0:
            return max_open_positions
        return max(1, int(max_open_positions * fractional_fraction / capital_split))

    def price_sanity_ok(self, symbol: str, last_price: Decimal, limit_price: Decimal) -> bool:
        """Fat-finger guard: reject a limit price that's implausibly far
        from the last observed trade price instead of trusting sizing/
        pricing math blindly. Catches a stale or corrupted quote producing
        a wildly wrong limit before it ever reaches the broker - hardcoded,
        not config, since this is a sanity backstop, not a tuning knob.

        Records the rejection in price_sanity_rejected_at - see
        price_sanity_cooldown_ready. Live incident: one illiquid
        symbol's bid/ask sat consistently ~9-10% off its own last-trade
        price (a real market condition on a thin quote, not a bad
        broker read - past _sane_bid_or_ask's own, looser 8% tolerance,
        but still past this stricter 5% one), rejecting an entry attempt
        on essentially every single scan cycle for hours with no
        backoff between attempts and no symbol in the log line to even
        identify which stock it was.
        """
        if last_price <= 0:
            return True
        deviation = abs(limit_price - last_price) / last_price
        if deviation > PRICE_SANITY_TOLERANCE:
            self.price_sanity_rejected_at[symbol] = time.monotonic()
            log.error(
                "GUARD  | %-8s | price sanity check failed | last=%.4f limit=%.4f "
                "deviation=%.1f%% (max %.0f%%) | order skipped",
                symbol,
                last_price,
                limit_price,
                deviation * 100,
                PRICE_SANITY_TOLERANCE * 100,
            )
            return False
        return True

    def price_sanity_cooldown_ready(self, symbol: str) -> bool:
        """False while symbol is still within PRICE_SANITY_COOLDOWN_SECONDS
        of its last price_sanity_ok rejection - without this, a symbol
        whose quote sits just past the sanity tolerance gets retried
        (and re-rejected) on literally every scan cycle forever, wasting
        a batch slot another, viable candidate could have used instead.

        Live incident (this bug): originally entry-only (the docstring
        used to claim "unlike the exit side's stalled-order backstops,
        this only ever backs off" - that assumption was wrong in
        practice). BMEA's profit-take order re-escalated and resubmitted
        every ~15-20s continuously for over 5 HOURS, hitting this exact
        price-sanity rejection ~570 times with zero backoff, because
        place_stock_scaled itself never checked this cooldown - only the
        entry code paths checked it themselves, before ever calling
        place_stock_scaled. Now enforced directly inside
        place_stock_scaled, so it applies uniformly to every order this
        function submits - entries AND exits alike - not just whichever
        callers happened to remember to check it first.
        """
        rejected_at = self.price_sanity_rejected_at.get(symbol)
        if rejected_at is None:
            return True
        return (
            time.monotonic() - rejected_at
            >= float(self.config.price_sanity_cooldown_seconds)
        )

    def post_stop_reentry_ready(self, symbol: str) -> bool:
        """False while symbol is still within
        volatility_scalp_post_stop_cooldown_seconds of its last STOP-loss
        exit. By request, after the DAIC incident: 3 stop-losses in
        ~9 minutes on one symbol during a fast decline, erasing the
        day's gains, because nothing throttled re-entry into the exact
        same falling knife right after being stopped out of it - the
        volatility-scalp cohort's re-entry cooldown is deliberately
        zeroed for everything else ("orders can be made as frequently
        as possible"), and this cohort explicitly bypasses quarantine/
        the stop-loss guard/wash-sale blocks by request ("keep trading
        through losses"). This is a narrow, deliberate exception to
        that: it only pauses the ONE symbol that just stopped out, for
        a few minutes, not the strategy - compatible with "keep trading
        through losses" (the other 7 concurrent slots and every other
        symbol are completely unaffected) while closing the specific
        gap DAIC exposed. Fails open (True) for a symbol with no
        recorded stop-loss yet, same convention as every other cooldown
        gate in this file.
        """
        stopped_at = self.last_volatility_stop_loss_at.get(symbol)
        if stopped_at is None:
            return True
        return (
            time.monotonic() - stopped_at
            >= float(self.config.volatility_scalp_post_stop_cooldown_seconds)
        )

    def record_order_error(self, symbol: str, exc: Exception) -> None:
        """Order-error guard: distinct from the existing P&L-based circuit
        breakers (daily-loss, loss-spree) because it fires on *error
        rate*, not realized loss - the guard against a rogue loop or a
        systematically broken order path (bad auth, malformed payload,
        API outage) spinning through the whole symbol universe before any
        single trade even fills.

        Blacklists only the offending symbol (reusing
        broker_conflict_symbols - every entry path already skips symbols
        in that set), not the whole account. This used to trip a global
        kill switch that halted every symbol's entries AND exits until
        the process was restarted - in production, a single symbol stuck
        in a broker-side rejection (e.g. Webull's $0.10-$0.999 lot-size
        rule) repeatedly tripped this and froze the entire bot for the
        rest of the session over a problem confined to one symbol. The
        error-rate counter itself stays global (still the right signal
        for "something is systematically broken," e.g. bad auth spamming
        errors across many different symbols), but the consequence is now
        scoped to whichever symbol actually caused it.
        """
        now = time.monotonic()
        self.order_error_times.append(now)
        while (
            self.order_error_times
            and now - self.order_error_times[0] > ORDER_ERROR_WINDOW_SECONDS
        ):
            self.order_error_times.popleft()
        if len(self.order_error_times) >= CONSECUTIVE_ORDER_ERROR_LIMIT:
            self.order_error_times.clear()
            already_blacklisted = symbol in self.broker_conflict_symbols
            self.broker_conflict_symbols.add(symbol)
            self.pending_stock_exits.discard(symbol)
            self.pending_option_exits.discard(symbol)
            self.stop_exit_submitted.pop(symbol, None)
            self.stop_loss_escalated.discard(symbol)
            self.stop_condition_since.pop(symbol, None)
            if not already_blacklisted:
                log.critical(
                    "GUARD  | %s order errors in %ss (last: %s | %s) | "
                    "blacklisting %s from further automated action for "
                    "the rest of the day - other symbols are unaffected",
                    CONSECUTIVE_ORDER_ERROR_LIMIT,
                    ORDER_ERROR_WINDOW_SECONDS,
                    symbol,
                    exc,
                    symbol,
                )

    def place_stock_scaled(
        self,
        symbol: str,
        side: str,
        quantity: int | Decimal,
        key: str,
        quote: dict,
        fractional: bool = False,
        limit_price_override: Decimal | None = None,
    ) -> str | None:
        """Slices a large order into smaller clips instead of dumping the
        whole size in one order - large firms never do that because it
        moves the price against them. Below ICEBERG_MIN_SHARES this is
        identical to calling api.place_stock directly (today's behavior,
        unchanged for the bot's normal small scalp sizes); at/above it,
        places the first clip now and schedules the remainder to trickle
        out via process_iceberg_orders() on later cycles - never blocks
        the polling loop with a sleep, since that would stall order
        monitoring, the dashboard, and every other symbol for the whole
        slice duration.

        Also enforces price_sanity_cooldown_ready here, universally, for
        every order this function submits - entries AND exits alike.
        Live incident: BMEA's profit-take order re-escalated and
        resubmitted every ~15-20s continuously for over 5 hours, hitting
        the price-sanity rejection ~570 times with zero backoff, because
        only entry code paths checked this cooldown themselves before
        calling here - nothing stopped an exit from hammering the same
        unreachable price forever.
        """
        if not self.price_sanity_cooldown_ready(symbol):
            return None
        total = Decimal(str(quantity))
        last_price = self.api.quote_price(quote)
        # Webull requires a 100-share minimum lot for any order (either
        # side) while price sits in the $0.10-$0.999 band - slicing into
        # ICEBERG_SLICE_SHARES=10-share clips there guarantees every
        # single slice gets rejected (live incident: HOWL, 417
        # OAUTH_OPENAPI_CANT_TRADE_FOR_PRICE_BETWEEN_0099_AND_0999, on
        # every iceberg slice attempt). A lot-restricted order is cheap
        # enough in absolute notional (100 shares of a sub-$1 stock) that
        # it doesn't need price-impact slicing anyway - place it whole.
        clip = (
            total
            if total < ICEBERG_MIN_SHARES
            or fractional
            or self.strategy.minimum_lot_size(last_price) > 1
            else Decimal(ICEBERG_SLICE_SHARES)
        )
        if not fractional and clip * last_price > HARD_ORDER_NOTIONAL_CEILING:
            clip = (HARD_ORDER_NOTIONAL_CEILING / last_price).to_integral_value(
                rounding=ROUND_DOWN
            )
            if clip <= 0:
                log.error(
                    "GUARD  | %s | order notional exceeds the hard ceiling "
                    "($%s) even at 1 share | order skipped",
                    symbol,
                    HARD_ORDER_NOTIONAL_CEILING,
                )
                return None
            clip = min(clip, total)
        elif fractional and clip * last_price > HARD_ORDER_NOTIONAL_CEILING:
            log.error(
                "GUARD  | %s | fractional order notional exceeds the hard "
                "ceiling ($%s) | order skipped",
                symbol,
                HARD_ORDER_NOTIONAL_CEILING,
            )
            return None
        limit_price = (
            limit_price_override
            if limit_price_override is not None
            else self.api.stock_limit_price(quote, side)
        )
        if not self.price_sanity_ok(symbol, last_price, limit_price):
            return None
        try:
            order_id = self.api.place_stock(
                symbol,
                side,
                clip if not fractional else total,
                limit_price=limit_price,
                fractional=fractional,
            )
        except Exception as exc:
            self.record_order_error(symbol, exc)
            raise
        remaining = total - (clip if not fractional else total)
        if remaining > 0:
            self.iceberg_orders[f"{symbol}:{side}"] = {
                "symbol": symbol,
                "side": side,
                "key": key,
                "remaining": remaining,
                "last_slice_at": time.monotonic(),
            }
            log.info(
                "ICEBERG| %s | %s | first clip=%s | remaining=%s over %s "
                "more slice(s)",
                symbol,
                side,
                clip,
                remaining,
                -(-remaining // ICEBERG_SLICE_SHARES),
            )
        return order_id

    def process_iceberg_orders(self) -> None:
        now = time.monotonic()
        for iceberg_key in list(self.iceberg_orders):
            entry = self.iceberg_orders[iceberg_key]
            if now - entry["last_slice_at"] < ICEBERG_SLICE_INTERVAL_SECONDS:
                continue
            symbol = entry["symbol"]
            side = entry["side"]
            try:
                quote = self.api.stock_quote(symbol)
                last_price = self.api.quote_price(quote)
                # Same lot-restriction guard as place_stock_scaled - price
                # can drift into the $0.10-$0.999 band between the first
                # clip and a later slice even if it wasn't there at
                # submission time.
                clip = (
                    entry["remaining"]
                    if self.strategy.minimum_lot_size(last_price) > 1
                    else min(entry["remaining"], Decimal(ICEBERG_SLICE_SHARES))
                )
                limit_price = self.api.stock_limit_price(quote, side)
                if not self.price_sanity_ok(symbol, self.api.quote_price(quote), limit_price):
                    entry["last_slice_at"] = now
                    continue
                order_id = self.api.place_stock(
                    symbol,
                    side,
                    clip,
                    limit_price=limit_price,
                )
            except Exception as exc:
                self.record_order_error(symbol, exc)
                log.error("ICEBERG| %s | slice failed | %s", symbol, exc)
                entry["last_slice_at"] = now
                continue
            self.record_trade(
                entry["key"], order_id, side, entry_price=limit_price, quantity=clip
            )
            entry["remaining"] -= clip
            entry["last_slice_at"] = now
            log.info(
                "ICEBERG| %s | %s | slice=%s | remaining=%s",
                symbol,
                side,
                clip,
                entry["remaining"],
            )
            if entry["remaining"] <= 0:
                del self.iceberg_orders[iceberg_key]

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

    def reconcile_order_history(self) -> None:
        """Log-only audit: cross-checks today's Webull order history
        against every order_id the bot itself submitted today (see
        submitted_order_ids_today). An order in Webull's history the bot
        never recorded is very likely a manual action taken directly in
        the Webull app - this never changes any bot state (sizing, pnl,
        gates), purely a visibility signal. Each unrecognized order is
        logged once per day (reconciliation_flagged_order_ids), not every
        cycle this runs.
        """
        if not self.config.order_history_reconcile_enabled:
            return
        now = time.monotonic()
        if now - self.last_order_history_reconcile < self.config.order_history_reconcile_seconds:
            return
        self.last_order_history_reconcile = now
        today = self.now().date()
        try:
            # Webull rejects a same-day start_date/end_date pair outright
            # (417 OAUTH_OPENAPI_PARAM_ERR) - a 1-day lookback is the
            # smallest range it accepts. Yesterday's orders are filtered
            # back out below (placed_today), so this doesn't widen what
            # actually gets flagged.
            history = self.api.order_history(
                (today - timedelta(days=1)).isoformat(), today.isoformat()
            )
        except Exception as exc:
            log.warning("RECON  | order history fetch failed | %s", exc)
            return
        # place_time_at is UTC (a trailing "Z"), not the bot's trading
        # timezone - comparing it against today's ET-local date string
        # would misclassify anything placed in the last few hours of ET
        # extended trading (already the next UTC calendar date) as
        # "yesterday" and silently skip it.
        today_prefix = datetime.now(timezone.utc).date().isoformat()
        for combo in history:
            client_order_id = combo.get("client_order_id")
            if not client_order_id:
                continue
            placed_today = any(
                str(order.get("place_time_at", "")).startswith(today_prefix)
                for order in combo.get("orders", [])
            )
            if not placed_today:
                continue
            if client_order_id in self.submitted_order_ids_today:
                continue
            if client_order_id in self.reconciliation_flagged_order_ids:
                continue
            self.reconciliation_flagged_order_ids.add(client_order_id)
            for order in combo.get("orders", []):
                log.warning(
                    "RECON  | %-8s | order not recognized by the bot - "
                    "likely a manual action outside it | side=%s status=%s "
                    "qty=%s filled=%s id=%s",
                    order.get("symbol", "?"),
                    order.get("side"),
                    order.get("status"),
                    order.get("total_quantity"),
                    order.get("filled_quantity"),
                    client_order_id,
                )

    def log_trade_events(self) -> None:
        """Phase 0 of the polling-to-streaming migration (see the plan):
        drains and logs whatever TradeEventStreamService received since
        the last cycle. Purely observational - no trading state is
        touched here. The goal is to document the real payload schema
        from live traffic (the SDK source only confirms one field,
        request_id) before any later phase parses these events for
        anything that matters.
        """
        if self.trade_event_service is None:
            return
        for event_type, subscribe_type, payload in self.trade_event_service.drain():
            log.info(
                "EVENTS | event_type=%s | subscribe_type=%s | %s",
                event_type,
                subscribe_type,
                payload,
            )

    def backfill_stock_symbols(self, count: int) -> int:
        active = set(self.stock_symbols)
        added = 0
        while added < count and self.reserve_symbols:
            candidate = self.reserve_symbols.pop(0)
            if candidate in active or candidate in self.invalid_symbols:
                continue
            self.stock_symbols.append(candidate)
            active.add(candidate)
            added += 1
        return added

    def seed_volatility_windows(self, symbols: list[str]) -> None:
        """Warm-starts each not-yet-seen symbol's volatility-scalp window
        from real M1 bar closes in one batched call per category, instead
        of leaving it to build up one live snapshot poll at a time. Fully
        self-limiting: TradingStrategy.seed_volatility_window is a no-op
        for any symbol whose window already has data (from a prior seed
        or from live polling), so the candidate list naturally shrinks to
        nothing as the watchlist gets covered.
        """
        unseeded = [
            symbol
            for symbol in symbols
            if not self.strategy.volatility_price_history.get(symbol)
        ]
        if not unseeded:
            return
        grouped: dict[str, list[str]] = defaultdict(list)
        for symbol in unseeded:
            grouped[self.stock_categories.get(symbol, "US_STOCK")].append(symbol)
        for category, category_symbols in grouped.items():
            try:
                closes_by_symbol = self.api.recent_minute_closes(
                    category_symbols,
                    category,
                    self.config.volatility_scalp_lookback_samples,
                )
            except Exception as exc:
                log.warning("SCALP  | bar seed fetch failed | %s | %s", category, exc)
                continue
            for symbol, closes in closes_by_symbol.items():
                self.strategy.seed_volatility_window(symbol, closes)
                # select_volatility_scalp_symbols candidates come from
                # self.strategy.prices, which otherwise only gets
                # populated by a live quote scan (update_stock_snapshot)
                # - without this, bar-seeding the volatility window alone
                # still wouldn't make a symbol visible to cohort
                # selection until it was actually scanned. Never
                # overwrites an already-live price with a stale bar
                # close.
                if closes and symbol not in self.strategy.prices:
                    self.strategy.prices[symbol] = Decimal(str(closes[-1]))

    def select_volatility_scalp_symbols(self) -> None:
        """Re-ranks the curated volatility-scalp cohort from data already
        being collected during normal scanning (self.strategy.prices/
        volatility_price_history) - no extra API calls needed. Picks the
        top VOLATILITY_SCALP_SYMBOL_COUNT symbols, by realized short-
        window volatility, among those priced at or under
        VOLATILITY_SCALP_MAX_PRICE with enough samples to have a real
        reading. Re-run periodically (VOLATILITY_SCALP_RESELECT_SECONDS),
        so a symbol that's cooled off drops out and a newly-hot one
        (from anywhere in the scanned universe, not just today's
        starting picks) can take its place - "keep looking for volatile
        stocks to add to the group."
        """
        if not self.config.volatility_scalp_enabled:
            return
        now = time.monotonic()
        if (
            now - self.last_volatility_symbol_selection
            < float(self.config.volatility_scalp_reselect_seconds)
        ):
            return
        candidates: list[tuple[Decimal, str]] = []
        for symbol, price in self.strategy.prices.items():
            if price <= 0 or price > self.config.volatility_scalp_max_price:
                continue
            stdev = self.strategy.realized_volatility_percent(symbol)
            if stdev is None:
                continue
            candidates.append((stdev, symbol))
        if not candidates:
            # Don't stamp the throttle yet - this call ran before any
            # symbol had accumulated a real volatility reading (always
            # true for the very first call or two right after startup,
            # since self.strategy.prices is still empty then). Stamping
            # here anyway would "spend" the throttle on a result with no
            # real data behind it and leave the cohort empty for the
            # full VOLATILITY_SCALP_RESELECT_SECONDS (default 30 min)
            # before ever trying again.
            return
        self.last_volatility_symbol_selection = now
        candidates.sort(key=lambda item: item[0], reverse=True)
        selected = {
            symbol
            for _, symbol in candidates[: self.config.volatility_scalp_symbol_count]
        }
        if selected != self.volatility_scalp_symbols:
            log.info(
                "SCALP  | daily cohort | %s",
                ", ".join(sorted(selected)) if selected else "(none eligible yet)",
            )
        self.volatility_scalp_symbols = selected

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
        for symbol in batch:
            if symbol in self.broker_conflict_symbols:
                continue
            try:
                quote = quote_by_symbol.get(symbol)
                if not quote:
                    continue
                price = self.api.quote_price(quote)
                self.strategy.update_stock_snapshot(quote, price)
                if self.strategy.is_volatility_scalp_eligible(symbol):
                    self.volatility_scalp_recently_eligible.add(symbol)
                else:
                    self.volatility_scalp_recently_eligible.discard(symbol)
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
                    decision = self.strategy.volatility_scalp_exit_override(
                        decision,
                        quantity,
                        cost,
                        price,
                        averaging_available=True,
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
                            "scalp - still in post-stop-loss cooldown",
                            self.post_stop_reentry_ready(symbol),
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
                            "scalp - no dip/breakout/reversal trigger",
                            (
                                self.strategy.volatility_scalp_dip_signal(
                                    symbol, price
                                )
                                or self.strategy.dual_thrust_breakout_signal(
                                    symbol, price
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
                    # symbol_quarantined (recent-loss pause), a wash-
                    # sale block, or rate_capped (hourly trade cap),
                    # since all of those exist specifically to slow
                    # down or pause trading after losses - exactly what
                    # this strategy is meant to keep doing anyway. Only
                    # cooldown_ready (a cross-order-submission race
                    # guard, not a loss-driven pause -
                    # trade_cooldown_seconds defaults to 0) and
                    # volatility_scalp_reentry_ready (zeroed by request -
                    # "orders can be made as frequently as possible
                    # without a cooldown") still gate timing.
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
                    # THREE independent, OR'd entry triggers - by request,
                    # every extra qualifying signal means MORE trading
                    # opportunities, not a stricter combined bar: the
                    # original dip-buy signal, a Dual-Thrust-style
                    # opening-range breakout (the mirror case - a fresh
                    # push to a new high instead of a pullback), and a
                    # Heikin-Ashi confirmed bullish reversal candle.
                    and (
                        self.strategy.volatility_scalp_dip_signal(symbol, price)
                        or self.strategy.dual_thrust_breakout_signal(symbol, price)
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
                    ):
                        if not ok:
                            self.gate_rejections[reason] += 1
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
                ):
                    average_down_quantity = self.strategy.volatility_scalp_share_count(
                        price,
                        buying_power=buying_power,
                        intensity=volatility_scalp_intensity,
                    )
                    if average_down_quantity > 0 and price * Decimal(
                        average_down_quantity
                    ) * Decimal("1.03") > buying_power:
                        average_down_quantity = 0
                    if (
                        average_down_quantity > 0
                        and not self.volatility_scalp_position_value_ok(
                            quantity, average_down_quantity, price
                        )
                    ):
                        average_down_quantity = 0
                    if (
                        average_down_quantity > 0
                        and not self.volatility_scalp_total_exposure_ok(
                            positions, price * Decimal(average_down_quantity)
                        )
                    ):
                        average_down_quantity = 0
                    if average_down_quantity > 0:
                        order_id = self.place_stock_scaled(
                            symbol,
                            "BUY",
                            average_down_quantity,
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
                    # By request, after pre-market losses: "only trading
                    # established stocks with more volume and popularity
                    # in extended hours." Outside core hours, a fresh
                    # long entry only fires for the POPULAR bucket
                    # (already gated on popular_stock_min_volume/
                    # popular_stock_symbols - see TradingStrategy.
                    # select_stock_symbols) - PENNY and DISCOVERY names
                    # wait for core hours. Core hours are unaffected.
                    if not core_session_active and bucket != "POPULAR":
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
                    if not core_session_active and bucket != "POPULAR":
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

    def log_day_end_summary(self, moment: datetime) -> None:
        if self.last_day_end_log_date == moment.date():
            return
        self.last_day_end_log_date = moment.date()
        try:
            buying_power = self.api.buying_power()
            positions = [
                item
                for item in self.api.positions()
                if Decimal(str(item.get("quantity", "0"))) != 0
            ]
            log.info(
                "DAYEND | date=%s | buying_power=$%.2f | positions=%s | working_orders=%s | popular_research=%s",
                moment.date().isoformat(),
                buying_power,
                len(positions),
                len(self.working_orders),
                ",".join(
                    sorted(
                        self.seed_popular_symbols
                        | self.agent_popular_symbols
                    )
                )
                or "NONE",
            )
        except Exception as exc:
            log.error("DAYEND | date=%s | summary failed | %s", moment.date(), exc)

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
        batch, self.option_cursor = self.strategy.rotating_batch(
            self.option_contracts,
            self.option_cursor,
            self.config.option_batch_size,
        )
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
        # A fresh underlying quote per cycle, decoupled from whatever batch
        # the stock-scanning path happens to be covering this cycle - the
        # direction signal must never silently run on stale/absent state.
        # VIXY rides along in the same batched call (real VIX/CGIF index
        # data isn't reachable through the OpenAPI - confirmed live) to
        # track a market-wide volatility regime gate every cycle.
        underlyings = sorted({contract["underlying_symbol"] for contract in batch})
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
                    if days_to_expiration <= self.config.option_min_hold_dte:
                        continue
                    underlying = contract["underlying_symbol"]
                    direction = directions.get(underlying, "HOLD")
                    contract_type = contract.get("option_type")
                    if not (
                        (contract_type == "CALL" and direction == "CALL")
                        or (contract_type == "PUT" and direction == "PUT")
                    ):
                        continue
                    tick_score = self.strategy.tick_direction_score(
                        f"OPTU:{underlying}"
                    )
                    underlying_quote = underlying_quote_by_symbol.get(underlying)
                    obi_score = (
                        self.obi_score_for(underlying, "US_STOCK", underlying_quote)
                        if underlying_quote is not None
                        else None
                    )
                    if not self.strategy.option_entry_confirmed(
                        direction, tick_score, obi_score
                    ):
                        continue
                    if not self.strategy.option_delta_ok(
                        self.api.option_delta(quote)
                    ):
                        continue
                    if not self.strategy.option_iv_percentile_ok(
                        self.option_iv_history[option_symbol], current_iv
                    ):
                        continue
                    if not self.strategy.option_market_regime_ok(
                        self.vixy_history, current_vixy
                    ):
                        continue
                    blocked_until = self.wash_sales.blocked_until(underlying)
                    if blocked_until:
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
                        continue
                    if self.symbol_quarantined(key):
                        self.gate_rejections[
                            "symbol quarantined - recent net losses on this symbol"
                        ] += 1
                        continue
                    limit_price = self.api.option_limit_price(quote, "BUY")
                    buy_quantity, contract_cost = (
                        self.strategy.option_order_quantity(
                            limit_price,
                            buying_power,
                        )
                    )
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

    def _stall_equity_quotes(
        self,
        positions: list[dict],
        core_session_active: bool,
        stall_seconds: float,
        now: float,
    ) -> dict[str, dict]:
        """Batch-fetches quotes for every EQUITY position that clears the
        cheap stall-eligibility checks, instead of boost_stalled_positions
        calling api.stock_quote(symbol) one at a time inside its loop.
        stock_quote() with no category does its OWN per-symbol category
        lookup plus its own single-symbol quote fetch - two API calls
        each - so a held-position count in the teens meant dozens of
        sequential, individually rate-limited round trips blocking this
        entire single-threaded loop for minutes at a stretch, right when
        the per-symbol stall check (see boost_stalled_positions) started
        actually reaching this code instead of bailing out early.
        """
        candidates = []
        for position in positions:
            if position.get("instrument_type") != "EQUITY":
                continue
            quantity = Decimal(str(position.get("quantity", "0")))
            if quantity <= 0:
                continue
            if Decimal(str(position.get("cost_price") or "0")) <= 0:
                continue
            symbol = str(position.get("symbol", "")).upper()
            if symbol in self.pending_stock_exits:
                continue
            key = f"STOCK:{symbol}"
            if not self.cooldown_ready(key):
                continue
            if now - self.last_trade.get(key, 0.0) < stall_seconds:
                continue
            if self.is_fractional_quantity(quantity) and not core_session_active:
                continue
            candidates.append(symbol)
        if not candidates:
            return {}
        by_category: dict[str, list[str]] = defaultdict(list)
        for symbol in candidates:
            by_category[self.stock_categories.get(symbol, "US_STOCK")].append(symbol)
        quote_by_symbol: dict[str, dict] = {}
        for category, symbols in by_category.items():
            try:
                quotes, _ = self.api.stock_quotes_resilient(symbols, category)
            except Exception as exc:
                log.error(
                    "STALL  | batch quote fetch failed | %s | %s",
                    category,
                    exc,
                )
                continue
            for quote in quotes:
                quote_by_symbol[str(quote.get("symbol", "")).upper()] = quote
        return quote_by_symbol

    def _stall_exit_price(
        self,
        quote: dict,
        average_cost: Decimal,
        min_profit: Decimal,
        fee_per_share: Decimal,
        max_spread_percent: Decimal | None = None,
    ) -> Decimal | None:
        """Pick the best available green exit price for a stalled position.

        Prefers the bid (fills immediately) whenever it alone clears cost +
        min_profit + fee. If the bid doesn't clear but the ask (top of the
        spread) does, rest a passive limit there instead of giving up - a
        stalled position sitting inside the spread shouldn't be abandoned
        just because the aggressive/immediate price isn't green yet. Never
        prices below cost + min_profit + fee on either side, so this can
        only ever produce a genuinely profitable exit or no exit at all.

        max_spread_percent defaults to stock_entry_max_spread_percent (the
        stall-breaker's own long-standing bound, tuned for a quote-glitch
        on an otherwise normal, liquid stock - see the TBB incident
        below). Callers whose positions are deliberately choppy/wide-
        spread by their own selection criterion (the volatility-scalp
        cohort) should pass a wider bound explicitly - live incident:
        GAUZ routinely quoted 2-7% spreads (its normal character, not a
        glitch), so the 0.50% default meant the ask-fallback almost
        never fired, exits depended entirely on the bid alone clearing
        cost, and the strategy kept averaging into new dip-buys (a much
        looser bar) far faster than it could ever exit.
        """
        if max_spread_percent is None:
            max_spread_percent = self.config.stock_entry_max_spread_percent
        floor = average_cost + min_profit + fee_per_share
        bid = self.api.quote_bid(quote)
        if bid is not None:
            # By request: these smaller/cheaper stocks have real
            # sub-penny precision (a live quote showed bid=0.4592) -
            # quantizing to a flat cent throws away real value on
            # exactly the stocks where a cent is a meaningful fraction
            # of the price. See WebullAPI.price_tick_size.
            sell_price = bid.quantize(
                self.api.price_tick_size(bid), rounding=ROUND_DOWN
            )
            if sell_price >= floor:
                return sell_price
        ask = self.api.quote_ask(quote)
        if ask is not None:
            # A resting limit at the ask only has a realistic chance of
            # filling if the spread itself is reasonably tight - the same
            # bound entries are already held to (entry_spread_ok). On a
            # thin/illiquid name with an artificially wide quoted spread,
            # the ask can sit far above where the stock is actually
            # trading (live incident: TBB quoted bid=19.39/ask=19.89 while
            # prints were at 19.41) - resting there submits an order that
            # can never fill, times out, and gets resubmitted at the
            # identical unreachable price every stall cycle, forever.
            # Skip the fallback entirely in that case and wait for the
            # spread to normalize instead of spinning on a doomed order.
            if bid is not None and bid > 0:
                spread_percent = (ask - bid) / bid * 100
                if spread_percent > max_spread_percent:
                    return None
            sell_price = ask.quantize(
                self.api.price_tick_size(ask), rounding=ROUND_DOWN
            )
            # By request: "you cannot always go to the top of the spread
            # when it is big, you must ask a reasonable price, not too
            # far from the last [trade]." Even within max_spread_percent,
            # the raw ask can still sit meaningfully far from where the
            # stock is actually trading on a genuinely wide (not just
            # glitchy) spread - the exact TBB pattern above, just not
            # extreme enough to trip the spread-sanity skip entirely.
            # Caps the fallback at half the allowed spread's distance
            # above the last print instead of the literal ask, so a big
            # spread means "rest closer to reality," not "chase the far
            # edge of the book."
            try:
                last_price = self.api.quote_price(quote)
            except Exception:
                last_price = None
            if last_price and last_price > 0:
                reasonable_cap = last_price * (
                    Decimal("1") + max_spread_percent / Decimal("200")
                )
                sell_price = min(
                    sell_price,
                    reasonable_cap.quantize(
                        self.api.price_tick_size(reasonable_cap),
                        rounding=ROUND_DOWN,
                    ),
                )
            if sell_price >= floor:
                return sell_price
        return None

    def boost_stalled_positions(
        self,
        positions: list[dict],
        options_active: bool,
        core_session_active: bool = False,
    ) -> None:
        """Free capital stuck in a stalled position at breakeven-plus-a-penny.

        This is capital hygiene, not a turnover target: it never sells at a
        loss and only fires on a position whose OWN last order activity is
        stale, so a position isn't held indefinitely waiting on a stalled
        quote. Deliberately per-symbol, not one global "has anything filled
        recently" clock - an account that's generally active (new entries
        landing every minute or two) would otherwise never let this run at
        all, even though a specific older position has been sitting
        untouched the whole time.
        """
        if not self.config.stall_breaker_enabled:
            return
        now = time.monotonic()
        stall_seconds = float(self.config.stall_breaker_seconds)
        if now - self.last_stall_boost < stall_seconds:
            return
        self.last_stall_boost = now
        min_profit = self.config.stall_breaker_min_profit
        boosted = 0
        quote_by_symbol = self._stall_equity_quotes(positions, core_session_active, stall_seconds, now)
        for position in positions:
            quantity = Decimal(str(position.get("quantity", "0")))
            if quantity <= 0:
                continue
            average_cost = Decimal(str(position.get("cost_price") or "0"))
            if average_cost <= 0:
                continue
            symbol = str(position.get("symbol", "")).upper()
            instrument_type = position.get("instrument_type")
            try:
                if instrument_type == "EQUITY":
                    if symbol in self.pending_stock_exits:
                        continue
                    key = f"STOCK:{symbol}"
                    if not self.cooldown_ready(key):
                        continue
                    # This specific symbol's own last order activity, not
                    # whether anything else in the account recently
                    # filled - see the docstring above.
                    if now - self.last_trade.get(key, 0.0) < stall_seconds:
                        continue
                    # Same fractional/core-hours constraint as trade_stocks'
                    # exits - Webull rejects any order on a non-integer
                    # quantity outside core hours, so don't bother trying.
                    if (
                        self.is_fractional_quantity(quantity)
                        and not core_session_active
                    ):
                        continue
                    quote = quote_by_symbol.get(symbol)
                    if quote is None:
                        continue
                    fee_per_share = self.config.sell_fee_dollars / quantity
                    sell_price = self._stall_exit_price(
                        quote, average_cost, min_profit, fee_per_share
                    )
                    if sell_price is None:
                        continue
                    # Same $0.10-$0.999 lot-restricted-band rejection as
                    # trade_stocks' exits - Webull rejects any order under
                    # 100 shares while price sits in that band, regardless
                    # of side or how many shares are actually held.
                    if self.strategy.exit_blocked_by_lot_restriction(quantity, sell_price):
                        continue
                    order_id = self.api.place_stock(
                        symbol,
                        "SELL",
                        quantity,
                        limit_price=sell_price,
                        fractional=quantity != quantity.to_integral_value(),
                    )
                    self.pending_stock_exits.add(symbol)
                    pnl = self.record_realized_exit(average_cost, sell_price, quantity)
                    self.record_trade(
                        key, order_id, "PROFIT", sell_price, pnl=pnl,
                        entry_price=average_cost, quantity=quantity,
                    )
                    boosted += 1
                elif instrument_type == "OPTION" and options_active:
                    if symbol in self.pending_option_exits:
                        continue
                    key = f"OPTION:{symbol}"
                    if not self.cooldown_ready(key):
                        continue
                    if now - self.last_trade.get(key, 0.0) < stall_seconds:
                        continue
                    contract = self.api.contract_from_position(position)
                    if not contract:
                        continue
                    fee_per_share = self.config.sell_fee_dollars / (quantity * 100)
                    quote = self.api.option_quote(contract["symbol"])
                    sell_price = self._stall_exit_price(
                        quote, average_cost, min_profit, fee_per_share
                    )
                    if sell_price is None:
                        continue
                    order_id = self.api.place_option(
                        contract,
                        "SELL",
                        quantity,
                        sell_price,
                        "SELL_TO_CLOSE",
                    )
                    self.pending_option_exits.add(symbol)
                    pnl = self.record_realized_exit(average_cost, sell_price, quantity, multiplier=100)
                    self.record_trade(
                        key, order_id, "PROFIT", sell_price, pnl=pnl,
                        entry_price=average_cost, quantity=quantity,
                    )
                    boosted += 1
            except Exception as exc:
                if isinstance(exc, QuoteUnavailableError):
                    continue
                log.error("STALL  | %s | %s", symbol, exc)
        if boosted:
            self.last_account_refresh = 0.0
        log.info(
            "STALL  | checked %s position(s) idle %ss+ | boosted %s "
            "profitable exit(s)",
            len(quote_by_symbol),
            self.config.stall_breaker_seconds,
            boosted,
        )

    def write_status_snapshot(
        self,
        positions: list[dict],
        buying_power: Decimal,
        paused: bool,
    ) -> None:
        if time.monotonic() - self.last_status_write < float(self.config.poll_seconds):
            return
        self.last_status_write = time.monotonic()
        position_rows = []
        for position in positions:
            quantity = Decimal(str(position.get("quantity", "0")))
            if quantity == 0:
                continue
            symbol = str(position.get("symbol", "")).upper()
            # Live incident (this bug, caught from a real UI report): the
            # displayed last_price used to come from self.strategy.prices
            # (the bot's own scan-cycle quote cache), while the P&L
            # figures shown right next to it (position_unrealized_pnl/
            # position_day_pnl) prefer the BROKER's own reported
            # last_price/market_price field on the position itself when
            # present - two independently-refreshed sources on different
            # cadences, so the displayed price and the P&L shown beside
            # it could silently disagree (a live example: KNRX showed
            # last_price=0.392, but that price doesn't reconcile with
            # the unrealized_pnl shown alongside it). Prefer the SAME
            # broker-native fields the P&L math already uses, falling
            # back to strategy.prices only when the broker hasn't
            # reported one - makes price and P&L internally consistent.
            last_price = (
                position.get("last_price")
                or position.get("market_price")
                or self.strategy.prices.get(symbol)
                or position.get("cost_price", "0")
            )
            position_rows.append(
                {
                    "symbol": symbol,
                    "instrument_type": position.get("instrument_type"),
                    "quantity": str(quantity),
                    "cost_price": str(position.get("cost_price", "0")),
                    "last_price": str(last_price),
                    "unrealized_pnl": str(self.strategy.position_unrealized_pnl(position)),
                    "day_pnl": str(self.strategy.position_day_pnl(position)),
                    "bucket": self.position_buckets.get(symbol, "DISCOVERY"),
                }
            )
        held_symbols = {row["symbol"] for row in position_rows}
        watchlist_rows = [
            {
                "symbol": symbol,
                "price": str(self.strategy.prices.get(symbol, "0")),
                "bucket": self.strategy.selection_bucket(symbol),
                "has_position": symbol in held_symbols,
                **self.strategy.metrics.get(symbol, {}),
            }
            for symbol in sorted(self.user_watchlist)
        ]
        agent_summary = None
        if self.market_agent:
            agent_summary = {
                "enabled": True,
                "market_pulse": self.market_pulse_cache,
                "popular_symbols": sorted(self.agent_popular_symbols),
                "strategy_review": self.market_agent.strategy_review(),
            }
        day_pnl_total = sum(
            (Decimal(row["day_pnl"]) for row in position_rows),
            Decimal("0"),
        )
        now = time.monotonic()
        if now - self.last_balance_history_write >= 20:
            self.last_balance_history_write = now
            # Signed quantity (negative for shorts) makes this the same
            # cash + market-value equity formula for both directions - a
            # short's proceeds already sit in buying_power, and its
            # negative position value nets out the buy-back liability.
            # cached_raw_buying_power (not the buying_power parameter,
            # which is reserved-down for trading sizing) so this doesn't
            # understate real equity by MIN_CASH_RESERVE_DOLLARS.
            total_equity = self.cached_raw_buying_power + sum(
                (
                    Decimal(row["quantity"]) * Decimal(row["last_price"])
                    for row in position_rows
                ),
                Decimal("0"),
            )
            self.status.record_balance(total_equity)
        with _working_orders_lock(self):
            working_orders_snapshot = list(self.working_orders.items())
        pending_order_rows = [
            {
                "order_id": order_id,
                "instrument_type": order.get("key", "?:?").split(":", 1)[0],
                "symbol": order.get("key", "?:?").split(":", 1)[-1],
                "action": order.get("action"),
                "quantity": (
                    str(order["quantity"]) if order.get("quantity") is not None else None
                ),
                "limit_price": (
                    str(order["limit_price"])
                    if order.get("limit_price") is not None
                    else None
                ),
                "age_seconds": round(now - float(order.get("submitted_at", now))),
                "cancel_requested": order.get("cancel_requested_at") is not None,
            }
            for order_id, order in working_orders_snapshot
        ]
        self.status.write(
            mode=self.config.mode,
            # Real, un-reserved buying power (what Webull's own app
            # shows), not the buying_power parameter this function also
            # received - that one is reserved down by
            # MIN_CASH_RESERVE_DOLLARS for trading sizing and showing it
            # here reads as a silent gap against the account's real cash.
            buying_power=self.cached_raw_buying_power,
            positions=position_rows,
            watchlist=watchlist_rows,
            agent_summary=agent_summary,
            paused=paused,
            stock_count=len(self.stock_symbols),
            option_count=len(self.option_contracts),
            realized_pnl_today=self.daily_realized_pnl,
            open_pnl_total=day_pnl_total,
            account_day_pnl_total=self.cached_account_day_pnl,
            account_value=self.cached_account_value,
            user_watchlist=sorted(self.user_watchlist),
            pending_orders=pending_order_rows,
        )

    def overnight_hold_symbols(self) -> set[str]:
        """Symbols whose bucket is eligible to carry a position past
        EOD_CLOSE_TIME instead of always flattening. Pairs positions are
        excluded - that strategy is intraday-only by design - so only the
        core EMA/OBI stock strategy's own positions (plus manual buys)
        ever ride overnight.
        """
        if not OVERNIGHT_HOLD_ENABLED:
            return set()
        return {
            symbol
            for symbol, bucket in self.position_buckets.items()
            if bucket not in ALWAYS_FLATTEN_BUCKETS
            and symbol not in self.short_symbols
        }

    def close_fractional_positions_before_core_close(self) -> None:
        """Fractional orders only work during core hours - once core
        session ends, a fractional position can't be bought, sold,
        stopped out, or profit-taken at all until the next session opens.
        Unlike a whole-share position (which OVERNIGHT_HOLD_ENABLED lets
        ride deliberately, still exitable pre/after-hours if needed), a
        fractional position caught past this boundary has zero downside
        protection for the rest of the day/overnight - overnight_hold_
        symbols() doesn't know about quantity at all, so a fractional
        position in an otherwise overnight-eligible bucket (POPULAR/
        PENNY/DISCOVERY) would silently ride along with no way to defend
        it.

        Only closes the ones currently sitting at a profit - locking in a
        gain before it becomes undefendable is the whole point, but a
        loser isn't forced out just because the window is closing (it's
        already undefendable either way, and forcing a realized loss here
        isn't necessary the way capturing a gain is). Called from the same
        option_closeout-to-option_close window the option EOD closeout
        already uses.
        """
        now = time.monotonic()
        if now - self.last_fractional_sweep < self.config.eod_retry_seconds:
            return
        self.last_fractional_sweep = now
        try:
            positions = self.api.positions()
        except Exception as exc:
            log.error("CLOSE  | fractional pre-close sweep failed | %s", exc)
            return
        fractional_positions = [
            item
            for item in positions
            if item.get("instrument_type") == "EQUITY"
            and self.is_fractional_quantity(Decimal(str(item.get("quantity", "0"))))
        ]
        if not fractional_positions:
            return
        profitable_symbols: set[str] = set()
        for item in fractional_positions:
            symbol = str(item.get("symbol", "")).upper()
            cost = Decimal(str(item.get("cost_price") or "0"))
            if cost <= 0:
                continue
            try:
                price = self.api.quote_price(self.api.stock_quote(symbol))
            except Exception as exc:
                log.warning(
                    "CLOSE  | fractional pre-close sweep | %-8s | quote "
                    "failed, skipping this cycle | %s",
                    symbol,
                    exc,
                )
                continue
            if price > cost:
                profitable_symbols.add(symbol)
        if not profitable_symbols:
            return
        exclude_symbols = {
            str(item.get("symbol", "")).upper()
            for item in positions
            if item.get("instrument_type") == "EQUITY"
        } - profitable_symbols
        try:
            submitted = self.api.close_all_positions(
                {"EQUITY"},
                loss_callback=self.wash_sales.block,
                exclude_symbols=exclude_symbols,
            )
        except Exception as exc:
            log.error("CLOSE  | fractional pre-close sweep failed | %s", exc)
            return
        self.pending_stock_exits -= profitable_symbols
        log.info(
            "CLOSE  | fractional pre-core-close sweep | submitted=%s | %s",
            len(submitted),
            ",".join(sorted(profitable_symbols)),
        )

    def close_profitable_positions_during_extended_hours(self) -> None:
        """By request, after pre-market losses: "capturing any profits
        to close out the day as much as possible" outside core hours -
        proactively closes any equity position currently sitting at a
        profit during extended hours (pre-market or after-hours),
        instead of waiting for its normal PROFIT target or letting it
        ride toward an overnight hold. Only ever fires outside core
        hours (see the call site's core_session_active check) and only
        ever closes a confirmed GAIN - same reasoning as
        close_fractional_positions_before_core_close: locking in a
        profit before the session gets even thinner is the point,
        forcing a realized loss isn't.
        """
        now = time.monotonic()
        if (
            now - self.last_extended_hours_profit_sweep
            < float(self.config.extended_hours_profit_sweep_seconds)
        ):
            return
        self.last_extended_hours_profit_sweep = now
        try:
            positions = self.api.positions()
        except Exception as exc:
            log.error("CLOSE  | extended-hours profit sweep failed | %s", exc)
            return
        equity_positions = [
            item
            for item in positions
            if item.get("instrument_type") == "EQUITY"
            and Decimal(str(item.get("quantity", "0"))) != 0
        ]
        if not equity_positions:
            return
        profitable_symbols: set[str] = set()
        for item in equity_positions:
            symbol = str(item.get("symbol", "")).upper()
            cost = Decimal(str(item.get("cost_price") or "0"))
            if cost <= 0:
                continue
            try:
                price = self.api.quote_price(self.api.stock_quote(symbol))
            except Exception as exc:
                log.warning(
                    "CLOSE  | extended-hours profit sweep | %-8s | quote "
                    "failed, skipping this cycle | %s",
                    symbol,
                    exc,
                )
                continue
            if price > cost:
                profitable_symbols.add(symbol)
        if not profitable_symbols:
            return
        exclude_symbols = {
            str(item.get("symbol", "")).upper() for item in equity_positions
        } - profitable_symbols
        try:
            submitted = self.api.close_all_positions(
                {"EQUITY"},
                loss_callback=self.wash_sales.block,
                exclude_symbols=exclude_symbols,
            )
        except Exception as exc:
            log.error("CLOSE  | extended-hours profit sweep failed | %s", exc)
            return
        self.pending_stock_exits -= profitable_symbols
        log.info(
            "CLOSE  | extended-hours profit sweep | submitted=%s | %s",
            len(submitted),
            ",".join(sorted(profitable_symbols)),
        )

    def close_instruments(
        self,
        instrument_types: set[str],
        apply_overnight_hold: bool = False,
    ) -> bool:
        now = time.monotonic()
        if now - self.last_close_attempt < self.config.eod_retry_seconds:
            return False
        self.last_close_attempt = now
        held_overnight = (
            self.overnight_hold_symbols() if apply_overnight_hold else set()
        )
        try:
            submitted = self.api.close_all_positions(
                instrument_types,
                loss_callback=self.wash_sales.block,
                exclude_symbols=held_overnight,
            )
            self.pending_stock_exits.clear()
            self.pending_option_exits.clear()
            remaining = [
                item
                for item in self.api.positions()
                if item.get("instrument_type") in instrument_types
                if Decimal(str(item.get("quantity", "0"))) != 0
                if str(item.get("symbol", "")).upper() not in held_overnight
            ]
            log.info(
                "CLOSE  | submitted=%s | remaining=%s%s",
                len(submitted),
                len(remaining),
                f" | held overnight={len(held_overnight)}" if held_overnight else "",
            )
            return not remaining
        except Exception as exc:
            log.error("CLOSE  | failed | %s", exc)
            return False

    def process_ui_commands(
        self,
        positions: list[dict],
        buying_power: Decimal = Decimal("0"),
        core_session_active: bool = False,
    ) -> Decimal:
        """Executes dashboard-initiated actions (close all, sell one
        position, buy one symbol, cancel one pending order, add a watchlist
        symbol). The dashboard has no Webull credentials or API access of
        its own - it can only enqueue a request, which is executed here
        through the same order-placement, wash-sale, and position-tracking
        code every other entry/exit uses. Runs before the circuit-breaker
        gate so a manual risk-reducing action (Sell, Cancel, Close All) is
        never blocked by a paused/halted state - a manual Buy still is,
        naturally, since handle_portfolio_circuit_breaker/handle_daily_loss_
        breaker only gate the automatic entry paths that run after this.
        """
        try:
            commands = self.commands.pop_all()
        except Exception as exc:
            log.error("CMD    | queue read failed | %s", exc)
            return buying_power
        for command in commands:
            command_type = command.get("type")
            try:
                if command_type == "close_all":
                    self.close_instruments({"EQUITY", "OPTION"})
                    log.warning("CMD    | manual close-all executed from dashboard")
                elif command_type == "sell":
                    self._manual_sell(command, positions, core_session_active)
                elif command_type == "buy":
                    buying_power = self._manual_buy(
                        command, positions, buying_power, core_session_active
                    )
                elif command_type == "watchlist_add":
                    self.add_to_watchlist(command.get("symbol", ""))
                elif command_type == "cancel_order":
                    self._manual_cancel_order(command)
                else:
                    log.warning("CMD    | unknown command type=%s", command_type)
            except Exception as exc:
                log.error("CMD    | %s failed | %s", command_type, exc)
        return buying_power

    def _manual_cancel_order(self, command: dict) -> None:
        order_id = str(command.get("order_id", "")).strip()
        if not order_id:
            return
        order = self.working_orders.get(order_id)
        if not order:
            log.info(
                "CMD    | cancel skipped | id=%s | no longer a tracked working order",
                order_id,
            )
            return
        if order.get("cancel_requested_at") is not None:
            log.info(
                "CMD    | cancel skipped | id=%s | already cancel-requested",
                order_id,
            )
            return
        try:
            self.api.cancel(order_id)
        except Exception as exc:
            log.error("CMD    | cancel failed | id=%s | %s", order_id, exc)
            return
        order["cancel_requested_at"] = time.monotonic()
        # Reconciliation (releasing pending_stock_exits/pending_option_exits
        # for a cancelled STOP/PROFIT, dropping it from working_orders) is
        # handled the next time monitor_working_orders sees the order has
        # actually disappeared from the broker's open-order list - the same
        # path an automatic timeout cancel already goes through, so a
        # manual cancel doesn't need its own separate cleanup logic.
        log.warning(
            "CMD    | manual cancel requested from dashboard | %s | id=%s",
            order.get("key", "?"),
            order_id,
        )

    def _manual_sell(
        self,
        command: dict,
        positions: list[dict],
        core_session_active: bool = False,
    ) -> None:
        symbol = str(command.get("symbol", "")).upper()
        instrument_type = command.get("instrument_type", "EQUITY")
        if not symbol:
            return
        position = next(
            (
                item
                for item in positions
                if item.get("instrument_type") == instrument_type
                and str(item.get("symbol", "")).upper() == symbol
            ),
            None,
        )
        if not position:
            log.info(
                "CMD    | manual sell skipped | %-8s | no matching open position",
                symbol,
            )
            return
        quantity = Decimal(str(position.get("quantity", "0")))
        cost = Decimal(str(position.get("cost_price", "0")))
        if quantity <= 0:
            return
        if instrument_type == "EQUITY":
            if symbol in self.pending_stock_exits:
                log.info(
                    "CMD    | manual sell skipped | %-8s | exit already pending",
                    symbol,
                )
                return
            is_fractional = self.is_fractional_quantity(quantity)
            if is_fractional and not core_session_active:
                log.info(
                    "CMD    | manual sell skipped | %-8s | fractional "
                    "position, Webull only allows an order on it during "
                    "core hours",
                    symbol,
                )
                return
            quote = self.api.stock_quote(symbol)
            # A manual sell is an urgent "get me out" click, not a patient
            # resting order - the old below-bid crossing price
            # (stock_limit_price's SELL side) shaved off an extra
            # STOCK_LIMIT_OFFSET on top of the spread, which could tip an
            # otherwise-flat or barely-profitable exit into a recorded
            # loss for no real reason. Price it at the ask (top of the
            # spread) instead, and place a genuine MARKET order whenever
            # one is actually usable (whole shares, core hours, account
            # allows fractional/MARKET orders) so it's not left resting
            # unfilled either.
            sell_price = self.api.quote_ask(quote) or self.api.stock_limit_price(
                quote, "SELL"
            )
            use_market = (
                core_session_active
                and not is_fractional
                and self.fractional_trading_enabled
            )
            order_id = self.api.place_stock(
                symbol,
                "SELL",
                quantity,
                limit_price=None if use_market else sell_price,
                fractional=is_fractional,
                market=use_market,
            )
            self.pending_stock_exits.add(symbol)
            pnl = self.record_realized_exit(cost, sell_price, quantity)
            self.record_trade(
                f"STOCK:{symbol}", order_id, "MANUAL_SELL", sell_price, pnl=pnl,
                entry_price=cost, quantity=quantity,
            )
            if pnl < 0:
                self.wash_sales.block(symbol, "manual sell at a loss")
        elif instrument_type == "OPTION":
            if symbol in self.pending_option_exits:
                log.info(
                    "CMD    | manual sell skipped | %-8s | exit already pending",
                    symbol,
                )
                return
            contract = self.api.contract_from_position(position)
            if not contract:
                log.error(
                    "CMD    | manual sell failed | %-8s | could not resolve option contract",
                    symbol,
                )
                return
            quote = self.api.option_quote(contract["symbol"])
            sell_price = self.api.quote_ask(quote) or self.api.option_limit_price(
                quote, "SELL"
            )
            order_id = self.api.place_option(
                contract,
                "SELL",
                quantity,
                sell_price,
                "SELL_TO_CLOSE",
            )
            self.pending_option_exits.add(symbol)
            pnl = self.record_realized_exit(cost, sell_price, quantity, multiplier=100)
            self.record_trade(
                f"OPTION:{symbol}", order_id, "MANUAL_SELL", sell_price, pnl=pnl,
                entry_price=cost, quantity=quantity,
            )
            if pnl < 0:
                self.wash_sales.block(
                    contract["underlying_symbol"],
                    "manual option sell at a loss",
                )
        else:
            return
        log.warning(
            "CMD    | manual sell executed | %-8s (%s) | qty=%s",
            symbol,
            instrument_type,
            quantity,
        )

    def _manual_buy(
        self,
        command: dict,
        positions: list[dict],
        buying_power: Decimal,
        core_session_active: bool = False,
    ) -> Decimal:
        """Stocks only for now, mirroring the same entry sizing/pricing the
        automatic strategy uses (dollar-sized during core hours, fixed
        STOCK_QUANTITY sizing otherwise) rather than a separate ad-hoc
        path, so a manual buy still respects the account's normal risk
        limits (MAX_ORDER_NOTIONAL, the $0.10-$0.999 lot rule, etc).
        """
        symbol = str(command.get("symbol", "")).upper()
        if not symbol:
            return buying_power
        if symbol in self.broker_conflict_symbols:
            log.info(
                "CMD    | manual buy skipped | %-8s | broker conflict blacklisted",
                symbol,
            )
            return buying_power
        quantity, _cost = self.api.stock_position(symbol, positions)
        if quantity > 0:
            log.info(
                "CMD    | manual buy skipped | %-8s | already holding a position",
                symbol,
            )
            return buying_power
        blocked_until = self.wash_sales.blocked_until(symbol)
        if blocked_until:
            log.info(
                "CMD    | manual buy skipped | %-8s | wash-sale blocked until %s",
                symbol,
                blocked_until.strftime("%Y-%m-%d"),
            )
            return buying_power
        if self.strategy.open_position_count(positions) >= self.config.max_open_positions:
            log.info(
                "CMD    | manual buy skipped | %-8s | at MAX_OPEN_POSITIONS",
                symbol,
            )
            return buying_power
        try:
            quote = self.api.stock_quote(symbol)
            price = self.api.quote_price(quote)
        except Exception as exc:
            log.error("CMD    | manual buy failed | %-8s | %s", symbol, exc)
            return buying_power
        self.strategy.update_stock_snapshot(quote, price)
        fractional = False
        if (
            core_session_active
            and self.fractional_trading_enabled
            and self.config.stock_core_session_position_fraction > 0
        ):
            target_notional = min(
                buying_power * self.config.stock_core_session_position_fraction,
                buying_power,
                self.config.max_order_notional,
            )
            buy_quantity, buffered_price = self.strategy.dollar_stock_quantity(
                price, target_notional
            )
            fractional = buy_quantity > 0
        else:
            buy_quantity, buffered_price = self.strategy.stock_order_quantity(
                price, buying_power
            )
            if (
                buy_quantity == 0
                and self.config.fractional_shares_enabled
                and core_session_active
                and self.fractional_trading_enabled
            ):
                fractional_quantity = self.strategy.fractional_stock_quantity(
                    price, buying_power
                )
                if fractional_quantity > 0:
                    buy_quantity = fractional_quantity
                    buffered_price = price * Decimal("1.03")
                    fractional = True
        if buy_quantity <= 0:
            log.info(
                "CMD    | manual buy skipped | %-8s | no affordable quantity",
                symbol,
            )
            return buying_power
        entry_price = self.api.stock_limit_price(quote, "BUY")
        try:
            order_id = self.api.place_stock(
                symbol,
                "BUY",
                buy_quantity,
                limit_price=entry_price,
                fractional=fractional,
            )
        except Exception as exc:
            if self.is_fractional_trading_not_enabled(exc):
                self.handle_fractional_trading_not_enabled(exc)
            elif self.is_fractional_ticker_unsupported(exc):
                self.handle_fractional_ticker_unsupported(symbol, exc)
            else:
                log.error("CMD    | manual buy failed | %-8s | %s", symbol, exc)
            return buying_power
        self.record_trade(
            f"STOCK:{symbol}",
            order_id,
            "MANUAL_BUY",
            entry_price=entry_price,
            quantity=buy_quantity,
        )
        self.position_buckets[symbol] = "MANUAL"
        log.warning(
            "CMD    | manual buy executed | %-8s | qty=%s",
            symbol,
            buy_quantity,
        )
        return max(Decimal("0"), buying_power - buffered_price * buy_quantity)

    def add_to_watchlist(self, symbol: str) -> None:
        symbol = str(symbol).upper().strip()
        if not symbol:
            return
        self.user_watchlist.add(symbol)
        if symbol not in self.stock_categories:
            try:
                categories = self.api.stock_categories([symbol])
            except Exception as exc:
                log.error(
                    "CMD    | watchlist category lookup failed | %-8s | %s",
                    symbol,
                    exc,
                )
                categories = {}
            self.stock_categories[symbol] = categories.get(symbol, "US_STOCK")
        if symbol not in self.stock_symbols:
            self.stock_symbols.append(symbol)
        self.priority_scan_symbols.add(symbol)
        log.warning("CMD    | added %-8s to watchlist from dashboard", symbol)

    def _position_protection_loop(self) -> None:
        """Runs fill/cancel detection, exit repricing, and stop-loss
        escalation on their OWN cadence (poll_seconds, default 0.25s),
        independent of the main loop's much slower full-universe-scan
        cadence (SCAN cycles observed 30-90s+ live). By request:
        "held positions should be checked every 0.25s separately, the
        rest of the scan can take its own time" - live evidence (CHOW)
        showed a stuck PROFIT order sit unrefreshed far longer than
        intended because monitor_working_orders/the repricers/
        escalate_stalled_stop_losses previously ran inline in the same
        single-threaded loop body as trade_stocks' slow, batched
        universe scan, inheriting its cadence instead of the real
        poll_seconds target.

        Runs as a daemon thread (see run(), which starts this once and
        removes these same calls from its own sequential body so they
        never run twice concurrently). self.cached_positions and
        self.cached_core_session_active are read-only snapshots here,
        refreshed by the main thread each cycle - a single attribute
        read is safe under the GIL without its own lock, same
        "atomic reassignment" convention already used for
        stock_symbols/stock_categories in resolve_targets. Everything
        that actually touches self.working_orders (and reads/writes it
        from the main thread's record_trade for fresh entries) goes
        through _working_orders_lock/​_rekey_working_order instead.
        """
        while True:
            started = time.monotonic()
            try:
                self.monitor_working_orders()
                self.reprice_resting_exits(
                    self.cached_positions, self.cached_core_session_active
                )
                self.reprice_volatility_scalp_exits(
                    self.cached_positions, self.cached_core_session_active
                )
                self.reprice_volatility_scalp_entries()
                self.reprice_resting_entries(self.cached_core_session_active)
                self.escalate_stalled_stop_losses()
            except Exception as exc:
                log.error("PROTECT| position-protection cycle failed | %s", exc)
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, float(self.config.poll_seconds) - elapsed))

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
                    buying_power = self.trade_pairs(positions, buying_power)
                    buying_power = self.trade_stocks(
                        positions,
                        buying_power,
                        opening_grace_active,
                        core_session_active,
                    )
                    if option_open <= moment < option_closeout:
                        self.discover_option_contracts()
                        buying_power = self.trade_options(positions, buying_power)
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
