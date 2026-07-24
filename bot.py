import logging
import time
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from config import settings
from strategy import EMACrossStrategy
from webull_api import WebullAPI


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("webull-bot")


class AutoTrader:
    def __init__(self):
        self.config = settings()
        self.config.validate_runtime()
        self.api = WebullAPI(self.config)
        self.strategy = EMACrossStrategy(
            self.config.ema_fast_period,
            self.config.ema_slow_period,
        )
        self.timezone = ZoneInfo(self.config.trading_timezone)
        self.last_trade: dict[str, float] = {}
        self.stock_symbols: list[str] = []
        self.option_contracts: list[dict] = []
        self.stock_prices: dict[str, Decimal] = {}
        self.stock_cursor = 0
        self.option_cursor = 0
        self.option_discovery_cursor = 0
        self.option_discovery_attempted: set[str] = set()
        self.discover_all_options = False
        self.resolved_date = None
        self.last_close_attempt = 0.0

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
        self.stock_symbols = (
            self.api.stock_universe()
            if requested_stocks == ["ALL"]
            else (
                requested_stocks
                if self.config.max_symbols == 0
                else requested_stocks[: self.config.max_symbols]
            )
        )
        self.option_contracts = self.api.resolve_options()
        self.discover_all_options = "ALL" in self.config.option_roots()
        self.stock_prices.clear()
        self.stock_cursor = 0
        self.option_cursor = 0
        self.option_discovery_cursor = 0
        self.option_discovery_attempted.clear()
        self.resolved_date = moment.date()
        log.info(
            "Targets: %s stocks, %s initial options, all-options-discovery=%s",
            len(self.stock_symbols),
            len(self.option_contracts),
            self.discover_all_options,
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
        if not self.discover_all_options or not self.stock_symbols:
            return
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
                    "Options discovered %s for %s (%s/%s checked)",
                    ",".join(contract["symbol"] for contract in contracts),
                    underlying,
                    len(self.option_discovery_attempted),
                    len(self.stock_symbols),
                )
            except Exception as exc:
                if len(self.option_discovery_attempted) % 100 == 0:
                    log.info(
                        "Option discovery progress %s/%s; latest=%s: %s",
                        len(self.option_discovery_attempted),
                        len(self.stock_symbols),
                        underlying,
                        exc,
                    )

    def cooldown_ready(self, key: str) -> bool:
        elapsed = time.monotonic() - self.last_trade.get(key, float("-inf"))
        return elapsed >= float(self.config.trade_cooldown_seconds)

    def record_trade(self, key: str, order_id: str, action: str) -> None:
        self.last_trade[key] = time.monotonic()
        log.info("%s %s order=%s", action, key, order_id)

    @staticmethod
    def open_position_count(positions: list[dict]) -> int:
        return sum(
            1
            for item in positions
            if Decimal(str(item.get("quantity", "0"))) != 0
        )

    def trade_stocks(self, positions: list[dict]) -> None:
        open_count = self.open_position_count(positions)
        batch, self.stock_cursor = self.rotating_batch(
            self.stock_symbols,
            self.stock_cursor,
            self.config.stock_batch_size,
        )
        try:
            quotes = self.api.stock_quotes(batch)
        except Exception as exc:
            log.error("Stock quote batch: %s", exc)
            return
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
                signal = self.strategy.signal(
                    f"STOCK:{symbol}",
                    float(price),
                    self.config.reenter_on_trend,
                )
                quantity, cost = self.api.stock_position(symbol, positions)
                key = f"STOCK:{symbol}"
                if signal == "BUY" and quantity == 0:
                    notional = price * self.config.stock_quantity
                    if (
                        open_count < self.config.max_open_positions
                        and notional <= self.config.max_order_notional
                        and self.cooldown_ready(key)
                    ):
                        order_id = self.api.place_stock(
                            symbol,
                            "BUY",
                            self.config.stock_quantity,
                        )
                        self.record_trade(key, order_id, "BUY")
                        positions.append(
                            {
                                "instrument_type": "EQUITY",
                                "symbol": symbol,
                                "quantity": str(self.config.stock_quantity),
                            }
                        )
                        open_count += 1
                take_profit = (
                    quantity > 0
                    and cost > 0
                    and price - cost >= self.config.stock_take_profit_per_share
                )
                stop_loss = (
                    quantity > 0
                    and cost > 0
                    and cost - price >= self.config.stock_stop_loss_per_share
                )
                if (
                    quantity > 0
                    and (signal == "SELL" or take_profit or stop_loss)
                    and self.cooldown_ready(key)
                ):
                    order_id = self.api.place_stock(symbol, "SELL", quantity)
                    reason = "TAKE_PROFIT" if take_profit else "STOP_LOSS" if stop_loss else "SELL"
                    self.record_trade(key, order_id, reason)
            except Exception as exc:
                log.error("Stock %s: %s", symbol, exc)
        self.discover_option_contracts()

    def trade_options(self, positions: list[dict]) -> None:
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
            log.error("Option quote batch: %s", exc)
            return
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
                signal = self.strategy.signal(
                    key,
                    float(price),
                    self.config.reenter_on_trend,
                )
                quantity, cost = self.api.option_position(contract, positions)
                if signal == "BUY" and quantity == 0:
                    premium = price * 100 * self.config.option_quantity
                    if (
                        open_count < self.config.max_open_positions
                        and premium <= self.config.max_order_notional
                        and self.cooldown_ready(key)
                    ):
                        limit_price = self.api.option_limit_price(quote, "BUY")
                        order_id = self.api.place_option(
                            contract,
                            "BUY",
                            self.config.option_quantity,
                            limit_price,
                            "BUY_TO_OPEN",
                        )
                        self.record_trade(key, order_id, "BUY")
                        positions.append(
                            {
                                "instrument_type": "OPTION",
                                "symbol": option_symbol,
                                "quantity": str(self.config.option_quantity),
                            }
                        )
                        open_count += 1
                take_profit = (
                    quantity > 0
                    and cost > 0
                    and price - cost >= self.config.option_take_profit_price
                )
                stop_loss = (
                    quantity > 0
                    and cost > 0
                    and cost - price >= self.config.option_stop_loss_price
                )
                if (
                    quantity > 0
                    and (signal == "SELL" or take_profit or stop_loss)
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
                    reason = "TAKE_PROFIT" if take_profit else "STOP_LOSS" if stop_loss else "SELL"
                    self.record_trade(key, order_id, reason)
            except Exception as exc:
                log.error("Option %s: %s", option_symbol, exc)

    def close_everything(self) -> bool:
        now = time.monotonic()
        if now - self.last_close_attempt < self.config.eod_retry_seconds:
            return False
        self.last_close_attempt = now
        try:
            submitted = self.api.close_all_positions()
            remaining = [
                item
                for item in self.api.positions()
                if Decimal(str(item.get("quantity", "0"))) != 0
            ]
            log.info(
                "End-of-day closeout submitted=%s remaining=%s",
                len(submitted),
                len(remaining),
            )
            return not remaining
        except Exception as exc:
            log.error("End-of-day closeout: %s", exc)
            return False

    def run(self) -> None:
        log.info(
            "Starting mode=%s poll=%ss cooldown=%ss",
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

            if moment < market_open:
                time.sleep(min(60, max(1, (market_open - moment).total_seconds())))
                continue

            if closeout <= moment < market_close:
                finished = self.close_everything()
                time.sleep(60 if finished else self.config.eod_retry_seconds)
                continue

            if moment >= market_close:
                time.sleep(60)
                continue

            self.resolve_targets(moment)
            try:
                positions = self.api.positions()
                self.trade_stocks(positions)
                self.trade_options(positions)
            except Exception as exc:
                log.error("Trading cycle: %s", exc)

            seconds_to_closeout = max(
                1.0,
                (closeout - self.now()).total_seconds(),
            )
            time.sleep(min(float(self.config.poll_seconds), seconds_to_closeout))


if __name__ == "__main__":
    try:
        AutoTrader().run()
    except KeyboardInterrupt:
        log.info("Stopped")
