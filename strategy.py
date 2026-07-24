import math
from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    target_price: Decimal | None = None


@dataclass(frozen=True)
class PortfolioDecision:
    action: str
    reason: str
    losing_positions: int = 0
    total_loss: Decimal = Decimal("0")


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

    def clear_market_state(self) -> None:
        self.activity.clear()
        self.prices.clear()
        self.metrics.clear()

    @staticmethod
    def rotating_batch(items: list, cursor: int, batch_size: int) -> tuple[list, int]:
        if not items:
            return [], 0
        size = min(batch_size, len(items))
        batch = [items[(cursor + offset) % len(items)] for offset in range(size)]
        return batch, (cursor + size) % len(items)

    @staticmethod
    def quote_number(quote: dict, *fields: str) -> float:
        for field in fields:
            try:
                value = float(quote.get(field, ""))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                return value
        return 0.0

    def update_stock_snapshot(self, quote: dict, price: Decimal) -> None:
        symbol = str(quote.get("symbol", "")).upper()
        if not symbol:
            return
        regular_volume = self.quote_number(quote, "volume")
        extended_volume = self.quote_number(quote, "extend_hour_volume")
        volume = max(0.0, regular_volume + extended_volume)
        movement = max(
            abs(self.quote_number(quote, "change_ratio")),
            abs(self.quote_number(quote, "extend_hour_change_ratio")),
        )
        high = self.quote_number(quote, "extend_hour_high", "high")
        low = self.quote_number(quote, "extend_hour_low", "low")
        bid = self.quote_number(quote, "bid")
        ask = self.quote_number(quote, "ask")
        midpoint = (bid + ask) / 2 if bid > 0 and ask >= bid else 0.0
        spread_percent = (
            ((ask - bid) / midpoint) * 100
            if midpoint > 0
            else 0.0
        )
        range_ratio = (
            (high - low) / float(price)
            if price > 0 and high >= low
            else 0.0
        )
        activity = (
            math.log10(1.0 + volume)
            + 50.0 * movement
            + 25.0 * max(0.0, range_ratio)
        )
        self.prices[symbol] = price
        self.activity[symbol] = activity
        self.metrics[symbol] = {
            "volume": int(volume),
            "change_ratio": movement,
            "bid": bid,
            "ask": ask,
            "spread_percent": round(spread_percent, 4),
            "activity_score": activity,
        }

    def priority_score(self, symbol: str, assessment: dict | None) -> float:
        score = self.activity.get(symbol, 0.0)
        if not assessment:
            return score
        confidence = float(assessment.get("confidence", 0))
        research_priority = float(assessment.get("priority", 0))
        spread_opportunity = float(
            assessment.get("spread_opportunity", 0)
        )
        expected_move = min(
            1.0,
            abs(float(assessment.get("expected_move_percent", 0))) / 5.0,
        )
        catalyst = abs(float(assessment.get("catalyst_strength", 0)))
        volatility = float(assessment.get("market_volatility", 0))
        return score + 2.0 * confidence * (
            research_priority
            + spread_opportunity
            + expected_move
            + catalyst
            + volatility
        )

    def prioritized_stock_batch(
        self,
        symbols: list[str],
        cursor: int,
        positions: list[dict],
        assessment_for,
    ) -> tuple[list[str], int]:
        if not symbols:
            return [], 0
        size = min(self.config.stock_batch_size, len(symbols))
        available = set(symbols)
        held = [
            str(item.get("symbol", "")).upper()
            for item in positions
            if item.get("instrument_type") == "EQUITY"
            and Decimal(str(item.get("quantity", "0"))) != 0
            and str(item.get("symbol", "")).upper() in available
        ]
        ranked = sorted(
            (symbol for symbol in self.activity if symbol in available),
            key=lambda symbol: self.priority_score(
                symbol,
                assessment_for(symbol),
            ),
            reverse=True,
        )
        penny = [
            symbol
            for symbol in ranked
            if self.prices.get(symbol, Decimal("Infinity"))
            < self.config.penny_stock_max_price
        ]
        popular = [
            symbol
            for symbol in ranked
            if self.prices.get(symbol, Decimal("0"))
            >= self.config.penny_stock_max_price
        ]
        exploration, cursor = self.rotating_batch(symbols, cursor, size)
        penny_count = int(size * self.config.stock_penny_fraction)
        popular_count = int(size * self.config.stock_priority_fraction)
        selected = list(
            dict.fromkeys(
                held
                + penny[:penny_count]
                + popular[:popular_count]
                + exploration
            )
        )
        return selected[:size], cursor

    def research_candidates(
        self,
        limit: int,
        excluded: set[str],
        assessment_for,
        blocked_until,
    ) -> list[dict]:
        ranked = sorted(
            self.activity,
            key=lambda symbol: self.priority_score(
                symbol,
                assessment_for(symbol),
            ),
            reverse=True,
        )
        results = []
        for symbol in ranked:
            if symbol in excluded or blocked_until(symbol):
                continue
            price = self.prices.get(symbol)
            if not price:
                continue
            results.append(
                {
                    "symbol": symbol,
                    "type": (
                        "PENNY"
                        if price < self.config.penny_stock_max_price
                        else "POPULAR_VOLATILE"
                    ),
                    "price": str(price),
                    **self.metrics.get(symbol, {}),
                }
            )
            if len(results) >= limit:
                break
        return results

    @staticmethod
    def _ema(values: list[float], period: int) -> float:
        weight = 2 / (period + 1)
        result = values[0]
        for value in values[1:]:
            result = value * weight + result * (1 - weight)
        return result

    def trend_signal(self, key: str, price: Decimal) -> str:
        values = self.history[key]
        values.append(float(price))
        slow = self.config.ema_slow_period
        fast = self.config.ema_fast_period
        if len(values) < slow + 1:
            return "HOLD"
        series = list(values)
        previous = series[:-1]
        old_spread = self._ema(previous[-slow:], fast) - self._ema(
            previous[-slow:],
            slow,
        )
        new_spread = self._ema(series[-slow:], fast) - self._ema(
            series[-slow:],
            slow,
        )
        if old_spread <= 0 < new_spread:
            return "BUY"
        if self.config.reenter_on_trend and new_spread > 0:
            return "BUY"
        return "HOLD"

    def stock_decision(
        self,
        key: str,
        price: Decimal,
        quantity: int,
        average_cost: Decimal,
    ) -> Decision:
        trend = self.trend_signal(key, price)
        if quantity > 0:
            target = average_cost * (
                Decimal("1")
                + self.config.stock_min_net_profit_percent
                + self.config.stock_estimated_round_trip_cost_percent
            )
            stop = average_cost * (
                Decimal("1") - self.config.stock_stop_loss_percent
            )
            if average_cost > 0 and price <= stop:
                return Decision("LOSS", "percentage stop reached", price)
            if average_cost > 0 and price >= target:
                return Decision("PROFIT", "percentage profit reached", target)
            return Decision("HOLD", "position between target and stop", target)
        if trend != "BUY":
            return Decision("HOLD", "EMA entry not ready")
        return Decision("BUY", "EMA entry confirmed")

    def option_decision(
        self,
        key: str,
        price: Decimal,
        quantity: int,
        average_cost: Decimal,
    ) -> Decision:
        trend = self.trend_signal(key, price)
        if quantity > 0:
            target = average_cost + self.config.option_take_profit_price
            if average_cost > 0 and price >= target:
                return Decision("PROFIT", "option profit target reached", target)
            return Decision("HOLD", "option waiting for profit", target)
        return (
            Decision("BUY", "EMA entry confirmed")
            if trend == "BUY"
            else Decision("HOLD", "EMA entry not ready")
        )

    def stock_order_quantity(
        self,
        price: Decimal,
        buying_power: Decimal,
    ) -> tuple[int, Decimal]:
        buffered_price = price * Decimal("1.03")
        affordable = int(
            (buying_power / buffered_price).to_integral_value(
                rounding=ROUND_DOWN
            )
        )
        notional_limit = int(
            (self.config.max_order_notional / price).to_integral_value(
                rounding=ROUND_DOWN
            )
        )
        return (
            min(self.config.stock_quantity, affordable, notional_limit),
            buffered_price,
        )

    def option_order_quantity(
        self,
        limit_price: Decimal,
        buying_power: Decimal,
    ) -> tuple[int, Decimal]:
        contract_cost = limit_price * 100
        affordable = int(
            (buying_power / contract_cost).to_integral_value(
                rounding=ROUND_DOWN
            )
        )
        notional_limit = int(
            (
                self.config.max_order_notional / contract_cost
            ).to_integral_value(rounding=ROUND_DOWN)
        )
        return (
            min(self.config.option_quantity, affordable, notional_limit),
            contract_cost,
        )

    @staticmethod
    def open_position_count(positions: list[dict]) -> int:
        return sum(
            1
            for item in positions
            if Decimal(str(item.get("quantity", "0"))) != 0
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

    @staticmethod
    def portfolio_decision(
        position_states: list[dict],
        minimum_losers: int,
        loss_threshold: Decimal,
    ) -> PortfolioDecision:
        losing = [
            item
            for item in position_states
            if Decimal(str(item.get("unrealized_pnl", "0"))) < 0
        ]
        if len(losing) < minimum_losers:
            return PortfolioDecision(
                "HOLD",
                "loss cluster below position threshold",
            )
        total_loss = -sum(
            Decimal(str(item["unrealized_pnl"]))
            for item in losing
        )
        if total_loss >= loss_threshold:
            return PortfolioDecision(
                "LIQUIDATE",
                "simultaneous loss threshold reached",
                len(losing),
                total_loss,
            )
        return PortfolioDecision(
            "HOLD",
            "loss cluster below liquidation threshold",
            len(losing),
            total_loss,
        )
