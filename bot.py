import logging
import sys
import time
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from zoneinfo import ZoneInfo

from rich.logging import RichHandler

from config import settings
from daily_logging import add_daily_file_logging
from market_agent import MarketResearchAgent
from strategy import TradingStrategy
from wash_sale import WashSaleTracker
from webull_api import (
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
        self.wash_skip_logged: set[str] = set()
        self.last_trade: dict[str, float] = {}
        self.stock_symbols: list[str] = []
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
        self.options_enabled = True
        self.resolved_date = None
        self.last_close_attempt = 0.0
        self.last_status_log = 0.0
        self.last_option_discovery = 0.0
        self.last_account_refresh = 0.0
        self.last_order_monitor = 0.0
        self.last_fill_time = time.monotonic()
        self.last_stall_boost = 0.0
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
        self.position_buckets: dict[str, str] = {}

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

    def resolve_targets(self, moment: datetime) -> None:
        if self.resolved_date == moment.date():
            return
        requested_stocks = self.config.stocks()
        if requested_stocks == ["ALL"]:
            limit = self.config.stock_universe_limit()
            log.info("LOAD   | downloading stocks and ETFs | limit=%s", limit)
            self.stock_categories = self.api.stock_universe(
                lambda category, count, category_limit: log.info(
                    "LOAD   | %-8s | %s/%s",
                    category,
                    count,
                    category_limit or "ALL",
                )
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
            self.stock_symbols = list(self.stock_categories)
        else:
            log.info("LOAD   | resolving %s configured symbols", len(requested_stocks))
            self.stock_symbols = (
                requested_stocks
                if self.config.max_symbols == 0
                else requested_stocks[: self.config.max_symbols]
            )
            self.stock_categories = self.api.stock_categories(self.stock_symbols)
            for symbol in self.stock_symbols:
                self.stock_categories.setdefault(symbol, "US_STOCK")
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

    def record_trade(
        self,
        key: str,
        order_id: str,
        action: str,
        limit_price: Decimal | None = None,
    ) -> None:
        submitted_at = time.monotonic()
        self.last_trade[key] = submitted_at
        self.working_orders[order_id] = {
            "submitted_at": submitted_at,
            "key": key,
            "action": action,
            "cancel_requested_at": None,
        }
        instrument_type, symbol = key.split(":", 1)
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
                if order.get("cancel_requested_at") is None:
                    self.last_fill_time = now
                self._release_pending_order(order)
                del self.working_orders[order_id]
                self.last_account_refresh = 0.0
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

    def account_state(self) -> tuple[Decimal, list[dict]]:
        now = time.monotonic()
        if (
            now - self.last_account_refresh
            >= float(self.config.account_refresh_seconds)
        ):
            self.cached_buying_power = self.api.buying_power()
            self.cached_positions = self.api.positions()
            self.last_account_refresh = now
        return self.cached_buying_power, [dict(item) for item in self.cached_positions]

    def agent_assessment(self, symbol: str) -> dict | None:
        if not self.market_agent:
            return None
        return self.market_agent.assessment(symbol)

    def refresh_agent_discoveries(self) -> None:
        if not self.market_agent:
            self.agent_popular_symbols.clear()
            return
        available = set(self.stock_symbols)
        discoveries = self.market_agent.discoveries()
        self.agent_popular_symbols = {
            str(item.get("symbol", "")).upper()
            for item in discoveries
            if str(item.get("symbol", "")).upper() in available
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
        research_limit = min(self.config.agent_max_symbols, 10)
        held = [
            {
                "symbol": item.get("symbol"),
                "type": item.get("instrument_type"),
                "quantity": item.get("quantity"),
                "average_cost": item.get("cost_price"),
                "unrealized_pnl": str(
                    self.strategy.position_unrealized_pnl(item)
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
                "time": self.now().isoformat(),
                "buying_power": str(buying_power),
                "positions": held,
                "entry_candidates": candidates,
                "discovery_limit": self.config.agent_discovery_max_symbols,
            },
            force=force,
        )

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

    def trade_stocks(
        self,
        positions: list[dict],
        buying_power: Decimal,
    ) -> Decimal:
        open_count = self.strategy.open_position_count(positions)
        self.refresh_agent_discoveries()
        batch, self.stock_cursor = self.strategy.prioritized_stock_batch(
            self.stock_symbols,
            self.stock_cursor,
            positions,
            self.agent_assessment,
            self.seed_popular_symbols | self.agent_popular_symbols,
        )
        bucket_remaining = {
            bucket: buying_power * fraction
            for bucket, fraction in self.config.stock_capital_fractions().items()
        }
        bucket_slot_limits = self.config.stock_bucket_slot_limits()
        bucket_position_counts = {bucket: 0 for bucket in bucket_slot_limits}
        known_popular = self.seed_popular_symbols | self.agent_popular_symbols
        for position in positions:
            if (
                position.get("instrument_type") != "EQUITY"
                or Decimal(str(position.get("quantity", "0"))) == 0
            ):
                continue
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
            self.stock_symbols = [
                symbol for symbol in self.stock_symbols if symbol not in invalid
            ]
            self.stock_cursor %= max(1, len(self.stock_symbols))
            log.warning(
                "SKIP   | invalid=%s | %s",
                len(invalid),
                ",".join(sorted(invalid)),
            )
        quote_by_symbol = {
            str(quote.get("symbol", "")).upper(): quote for quote in quotes
        }
        for symbol in batch:
            try:
                quote = quote_by_symbol.get(symbol)
                if not quote:
                    continue
                price = self.api.quote_price(quote)
                self.strategy.update_stock_snapshot(quote, price)
                quantity, cost = self.api.stock_position(symbol, positions)
                key = f"STOCK:{symbol}"
                decision = self.strategy.stock_decision(
                    key,
                    price,
                    quantity,
                    cost,
                    self.agent_assessment(symbol),
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
                    bucket = self.strategy.selection_bucket(symbol)
                    entry_budget = min(
                        buying_power,
                        bucket_remaining.get(bucket, Decimal("0")),
                    )
                    buy_quantity, buffered_price = (
                        self.strategy.stock_order_quantity(
                            price,
                            entry_budget,
                        )
                    )
                    if (
                        open_count < self.config.max_open_positions
                        and bucket_position_counts.get(bucket, 0)
                        < bucket_slot_limits.get(bucket, 0)
                        and buy_quantity > 0
                        and self.cooldown_ready(key)
                    ):
                        order_id = self.api.place_stock(
                            symbol,
                            "BUY",
                            buy_quantity,
                            limit_price=self.api.stock_limit_price(quote, "BUY"),
                        )
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
                if (
                    decision.action == "PROFIT"
                    and symbol not in self.pending_stock_exits
                    and self.cooldown_ready(key)
                ):
                    target = decision.target_price
                    if target is None:
                        continue
                    order_id = self.api.place_stock(
                        symbol,
                        "SELL",
                        quantity,
                        limit_price=target,
                    )
                    self.pending_stock_exits.add(symbol)
                    self.record_trade(key, order_id, "PROFIT", target)
                if (
                    decision.action == "LOSS"
                    and symbol not in self.pending_stock_exits
                    and self.cooldown_ready(key)
                ):
                    limit_price = self.api.stock_limit_price(quote, "SELL")
                    self.wash_sales.block(symbol, "2% stop-loss exit submitted")
                    order_id = self.api.place_stock(
                        symbol,
                        "SELL",
                        quantity,
                        limit_price=limit_price,
                    )
                    self.pending_stock_exits.add(symbol)
                    self.record_trade(key, order_id, "STOP", limit_price)
            except Exception as exc:
                if isinstance(exc, QuoteUnavailableError):
                    continue
                if "BUYING_POWER_INSUFFICIENT" in str(exc):
                    buying_power = Decimal("0")
                    log.warning(
                        "FUNDS  | %s | buy skipped | insufficient buying power",
                        symbol,
                    )
                    continue
                log.error("STOCK  | %s | %s", symbol, exc)
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
        if not self.options_enabled:
            return buying_power
        open_count = self.strategy.open_position_count(positions)
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
        for contract in batch:
            option_symbol = contract["symbol"]
            key = f"OPTION:{option_symbol}"
            try:
                quote = quote_by_symbol.get(option_symbol)
                if not quote:
                    continue
                price = self.api.quote_price(quote)
                quantity, cost = self.api.option_position(contract, positions)
                decision = self.strategy.option_decision(
                    key,
                    price,
                    quantity,
                    cost,
                )
                if quantity == 0:
                    self.pending_option_exits.discard(option_symbol)
                if decision.action == "BUY" and quantity == 0:
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
                    self.record_trade(key, order_id, "PROFIT", limit_price)
            except Exception as exc:
                if isinstance(exc, QuoteUnavailableError):
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

    def boost_stalled_positions(
        self,
        positions: list[dict],
        options_active: bool,
    ) -> None:
        if not self.config.stall_breaker_enabled:
            return
        now = time.monotonic()
        stall_seconds = float(self.config.stall_breaker_seconds)
        if now - self.last_fill_time < stall_seconds:
            return
        if now - self.last_stall_boost < stall_seconds:
            return
        self.last_stall_boost = now
        min_profit = self.config.stall_breaker_min_profit
        boosted = 0
        for position in positions:
            quantity = int(Decimal(str(position.get("quantity", "0"))))
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
                    quote = self.api.stock_quote(symbol)
                    bid = self.api.quote_bid(quote)
                    if bid is None or bid - average_cost < min_profit:
                        continue
                    sell_price = bid.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
                    if sell_price - average_cost < min_profit:
                        continue
                    order_id = self.api.place_stock(
                        symbol,
                        "SELL",
                        quantity,
                        limit_price=sell_price,
                    )
                    self.pending_stock_exits.add(symbol)
                    self.record_trade(key, order_id, "PROFIT", sell_price)
                    boosted += 1
                elif instrument_type == "OPTION" and options_active:
                    if symbol in self.pending_option_exits:
                        continue
                    key = f"OPTION:{symbol}"
                    if not self.cooldown_ready(key):
                        continue
                    contract = self.api.contract_from_position(position)
                    if not contract:
                        continue
                    quote = self.api.option_quote(contract["symbol"])
                    bid = self.api.quote_bid(quote)
                    if bid is None or bid - average_cost < min_profit:
                        continue
                    sell_price = bid.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
                    if sell_price - average_cost < min_profit:
                        continue
                    order_id = self.api.place_option(
                        contract,
                        "SELL",
                        quantity,
                        sell_price,
                        "SELL_TO_CLOSE",
                    )
                    self.pending_option_exits.add(symbol)
                    self.record_trade(key, order_id, "PROFIT", sell_price)
                    boosted += 1
            except Exception as exc:
                if isinstance(exc, QuoteUnavailableError):
                    continue
                log.error("STALL  | %s | %s", symbol, exc)
        if boosted:
            self.last_account_refresh = 0.0
            log.info(
                "STALL  | no fills for %ss | boosted %s profitable exit(s)",
                self.config.stall_breaker_seconds,
                boosted,
            )

    def close_instruments(self, instrument_types: set[str]) -> bool:
        now = time.monotonic()
        if now - self.last_close_attempt < self.config.eod_retry_seconds:
            return False
        self.last_close_attempt = now
        try:
            submitted = self.api.close_all_positions(
                instrument_types,
                loss_callback=self.wash_sales.block,
            )
            self.pending_stock_exits.clear()
            self.pending_option_exits.clear()
            remaining = [
                item
                for item in self.api.positions()
                if item.get("instrument_type") in instrument_types
                if Decimal(str(item.get("quantity", "0"))) != 0
            ]
            log.info(
                "CLOSE  | submitted=%s | remaining=%s",
                len(submitted),
                len(remaining),
            )
            return not remaining
        except Exception as exc:
            log.error("CLOSE  | failed | %s", exc)
            return False

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
                finished = self.close_instruments({"EQUITY"})
                time.sleep(60 if finished else self.config.eod_retry_seconds)
                continue

            if moment >= market_close:
                self.log_day_end_summary(moment)
                time.sleep(60)
                continue

            if option_closeout <= moment < option_close:
                self.close_instruments({"OPTION"})

            self.resolve_targets(moment)
            cycle_started = time.monotonic()
            try:
                self.monitor_working_orders()
                buying_power, positions = self.account_state()
                circuit_active = self.handle_portfolio_circuit_breaker(
                    positions,
                    buying_power,
                )
                if not circuit_active:
                    buying_power = self.trade_stocks(positions, buying_power)
                    if option_open <= moment < option_closeout:
                        self.discover_option_contracts()
                        buying_power = self.trade_options(positions, buying_power)
                    self.boost_stalled_positions(
                        positions,
                        option_open <= moment < option_closeout,
                    )
                    self.cached_buying_power = buying_power
                    self.cached_positions = [dict(item) for item in positions]
                    self.submit_agent_research(positions, buying_power)
                if time.monotonic() - self.last_status_log >= 30:
                    self.last_status_log = time.monotonic()
                    log.info(
                        "SCAN   | stocks=%s/%s | options=%s/%s | positions=%s | buying power=$%.2f | paused=%s",
                        min(self.config.stock_batch_size, len(self.stock_symbols)),
                        len(self.stock_symbols),
                        min(self.config.option_batch_size, len(self.option_contracts)),
                        len(self.option_contracts),
                        self.strategy.open_position_count(positions),
                        buying_power,
                        "YES" if circuit_active else "NO",
                    )
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


if __name__ == "__main__":
    try:
        runtime_config = settings()
        add_daily_file_logging(
            log,
            runtime_config.log_directory,
            runtime_config.trading_timezone,
        )
        if "--close-all" in sys.argv:
            force_close_all()
        else:
            AutoTrader().run()
    except KeyboardInterrupt:
        log.info("STOPPED")
