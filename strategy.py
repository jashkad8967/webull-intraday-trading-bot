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
        self.selection_buckets: dict[str, str] = {}

    def clear_market_state(self) -> None:
        self.activity.clear()
        self.prices.clear()
        self.metrics.clear()
        self.selection_buckets.clear()

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
        quick_trade_score = float(assessment.get("quick_trade_score", 0))
        symbol_volatility = float(assessment.get("symbol_volatility", 0))
        expected_move = min(
            1.0,
            abs(float(assessment.get("expected_move_percent", 0))) / 5.0,
        )
        catalyst = abs(float(assessment.get("catalyst_strength", 0)))
        volatility = float(assessment.get("market_volatility", 0))
        return score + 2.0 * confidence * (
            research_priority
            + 1.5 * quick_trade_score
            + 1.25 * symbol_volatility
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
        research_symbols: set[str] | None = None,
    ) -> tuple[list[str], int]:
        if not symbols:
            return [], 0
        size = min(self.config.stock_batch_size, len(symbols))
        available = set(symbols)
        research_symbols = (research_symbols or set()) & available
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
        liquid_popular = [
            symbol
            for symbol in ranked
            if self.prices.get(symbol, Decimal("0"))
            >= self.config.penny_stock_max_price
            and self.metrics.get(symbol, {}).get("volume", 0)
            >= self.config.popular_stock_min_volume
            and Decimal(
                str(self.metrics.get(symbol, {}).get("spread_percent", "999"))
            )
            <= self.config.popular_stock_max_spread_percent
        ]
        researched = sorted(
            research_symbols,
            key=lambda symbol: self.priority_score(
                symbol,
                assessment_for(symbol),
            ),
            reverse=True,
        )
        popular = list(dict.fromkeys(researched + liquid_popular))
        exploration, cursor = self.rotating_batch(symbols, cursor, size)
        penny_count = int(size * self.config.stock_penny_fraction)
        popular_count = int(size * self.config.stock_priority_fraction)
        popular_selected = popular[:popular_count]
        penny_selected = penny[:penny_count]
        selected = list(
            dict.fromkeys(
                held
                + popular_selected
                + penny_selected
                + exploration
            )
        )
        selected = selected[:size]
        self.selection_buckets = {}
        held_set = set(held)
        popular_set = set(popular_selected)
        penny_set = set(penny_selected)
        for symbol in selected:
            if symbol in held_set:
                self.selection_buckets[symbol] = "HELD"
            elif symbol in popular_set:
                self.selection_buckets[symbol] = "POPULAR"
            elif symbol in penny_set:
                self.selection_buckets[symbol] = "PENNY"
            else:
                self.selection_buckets[symbol] = "DISCOVERY"
        return selected, cursor

    def selection_bucket(self, symbol: str) -> str:
        return self.selection_buckets.get(symbol, "DISCOVERY")

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
        assessment: dict | None = None,
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
        if not self.entry_spread_ok(key):
            return Decision("HOLD", "spread too wide to scalp profitably")
        if trend == "BUY":
            return Decision("BUY", "EMA entry confirmed")
        if self.research_supports_entry(assessment):
            return Decision(
                "BUY",
                "strong liquid short-horizon research setup",
            )
        return Decision("HOLD", "EMA entry not ready")

    def entry_spread_ok(self, key: str) -> bool:
        symbol = key.split(":", 1)[-1]
        spread = self.metrics.get(symbol, {}).get("spread_percent")
        if spread in (None, ""):
            return True
        try:
            return Decimal(str(spread)) <= self.config.stock_entry_max_spread_percent
        except Exception:
            return True

    @staticmethod
    def research_supports_entry(assessment: dict | None) -> bool:
        if not assessment:
            return False
        return (
            float(assessment.get("confidence", 0)) >= 0.70
            and float(assessment.get("quick_trade_score", 0)) >= 0.70
            and float(assessment.get("symbol_volatility", 0)) >= 0.60
            and float(assessment.get("expected_move_percent", 0)) > 0
            and float(assessment.get("catalyst_strength", 0)) > 0
            and float(assessment.get("liquidity_risk", 1)) <= 0.40
            and float(assessment.get("downside_risk", 1)) <= 0.50
            and int(assessment.get("horizon_minutes", 390)) <= 30
        )

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
