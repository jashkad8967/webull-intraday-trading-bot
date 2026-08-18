import logging
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from zoneinfo import ZoneInfo

from rich.logging import RichHandler

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
        self.last_trade: dict[str, float] = {}
        self.last_exit_at: dict[str, float] = {}
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
        self.stock_cursor = 0
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
        self.last_close_attempt = 0.0
        self.last_fractional_sweep = 0.0
        self.last_status_log = 0.0
        self.opening_grace_logged_date = None
        self.last_option_discovery = 0.0
        self.last_account_refresh = 0.0
        self.last_order_monitor = 0.0
        self.last_reprice = 0.0
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
        self.cached_positions: list[dict] = []
        self.working_orders: dict[str, dict] = {}
        self.agent_candidates: dict[str, dict] = {}
        self.entries_paused = False
        self.circuit_breaker_time = 0.0
        self.last_circuit_research = 0.0
        self.last_day_end_log_date = None
        self.seed_popular_symbols: set[str] = set()
        self.agent_popular_symbols: set[str] = set()
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
        self.daily_realized_loss = self.daily_pnl.realized_loss
        self.daily_realized_pnl = self.daily_pnl.realized_pnl
        self.daily_loss_breaker_triggered = False
        self.commands = CommandQueue(self.config.command_file)
        self.user_watchlist: set[str] = set(self.config.default_watchlist())
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

    def resolve_targets(self, moment: datetime) -> None:
        if self.resolved_date == moment.date():
            return
        requested_stocks = self.config.stocks()
        if requested_stocks == ["ALL"]:
            limit = self.config.stock_universe_limit()
            pool = self.config.stock_universe_pool()
            log.info(
                "LOAD   | downloading stocks and ETFs | limit=%s | pool=%s",
                limit,
                pool,
            )
            self.stock_categories = self.api.stock_universe(
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
                if (
                    symbol not in self.stock_categories
                    and symbol in preferred_categories
                ):
                    self.stock_categories[symbol] = preferred_categories[symbol]
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
                    if symbol not in self.stock_categories:
                        self.stock_categories[symbol] = "US_STOCK"
                        gainers_added += 1
                if gainers_added:
                    log.info(
                        "LOAD   | added %s top-gainer symbols outside directory cap",
                        gainers_added,
                    )
            if self.config.exclude_etfs:
                etfs = [
                    symbol
                    for symbol, category in self.stock_categories.items()
                    if category == "US_ETF"
                ]
                for symbol in etfs:
                    self.stock_categories.pop(symbol, None)
                if etfs:
                    log.info("LOAD   | excluded %s ETFs", len(etfs))
            for symbol in self.invalid_symbols.symbols:
                self.stock_categories.pop(symbol, None)
            eligible = [
                symbol
                for symbol in self.stock_categories
                if symbol not in self.invalid_symbols
            ]
            eligible = self.filter_with_popular_reinstated(eligible)
            self.stock_symbols = eligible[:limit]
            self.reserve_symbols = eligible[limit:]
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

    def reentry_cooldown_ready(self, key: str) -> bool:
        elapsed = time.monotonic() - self.last_exit_at.get(key, float("-inf"))
        return elapsed >= float(self.config.stock_reentry_cooldown_seconds)

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
    ) -> None:
        submitted_at = time.monotonic()
        self.last_trade[key] = submitted_at
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
        if action in ("BUY", "SHORT", "MANUAL_BUY"):
            # Resets the idle-cash gate-relaxation ramp (see
            # idle_cash_ramp_progress) - capital just got deployed, so
            # quality gates snap back to their normal strictness until
            # cash sits idle above MIN_CASH_RESERVE_DOLLARS again.
            self.last_capital_deployed_at = submitted_at
        if action in ("BUY", "SHORT"):
            # Feeds TradingStrategy.adaptive_stop_percent's time-aware
            # widen window - see position_opened_at.
            self.position_opened_at[key] = submitted_at
        self.trade_times[key].append(submitted_at)
        self.working_orders[order_id] = {
            "submitted_at": submitted_at,
            "key": key,
            "action": action,
            "cancel_requested_at": None,
            "limit_price": limit_price,
            "pnl": pnl,
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

        for order_id in open_ids:
            if order_id not in self.working_orders:
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

        for order_id, order in list(self.working_orders.items()):
            if order_id not in open_ids:
                self._release_pending_order(order)
                del self.working_orders[order_id]
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
                self.api.cancel(order_id)
                order["cancel_requested_at"] = now
                log.warning(
                    "CANCEL | unfilled after %ss | id=%s",
                    self.config.order_timeout_seconds,
                    order_id,
                )
            except Exception as exc:
                log.error("CANCEL | id=%s | %s", order_id, exc)

    def reprice_resting_exits(
        self, positions: list[dict], core_session_active: bool = False
    ) -> None:
        """Continuously re-quote a resting stock PROFIT sell order to track
        the current ask - the top of the spread - for as long as it stays
        unfilled and unescalated ("keep modifying to stay in the spread
        until sold"). Once a symbol is escalated, this stops chasing the ask
        for it and leaves resubmission to the normal escalation path.

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
        for order_id, order in list(self.working_orders.items()):
            action = order.get("action")
            key = str(order.get("key") or "")
            if action != "PROFIT" or not key.startswith("STOCK:"):
                continue
            if order.get("cancel_requested_at") is not None:
                continue
            symbol = key.split(":", 1)[1]
            if symbol in self.stop_loss_escalated:
                continue
            try:
                quote = self.api.stock_quote(symbol)
                ask = self.api.quote_ask(quote)
                if ask is None or ask == order.get("limit_price"):
                    continue
                quantity, cost = self.api.stock_position(symbol, positions)
                if quantity <= 0:
                    continue
                # Same fractional/core-hours constraint as trade_stocks'
                # PROFIT exit: cancel-and-replace can't succeed on a
                # fractional quantity outside core hours either, so leave
                # the existing resting order alone rather than cancelling
                # it for a replacement that will just get rejected.
                if self.is_fractional_quantity(quantity) and not core_session_active:
                    continue
                if cost > 0 and ask < cost:
                    # Never chase the ask down below entry cost - the
                    # existing resting order was already validly priced at
                    # or above the profit target when submitted; repricing
                    # to a falling ask here could reprice a profit-take
                    # into a loss. Leave it resting and let escalation (or
                    # the ask recovering) handle it instead.
                    continue
                self.api.cancel(order_id)
                new_order_id = self.api.place_stock(
                    symbol,
                    "SELL",
                    quantity,
                    limit_price=ask,
                )
                self.working_orders.pop(order_id, None)
                self.working_orders[new_order_id] = {
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
                }
                log.info(
                    "REPRICE| %-8s | %-6s | ask=%s | id=%s",
                    symbol,
                    action,
                    ask,
                    new_order_id,
                )
            except Exception as exc:
                log.error("REPRICE| %s | %s", symbol, exc)

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
            self.cached_buying_power = max(
                Decimal("0"),
                self.api.buying_power() - self.config.min_cash_reserve_dollars,
            )
            self.cached_positions = self.api.positions()
            self.last_account_refresh = now
        return self.cached_buying_power, [dict(item) for item in self.cached_positions]

    def idle_cash_ramp_progress(self, buying_power: Decimal) -> Decimal:
        """0..1 - how far along the idle-cash gate-relaxation ramp the bot
        currently is. Keeping buying_power (already net of
        MIN_CASH_RESERVE_DOLLARS) deployed outranks entry quality, so the
        longer it sits unspent, the more entry_spread_ok/entry_extension_ok/
        vwap_supports_entry/tick_direction_ok loosen - see their
        idle_relaxation_multiplier parameter. Resets to 0 the moment
        record_trade() sees a new BUY/SHORT/MANUAL_BUY fill.
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
        if not self.market_agent:
            return None
        return self.market_agent.assessment(symbol)

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

    def submit_agent_research(
        self,
        positions: list[dict],
        buying_power: Decimal,
        force: bool = False,
        event: str = "ROUTINE_RESEARCH",
    ) -> None:
        if not self.market_agent:
            return
        self.refresh_market_pulse()
        research_limit = min(self.config.agent_max_symbols, 10)
        held = [
            {
                "symbol": str(item.get("symbol", "")).upper(),
                "type": item.get("instrument_type"),
                "qty": self._compact_number(item.get("quantity")),
                "pnl": self._compact_number(
                    self.strategy.position_unrealized_pnl(item), 2
                ),
            }
            for item in positions
            if Decimal(str(item.get("quantity", "0"))) != 0
        ][:research_limit]
        candidate_limit = max(0, research_limit - len(held))
        candidates = list(self.agent_candidates.values())[
            :candidate_limit
        ]
        self.agent_candidates.clear()
        selected = {
            str(item.get("symbol", "")).upper()
            for item in held + candidates
            if item.get("symbol")
        }
        if len(candidates) < candidate_limit:
            candidates.extend(
                self.strategy.research_candidates(
                    candidate_limit - len(candidates),
                    selected,
                    self.agent_assessment,
                    self.wash_sales.blocked_until,
                )
            )
        self.market_agent.submit(
            {
                "event": event,
                "buying_power": self._compact_number(buying_power, 0),
                "positions": held,
                "candidates": [
                    self._compact_candidate(item) for item in candidates
                ],
                "market_pulse": self.market_pulse_cache,
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

    def _compact_candidate(self, item: dict) -> dict:
        record = {
            "symbol": str(item.get("symbol", "")).upper(),
            "price": self._compact_number(item.get("price"), 4),
            "chg": self._compact_number(item.get("change_ratio"), 4),
            "vol": self._compact_number(item.get("volume"), 0),
            "spread": self._compact_number(item.get("spread_percent"), 3),
        }
        if item.get("technical_signal"):
            record["signal"] = item["technical_signal"]
        return record

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
                self.submit_agent_research(
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
        self.submit_agent_research(
            positions,
            buying_power,
            force=True,
            event="LOSS_CIRCUIT_BREAKER_LIQUIDATION",
        )
        return True

    def handle_daily_loss_breaker(self) -> bool:
        """Halt entries for the rest of the day once realized stop-loss
        exits alone (not counting the expected EOD closeout) add up past
        DAILY_MAX_LOSS_DOLLARS. The per-position stop already bounds any
        single loss; this bounds how many of those a bad day can rack up
        before the bot stops opening new positions.
        """
        if not self.config.daily_loss_circuit_breaker_enabled:
            return False
        if self.daily_loss_breaker_triggered:
            return True
        if self.daily_realized_loss < self.config.daily_max_loss_dollars:
            return False
        log.critical(
            "CIRCUIT | DAILY LOSS LIMIT | realized=$%.2f >= limit=$%.2f | "
            "halting new entries for the rest of the trading day",
            self.daily_realized_loss,
            self.config.daily_max_loss_dollars,
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

    def price_sanity_ok(self, last_price: Decimal, limit_price: Decimal) -> bool:
        """Fat-finger guard: reject a limit price that's implausibly far
        from the last observed trade price instead of trusting sizing/
        pricing math blindly. Catches a stale or corrupted quote producing
        a wildly wrong limit before it ever reaches the broker - hardcoded,
        not config, since this is a sanity backstop, not a tuning knob.
        """
        if last_price <= 0:
            return True
        deviation = abs(limit_price - last_price) / last_price
        if deviation > PRICE_SANITY_TOLERANCE:
            log.error(
                "GUARD  | price sanity check failed | last=%.4f limit=%.4f "
                "deviation=%.1f%% (max %.0f%%) | order skipped",
                last_price,
                limit_price,
                deviation * 100,
                PRICE_SANITY_TOLERANCE * 100,
            )
            return False
        return True

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
        """
        total = Decimal(str(quantity))
        clip = total if total < ICEBERG_MIN_SHARES or fractional else Decimal(ICEBERG_SLICE_SHARES)
        last_price = self.api.quote_price(quote)
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
        limit_price = self.api.stock_limit_price(quote, side)
        if not self.price_sanity_ok(last_price, limit_price):
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
                clip = min(entry["remaining"], Decimal(ICEBERG_SLICE_SHARES))
                limit_price = self.api.stock_limit_price(quote, side)
                if not self.price_sanity_ok(self.api.quote_price(quote), limit_price):
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
            self.record_trade(entry["key"], order_id, side)
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
            for oid, order in self.working_orders.items():
                if order.get("key") == key and order.get("action") in (
                    "STOP",
                    "PROFIT",
                ):
                    order_id = oid
                    action = order.get("action")
                    break
            if order_id:
                try:
                    self.api.cancel(order_id)
                except Exception as exc:
                    log.error(
                        "STOP   | %s | escalation cancel failed | %s",
                        symbol,
                        exc,
                    )
                    continue
                order = self.working_orders.pop(order_id, None)
                # This order is being deliberately abandoned mid-flight (it
                # never filled at the gentler price) - a fresh order fires
                # its own PROFIT/STOP decision and records its own pnl next
                # cycle, so the pnl recorded at THIS order's submission has
                # to be reversed now or it inflates the daily total for an
                # exit that never actually happened.
                if order:
                    self.reverse_phantom_exit(order.get("pnl"), order_id)
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

    def trade_stocks(
        self,
        positions: list[dict],
        buying_power: Decimal,
        opening_grace_active: bool = False,
        core_session_active: bool = False,
    ) -> Decimal:
        open_count = self.strategy.open_position_count(positions)
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
        self.refresh_agent_discoveries()
        batch, self.stock_cursor = self.strategy.prioritized_stock_batch(
            self.stock_symbols,
            self.stock_cursor,
            positions,
            self.agent_assessment,
            self.seed_popular_symbols | self.agent_popular_symbols | self.user_watchlist,
        )
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
        try:
            for category, category_symbols in grouped.items():
                category_quotes, category_invalid = (
                    self.api.stock_quotes_resilient(category_symbols, category)
                )
                quotes.extend(category_quotes)
                if category_invalid:
                    if self.config.exclude_etfs and category == "US_STOCK":
                        invalid.update(category_invalid)
                        continue
                    alternate = "US_ETF" if category == "US_STOCK" else "US_STOCK"
                    alternate_quotes, alternate_invalid = (
                        self.api.stock_quotes_resilient(
                            sorted(category_invalid),
                            alternate,
                        )
                    )
                    quotes.extend(alternate_quotes)
                    corrected = category_invalid - alternate_invalid
                    for symbol in corrected:
                        self.stock_categories[symbol] = alternate
                    invalid.update(alternate_invalid)
        except Exception as exc:
            if isinstance(exc, MarketDataPermissionError):
                raise
            log.error("STOCKS | quote batch failed | %s", exc)
            return buying_power
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
        for symbol in batch:
            if symbol in self.broker_conflict_symbols:
                continue
            try:
                quote = quote_by_symbol.get(symbol)
                if not quote:
                    continue
                price = self.api.quote_price(quote)
                self.strategy.update_stock_snapshot(quote, price)
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
                if decision.action == "BUY":
                    self.agent_candidates[symbol] = {
                        "symbol": symbol,
                        "type": self.stock_categories.get(symbol, "US_STOCK"),
                        "price": str(price),
                        **self.strategy.metrics.get(symbol, {}),
                        "technical_signal": "BUY",
                    }
                if quantity == 0:
                    self.pending_stock_exits.discard(symbol)
                    self.stop_exit_submitted.pop(symbol, None)
                    self.stop_loss_escalated.discard(symbol)
                    self.short_symbols.discard(symbol)
                if decision.action == "BUY" and quantity == 0:
                    blocked_until = self.wash_sales.blocked_until(symbol)
                    if blocked_until:
                        self.agent_candidates.pop(symbol, None)
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
                    bucket = self.strategy.selection_bucket(symbol)
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
                        self.record_trade(key, order_id, "BUY")
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
                    bucket = self.strategy.selection_bucket(symbol)
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
                        self.record_trade(key, order_id, "SHORT")
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
                    if exit_quantity < self.strategy.minimum_lot_size(price):
                        self.gate_rejections[
                            "sub-$1 lot-restricted band - exit waits for "
                            "price to clear it"
                        ] += 1
                        continue
                    target = decision.target_price
                    if target is None:
                        continue
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
                        limit_price = (
                            self.api.stock_limit_price(quote, "SELL")
                            if symbol in self.stop_loss_escalated
                            else (max(ask, target) if ask else target)
                        )
                    order_id = self.api.place_stock(
                        symbol,
                        exit_side,
                        exit_quantity,
                        limit_price=limit_price,
                        fractional=exit_is_fractional,
                    )
                    self.pending_stock_exits.add(symbol)
                    self.stop_exit_submitted[symbol] = time.monotonic()
                    pnl = self.record_realized_exit(cost, limit_price, quantity)
                    self.record_trade(key, order_id, "PROFIT", limit_price, pnl=pnl, entry_price=cost)
                if decision.action == "LOSS" and self.stop_ready_to_submit(key, symbol):
                    exit_is_fractional = self.is_fractional_quantity(exit_quantity)
                    if exit_is_fractional and not core_session_active:
                        self.gate_rejections[
                            "fractional position - exit waits for core hours"
                        ] += 1
                        continue
                    if exit_quantity < self.strategy.minimum_lot_size(price):
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
                    limit_price = (
                        self.api.stock_limit_price(
                            quote, "COVER" if is_short_position else "SELL"
                        )
                        if symbol in self.stop_loss_escalated
                        else self.api.stock_stop_exit_price(quote)
                    )
                    order_id = self.api.place_stock(
                        symbol,
                        exit_side,
                        exit_quantity,
                        limit_price=limit_price,
                        fractional=exit_is_fractional,
                    )
                    self.wash_sales.block(symbol, "stop-loss exit submitted")
                    self.pending_stock_exits.add(symbol)
                    self.stop_exit_submitted[symbol] = time.monotonic()
                    pnl = self.record_realized_exit(cost, limit_price, quantity)
                    self.record_trade(key, order_id, "STOP", limit_price, pnl=pnl, entry_price=cost)
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
                self.record_trade(f"STOCK:{long_symbol}", long_order, "BUY")
                self.record_trade(f"STOCK:{short_symbol}", short_order, "BUY")
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
                        self.record_trade(key, order_id, "BUY")
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
                    self.record_trade(key, order_id, "PROFIT", limit_price, pnl=pnl, entry_price=cost)
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
                    self.record_trade(key, order_id, "STOP", limit_price, pnl=pnl, entry_price=cost)
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
    ) -> Decimal | None:
        """Pick the best available green exit price for a stalled position.

        Prefers the bid (fills immediately) whenever it alone clears cost +
        min_profit + fee. If the bid doesn't clear but the ask (top of the
        spread) does, rest a passive limit there instead of giving up - a
        stalled position sitting inside the spread shouldn't be abandoned
        just because the aggressive/immediate price isn't green yet. Never
        prices below cost + min_profit + fee on either side, so this can
        only ever produce a genuinely profitable exit or no exit at all.
        """
        floor = average_cost + min_profit + fee_per_share
        bid = self.api.quote_bid(quote)
        if bid is not None:
            sell_price = bid.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            if sell_price >= floor:
                return sell_price
        ask = self.api.quote_ask(quote)
        if ask is not None:
            sell_price = ask.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
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
                    if quantity < self.strategy.minimum_lot_size(sell_price):
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
                    self.record_trade(key, order_id, "PROFIT", sell_price, pnl=pnl, entry_price=average_cost)
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
                    self.record_trade(key, order_id, "PROFIT", sell_price, pnl=pnl, entry_price=average_cost)
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
            position_rows.append(
                {
                    "symbol": symbol,
                    "instrument_type": position.get("instrument_type"),
                    "quantity": str(quantity),
                    "cost_price": str(position.get("cost_price", "0")),
                    "last_price": str(
                        self.strategy.prices.get(symbol, position.get("cost_price", "0"))
                    ),
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
            total_equity = buying_power + sum(
                (
                    Decimal(row["quantity"]) * Decimal(row["last_price"])
                    for row in position_rows
                ),
                Decimal("0"),
            )
            self.status.record_balance(total_equity)
        pending_order_rows = [
            {
                "order_id": order_id,
                "instrument_type": order.get("key", "?:?").split(":", 1)[0],
                "symbol": order.get("key", "?:?").split(":", 1)[-1],
                "action": order.get("action"),
                "limit_price": (
                    str(order["limit_price"])
                    if order.get("limit_price") is not None
                    else None
                ),
                "age_seconds": round(now - float(order.get("submitted_at", now))),
                "cancel_requested": order.get("cancel_requested_at") is not None,
            }
            for order_id, order in self.working_orders.items()
        ]
        self.status.write(
            mode=self.config.mode,
            buying_power=buying_power,
            positions=position_rows,
            watchlist=watchlist_rows,
            agent_summary=agent_summary,
            paused=paused,
            stock_count=len(self.stock_symbols),
            option_count=len(self.option_contracts),
            realized_pnl_today=self.daily_realized_pnl,
            open_pnl_total=day_pnl_total,
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
            self.record_trade(f"STOCK:{symbol}", order_id, "MANUAL_SELL", sell_price, pnl=pnl, entry_price=cost)
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
            self.record_trade(f"OPTION:{symbol}", order_id, "MANUAL_SELL", sell_price, pnl=pnl, entry_price=cost)
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
        try:
            order_id = self.api.place_stock(
                symbol,
                "BUY",
                buy_quantity,
                limit_price=self.api.stock_limit_price(quote, "BUY"),
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
        self.record_trade(f"STOCK:{symbol}", order_id, "MANUAL_BUY")
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
        log.warning("CMD    | added %-8s to watchlist from dashboard", symbol)

    def run(self) -> None:
        log.info(
            "START  | mode=%s | poll=%ss | cooldown=%ss",
            self.config.mode,
            self.config.poll_seconds,
            self.config.trade_cooldown_seconds,
        )
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
                self.monitor_working_orders()
                self.process_iceberg_orders()
                self.reprice_resting_exits(self.cached_positions, core_session_active)
                self.escalate_stalled_stop_losses()
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
                    self.submit_agent_research(positions, buying_power)
                self.write_status_snapshot(positions, buying_power, circuit_active)
                if time.monotonic() - self.last_status_log >= 1:
                    self.last_status_log = time.monotonic()
                    log.info(
                        "SCAN   | stocks=%s/%s | options=%s/%s | positions=%s | "
                        "buying power=$%.2f | pnl today=$%.2f | watchlist=%s | paused=%s",
                        min(self.config.stock_batch_size, len(self.stock_symbols)),
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
