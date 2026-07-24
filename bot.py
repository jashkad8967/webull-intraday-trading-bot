import logging
import sys
import time
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from zoneinfo import ZoneInfo

from rich.logging import RichHandler

from config import settings
from market_agent import MarketResearchAgent
from strategy import TradingStrategy
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
        self.strategy = TradingStrategy(
            self.config.ema_fast_period,
            self.config.ema_slow_period,
        )
        self.market_agent = (
            MarketResearchAgent(self.config, log)
            if self.config.agent_enabled
            else None
        )
        self.timezone = ZoneInfo(self.config.trading_timezone)
        self.last_trade: dict[str, float] = {}
        self.stock_symbols: list[str] = []
        self.stock_categories: dict[str, str] = {}
        self.invalid_stock_symbols: set[str] = set()
        self.option_contracts: list[dict] = []
        self.stock_prices: dict[str, Decimal] = {}
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
        self.cached_buying_power = Decimal("0")
        self.cached_positions: list[dict] = []
        self.agent_candidates: dict[str, dict] = {}
        self.entries_paused = False
        self.circuit_breaker_time = 0.0
        self.last_circuit_research = 0.0

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
            limit = self.config.max_symbols or "ALL"
            log.info("LOAD   | downloading stocks and ETFs | limit=%s", limit)
            self.stock_categories = self.api.stock_universe(
                lambda category, count, category_limit: log.info(
                    "LOAD   | %-8s | %s/%s",
                    category,
                    count,
                    category_limit or "ALL",
                )
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
        self.stock_prices.clear()
        self.stock_cursor = 0
        self.option_cursor = 0
        self.option_discovery_cursor = 0
        self.option_discovery_attempted.clear()
        self.invalid_stock_symbols.clear()
        self.resolved_date = moment.date()
        log.info(
            "READY  | stocks=%s | options=%s | option scan=%s",
            len(self.stock_symbols),
            len(self.option_contracts),
            "ON" if self.discover_all_options else "OFF",
        )

    @staticmethod
    def rotating_batch(
        items: list,
        cursor: int,
        batch_size: int,
    ) -> tuple[list, int]:
        if not items:
            return [], 0
        size = min(batch_size, len(items))
        batch = [items[(cursor + offset) % len(items)] for offset in range(size)]
        return batch, (cursor + size) % len(items)

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
                or underlying not in self.stock_prices
            ):
                continue
            self.option_discovery_attempted.add(underlying)
            attempts += 1
            try:
                contracts = self.api.select_atm_options(
                    underlying,
                    self.stock_prices[underlying],
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
        self.last_trade[key] = time.monotonic()
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

    @staticmethod
    def open_position_count(positions: list[dict]) -> int:
        return sum(
            1
            for item in positions
            if Decimal(str(item.get("quantity", "0"))) != 0
        )

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

    def submit_agent_research(
        self,
        positions: list[dict],
        buying_power: Decimal,
        force: bool = False,
        event: str = "ROUTINE_RESEARCH",
    ) -> None:
        if not self.market_agent:
            return
        held = [
            {
                "symbol": item.get("symbol"),
                "type": item.get("instrument_type"),
                "quantity": item.get("quantity"),
                "average_cost": item.get("cost_price"),
                "unrealized_pnl": str(self.position_unrealized_pnl(item)),
            }
            for item in positions
            if Decimal(str(item.get("quantity", "0"))) != 0
        ]
        candidates = list(self.agent_candidates.values())[
            : self.config.agent_max_symbols
        ]
        self.agent_candidates.clear()
        if not force and not candidates and not held:
            return
        self.market_agent.submit(
            {
                "event": event,
                "time": self.now().isoformat(),
                "buying_power": str(buying_power),
                "positions": held,
                "entry_candidates": candidates,
            },
            force=force,
        )

    @staticmethod
    def position_unrealized_pnl(position: dict) -> Decimal:
        reported = position.get("unrealized_profit_loss")
        if reported not in (None, ""):
            try:
                return Decimal(str(reported))
            except Exception:
                pass
        try:
            quantity = Decimal(str(position.get("quantity", "0")))
            cost = Decimal(str(position.get("cost_price", "0")))
            price = Decimal(
                str(
                    position.get("last_price")
                    or position.get("market_price")
                    or "0"
                )
            )
            multiplier = (
                Decimal("100")
                if position.get("instrument_type") == "OPTION"
                else Decimal("1")
            )
            return (price - cost) * quantity * multiplier
        except Exception:
            return Decimal("0")

    def handle_portfolio_circuit_breaker(
        self,
        positions: list[dict],
        buying_power: Decimal,
    ) -> bool:
        if not self.config.loss_circuit_breaker_enabled:
            return False

        now = time.monotonic()
        if self.entries_paused:
            regime = self.market_agent.market_regime() if self.market_agent else None
            old_enough = (
                now - self.circuit_breaker_time
                >= self.config.loss_reevaluation_seconds
            )
            fresh = regime and regime["updated_at"] > self.circuit_breaker_time
            approved = (
                fresh
                and regime["action"] == "RESUME"
                and regime["confidence"] >= self.config.agent_min_confidence
            )
            if old_enough and approved:
                self.entries_paused = False
                log.warning(
                    "CIRCUIT | resumed | confidence=%.2f | %s",
                    regime["confidence"],
                    regime["summary"],
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
            assessment = self.agent_assessment(symbol) or {}
            states.append(
                {
                    "symbol": symbol,
                    "unrealized_pnl": self.position_unrealized_pnl(position),
                    "agent_action": assessment.get("action"),
                    "agent_confidence": assessment.get("confidence", 0),
                }
            )
        decision = self.strategy.portfolio_decision(
            states,
            self.config.loss_spree_position_count,
            self.config.loss_spree_total_dollars,
            self.config.loss_spree_low_outlook_fraction,
            self.config.agent_min_confidence,
        )
        if decision.action != "LIQUIDATE":
            return False

        log.critical(
            "CIRCUIT | LIQUIDATE | losers=%s | loss=$%.2f | %s",
            decision.losing_positions,
            decision.total_loss,
            decision.reason,
        )
        submitted = self.api.close_all_positions()
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
        open_count = self.open_position_count(positions)
        batch, self.stock_cursor = self.rotating_batch(
            self.stock_symbols,
            self.stock_cursor,
            self.config.stock_batch_size,
        )
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
                self.stock_prices[symbol] = price
                quantity, cost = self.api.stock_position(symbol, positions)
                key = f"STOCK:{symbol}"
                agent_values = self.agent_assessment(symbol)
                decision = self.strategy.decide(
                    key=key,
                    price=price,
                    quantity=quantity,
                    average_cost=cost,
                    take_profit=self.config.stock_take_profit_per_share,
                    reenter_on_trend=self.config.reenter_on_trend,
                    agent_values=agent_values,
                    agent_required=(
                        self.market_agent is not None
                        and self.config.agent_required_for_entry
                    ),
                    agent_min_confidence=self.config.agent_min_confidence,
                    agent_min_entry_score=self.config.agent_min_entry_score,
                    agent_max_downside_risk=self.config.agent_max_downside_risk,
                    agent_max_liquidity_risk=self.config.agent_max_liquidity_risk,
                )
                if decision.action == "RESEARCH":
                    self.agent_candidates[symbol] = {
                        "symbol": symbol,
                        "type": self.stock_categories.get(symbol, "US_STOCK"),
                        "price": str(price),
                        "technical_signal": "BUY",
                    }
                if quantity == 0:
                    self.pending_stock_exits.discard(symbol)
                if decision.action == "BUY" and quantity == 0:
                    buffered_price = price * Decimal("1.03")
                    affordable = int(
                        (buying_power / buffered_price).to_integral_value(
                            rounding=ROUND_DOWN
                        )
                    )
                    order_limit = int(
                        (
                            self.config.max_order_notional / price
                        ).to_integral_value(rounding=ROUND_DOWN)
                    )
                    buy_quantity = min(
                        self.config.stock_quantity,
                        affordable,
                        order_limit,
                    )
                    if (
                        open_count < self.config.max_open_positions
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

    def trade_options(
        self,
        positions: list[dict],
        buying_power: Decimal,
    ) -> Decimal:
        if not self.options_enabled:
            return buying_power
        open_count = self.open_position_count(positions)
        batch, self.option_cursor = self.rotating_batch(
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
                underlying = contract["underlying_symbol"]
                agent_values = self.agent_assessment(underlying)
                decision = self.strategy.decide(
                    key=key,
                    price=price,
                    quantity=quantity,
                    average_cost=cost,
                    take_profit=self.config.option_take_profit_price,
                    reenter_on_trend=self.config.reenter_on_trend,
                    agent_values=agent_values,
                    agent_required=(
                        self.market_agent is not None
                        and self.config.agent_required_for_entry
                    ),
                    agent_min_confidence=self.config.agent_min_confidence,
                    agent_min_entry_score=self.config.agent_min_entry_score,
                    agent_max_downside_risk=self.config.agent_max_downside_risk,
                    agent_max_liquidity_risk=self.config.agent_max_liquidity_risk,
                )
                if decision.action == "RESEARCH":
                    self.agent_candidates[underlying] = {
                        "symbol": underlying,
                        "type": "OPTION_UNDERLYING",
                        "option_symbol": option_symbol,
                        "option_price": str(price),
                        "technical_signal": "BUY",
                    }
                if quantity == 0:
                    self.pending_option_exits.discard(option_symbol)
                if decision.action == "BUY" and quantity == 0:
                    limit_price = self.api.option_limit_price(quote, "BUY")
                    contract_cost = limit_price * 100
                    affordable = int(
                        (buying_power / contract_cost).to_integral_value(
                            rounding=ROUND_DOWN
                        )
                    )
                    order_limit = int(
                        (
                            self.config.max_order_notional / contract_cost
                        ).to_integral_value(rounding=ROUND_DOWN)
                    )
                    buy_quantity = min(
                        self.config.option_quantity,
                        affordable,
                        order_limit,
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

    def close_instruments(self, instrument_types: set[str]) -> bool:
        now = time.monotonic()
        if now - self.last_close_attempt < self.config.eod_retry_seconds:
            return False
        self.last_close_attempt = now
        try:
            submitted = self.api.close_all_positions(instrument_types)
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
                time.sleep(60)
                continue

            if option_closeout <= moment < option_close:
                self.close_instruments({"OPTION"})

            self.resolve_targets(moment)
            cycle_started = time.monotonic()
            try:
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
                        self.open_position_count(positions),
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
    log.warning("MANUAL | cancelling orders and closing every account position")
    submitted = api.close_all_positions()
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
        if "--close-all" in sys.argv:
            force_close_all()
        else:
            AutoTrader().run()
    except KeyboardInterrupt:
        log.info("STOPPED")
