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
        self.trend_streak: dict[str, int] = {}
        self.vwap_state: dict[str, dict] = {}
        self.crossover_counts: dict[str, int] = defaultdict(int)
        self.micro_scalp_reference: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=config.micro_scalp_reference_window)
        )
        self.tick_history: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=config.tick_direction_window)
        )

    def clear_market_state(self) -> None:
        self.activity.clear()
        self.prices.clear()
        self.metrics.clear()
        self.selection_buckets.clear()
        self.vwap_state.clear()
        self.crossover_counts.clear()
        self.micro_scalp_reference.clear()
        self.tick_history.clear()

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
            "range_ratio": round(max(0.0, range_ratio), 4),
            "high": high,
            "activity_score": activity,
        }
        self._update_vwap(symbol, price, volume)
        self.micro_scalp_reference[symbol].append(float(price))

    def micro_scalp_reference_price(self, symbol: str) -> Decimal | None:
        """A short rolling average of recent quote prices - the "just been
        trading around here" level a dip-buy measures itself against, not a
        trend signal.
        """
        window = self.micro_scalp_reference.get(symbol)
        if not window:
            return None
        return Decimal(str(sum(window) / len(window)))

    def micro_scalp_decision(
        self,
        key: str,
        price: Decimal,
        quantity: int,
        average_cost: Decimal,
    ) -> Decision:
        """A fixed-cents mean-reversion scalp for a small list of always-
        ticking, ultra-liquid symbols: buy a small dip below the recent
        average and take a small fixed profit on the bounce back, instead of
        requiring an EMA trend agreement that rarely holds tick-to-tick.
        """
        symbol = key.split(":", 1)[-1]
        if quantity > 0:
            fee_per_share = self.config.sell_fee_dollars / quantity
            target = average_cost + self.config.micro_scalp_target_cents + fee_per_share
            stop = average_cost - self.config.micro_scalp_stop_cents
            if price <= stop:
                return Decision("LOSS", "micro-scalp stop reached", price)
            if price >= target:
                return Decision("PROFIT", "micro-scalp target reached", target)
            return Decision("HOLD", "micro-scalp waiting for bounce", target)
        if not self.entry_spread_ok(key):
            return Decision("HOLD", "spread too wide to scalp profitably")
        reference = self.micro_scalp_reference_price(symbol)
        if reference is None:
            return Decision("HOLD", "insufficient reference history")
        if price <= reference - self.config.micro_scalp_dip_cents:
            return Decision("BUY", "micro-scalp dip entry")
        return Decision("HOLD", "no qualifying dip yet")

    def _update_vwap(self, symbol: str, price: Decimal, volume: float) -> None:
        state = self.vwap_state.get(symbol)
        if state is None:
            self.vwap_state[symbol] = {
                "cum_pv": 0.0,
                "cum_vol": 0.0,
                "last_volume": volume,
            }
            return
        delta = max(0.0, volume - state["last_volume"])
        if delta > 0:
            state["cum_pv"] += float(price) * delta
            state["cum_vol"] += delta
        state["last_volume"] = volume

    def vwap(self, symbol: str) -> Decimal | None:
        state = self.vwap_state.get(symbol)
        if not state or state["cum_vol"] <= 0:
            return None
        return Decimal(str(state["cum_pv"] / state["cum_vol"]))

    def vwap_supports_entry(self, symbol: str, price: Decimal) -> bool:
        vwap = self.vwap(symbol)
        if vwap is None:
            return True
        band = Decimal("1") - self.config.vwap_entry_band_percent
        return price >= vwap * band

    def priority_score(self, symbol: str, assessment: dict | None) -> float:
        score = self.activity.get(symbol, 0.0)
        # Reward symbols with a recurring back-and-forth pattern today (a
        # capped count of EMA direction flips) so the scanner keeps favoring
        # stocks that repeatedly create fresh scalp setups over ones that
        # already made their one move for the day.
        oscillation = min(20, self.crossover_counts.get(symbol, 0))
        score += oscillation * float(self.config.stock_oscillation_weight)
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
        penny_count = int(size * self.config.stock_penny_fraction)
        popular_count = int(size * self.config.stock_priority_fraction)
        popular_selected = popular[:popular_count]
        penny_selected = penny[:penny_count]
        # Reserve a guaranteed slice of every batch for fresh exploration so
        # the scanner keeps paging the whole universe instead of re-scanning
        # the same top-ranked names each cycle.
        explore_floor = max(1, size - popular_count - penny_count)
        priority = list(dict.fromkeys(held + popular_selected + penny_selected))
        priority = priority[: size - explore_floor]
        # Request exactly the open exploration slots, skipping symbols already
        # in the priority slice so exploration always keeps paging forward
        # through fresh names instead of re-picking (and then discarding)
        # names the priority slice already covered.
        exploration_slots = max(0, size - len(priority))
        priority_set = set(priority)
        exploration: list[str] = []
        attempts = 0
        while len(exploration) < exploration_slots and attempts < len(symbols):
            probe, cursor = self.rotating_batch(symbols, cursor, 1)
            attempts += 1
            if probe and probe[0] not in priority_set:
                exploration.append(probe[0])
        selected = list(dict.fromkeys(priority + exploration))
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

    def prioritized_stock_batch_by_market_cap(
        self,
        symbols: list[str],
        cursor: int,
        positions: list[dict],
        assessment_for,
        market_values: dict[str, float],
        large_cap_min_value: Decimal,
        large_cap_fraction: Decimal,
        research_symbols: set[str] | None = None,
    ) -> tuple[list[str], int]:
        """Same shape as prioritized_stock_batch, but tiers by market cap
        (LARGE_CAP/SMALL_CAP) instead of price-based penny/popular buckets.
        """
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
            key=lambda symbol: self.priority_score(symbol, assessment_for(symbol)),
            reverse=True,
        )
        threshold = float(large_cap_min_value)
        is_large = lambda symbol: market_values.get(symbol, 0.0) >= threshold
        large_cap = [symbol for symbol in ranked if is_large(symbol)]
        small_cap = [symbol for symbol in ranked if not is_large(symbol)]
        researched = sorted(
            research_symbols,
            key=lambda symbol: self.priority_score(symbol, assessment_for(symbol)),
            reverse=True,
        )
        large_cap = list(
            dict.fromkeys([s for s in researched if is_large(s)] + large_cap)
        )
        small_cap = list(
            dict.fromkeys([s for s in researched if not is_large(s)] + small_cap)
        )
        exploration, cursor = self.rotating_batch(symbols, cursor, size)
        large_count = int(size * large_cap_fraction)
        small_count = size - large_count
        large_selected = large_cap[:large_count]
        small_selected = small_cap[:small_count]
        selected = list(
            dict.fromkeys(held + large_selected + small_selected + exploration)
        )
        selected = selected[:size]
        self.selection_buckets = {}
        held_set = set(held)
        large_set = set(large_selected)
        small_set = set(small_selected)
        for symbol in selected:
            if symbol in held_set:
                self.selection_buckets[symbol] = "HELD"
            elif symbol in large_set:
                self.selection_buckets[symbol] = "LARGE_CAP"
            elif symbol in small_set:
                self.selection_buckets[symbol] = "SMALL_CAP"
            else:
                self.selection_buckets[symbol] = (
                    "LARGE_CAP" if is_large(symbol) else "SMALL_CAP"
                )
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
        self.tick_history[key].append(float(price))
        slow = self.config.ema_slow_period
        fast = self.config.ema_fast_period
        if len(values) < slow + 1:
            self.trend_streak[key] = 0
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
        if (old_spread > 0) != (new_spread > 0):
            # A stock that keeps flipping direction is exactly the kind of
            # choppy, mean-reverting mover that produces repeated small
            # scalps in a session - track it so priority_score can favor it
            # over a name that only trended once and went flat.
            symbol = key.split(":", 1)[-1]
            self.crossover_counts[symbol] += 1
        if new_spread <= 0:
            self.trend_streak[key] = 0
            return "HOLD"
        if old_spread <= 0:
            self.trend_streak[key] = 0
            return "BUY"
        self.trend_streak[key] = self.trend_streak.get(key, 0) + 1
        if (
            self.config.reenter_on_trend
            and self.trend_streak[key] >= self.config.reenter_confirmation_polls
        ):
            return "BUY"
        return "HOLD"

    def tick_direction_score(self, key: str) -> Decimal:
        """Net upticks vs downticks over the recent poll-to-poll price
        prints, as a proxy for order-flow imbalance - real bid/ask depth
        isn't available from the quote feed. Ranges -1 (all downticks) to
        +1 (all upticks); 0 when there's too little data or no net
        direction (flat prints, or an equal mix of up/down).
        """
        values = list(self.tick_history.get(key, ()))
        if len(values) < 2:
            return Decimal("0")
        up = down = 0
        for previous, current in zip(values, values[1:]):
            if current > previous:
                up += 1
            elif current < previous:
                down += 1
        total = up + down
        if total == 0:
            return Decimal("0")
        return Decimal(up - down) / Decimal(total)

    def tick_direction_ok(self, key: str) -> bool:
        if not self.config.tick_direction_enabled:
            return True
        return self.tick_direction_score(key) >= self.config.tick_direction_veto_threshold

    def adaptive_stop_percent(self, symbol: str) -> Decimal:
        range_ratio = Decimal(str(self.metrics.get(symbol, {}).get("range_ratio", 0)))
        scaled = range_ratio * self.config.stock_stop_loss_range_multiplier
        return max(
            self.config.stock_stop_loss_min_percent,
            min(self.config.stock_stop_loss_max_percent, scaled),
        )

    def stock_decision(
        self,
        key: str,
        price: Decimal,
        quantity: int,
        average_cost: Decimal,
        assessment: dict | None = None,
        opening_grace_active: bool = False,
    ) -> Decision:
        trend = self.trend_signal(key, price)
        symbol = key.split(":", 1)[-1]
        if quantity > 0:
            # The flat per-sell fee doesn't scale with share count, so it
            # has to be converted to a per-share amount before it can be
            # added to a per-share target/breakeven price - otherwise a
            # target hit at exactly the percentage-based price would still
            # net a loss once the flat fee comes out of the actual fill.
            fee_per_share = self.config.sell_fee_dollars / quantity
            stop_percent = self.adaptive_stop_percent(symbol)
            target_percent = max(
                self.config.stock_min_net_profit_percent
                + self.config.stock_estimated_round_trip_cost_percent,
                stop_percent * self.config.stock_target_stop_multiple,
            )
            base_target = average_cost * (Decimal("1") + target_percent) + fee_per_share
            stop = average_cost * (Decimal("1") - stop_percent)
            if average_cost > 0 and price <= stop:
                return Decision("LOSS", "percentage stop reached", price)
            bias = self._exit_bias(assessment)
            target = base_target
            if (
                self.config.agent_exit_influence_enabled
                and average_cost > 0
                and bias >= self.config.agent_runner_bias_threshold
            ):
                target = max(
                    base_target,
                    average_cost * (Decimal("1") + self.config.agent_runner_profit_percent)
                    + fee_per_share,
                )
            if average_cost > 0 and price >= target:
                reason = (
                    "agent runner target reached"
                    if target > base_target
                    else "percentage profit reached"
                )
                return Decision("PROFIT", reason, target)
            if (
                self.config.agent_exit_influence_enabled
                and average_cost > 0
                and bias <= self.config.agent_derisk_bias_threshold
            ):
                breakeven = average_cost * (
                    Decimal("1")
                    + self.config.stock_estimated_round_trip_cost_percent
                ) + fee_per_share
                if price >= breakeven:
                    return Decision(
                        "PROFIT",
                        "agent de-risk lock-in on fading catalyst",
                        breakeven,
                    )
            return Decision("HOLD", "position between target and stop", target)
        if not self.entry_spread_ok(key, opening_grace_active):
            return Decision("HOLD", "spread too wide to scalp profitably")
        if not self.vwap_supports_entry(symbol, price):
            return Decision("HOLD", "price below session VWAP")
        if not self.entry_extension_ok(symbol, price, opening_grace_active):
            return Decision("HOLD", "price already extended near today's high")
        if trend == "BUY":
            if self.tick_direction_ok(key):
                return Decision("BUY", "EMA entry confirmed")
            return Decision(
                "HOLD", "recent ticks trending against the EMA entry"
            )
        if self.research_supports_entry(assessment):
            return Decision(
                "BUY",
                "strong liquid short-horizon research setup",
            )
        return Decision("HOLD", "EMA entry not ready")

    def _exit_bias(self, assessment: dict | None) -> Decimal:
        if not assessment:
            return Decimal("0")
        try:
            confidence = Decimal(str(assessment.get("confidence", 0)))
            bias = Decimal(str(assessment.get("exit_bias", 0)))
        except Exception:
            return Decimal("0")
        if confidence < self.config.agent_exit_min_confidence:
            return Decimal("0")
        return max(Decimal("-1"), min(Decimal("1"), bias))

    def entry_spread_ok(self, key: str, opening_grace_active: bool = False) -> bool:
        symbol = key.split(":", 1)[-1]
        spread = self.metrics.get(symbol, {}).get("spread_percent")
        if spread in (None, ""):
            return True
        threshold = self.config.stock_entry_max_spread_percent
        if opening_grace_active:
            threshold *= self.config.opening_grace_spread_multiplier
        try:
            return Decimal(str(spread)) <= threshold
        except Exception:
            return True

    def entry_extension_ok(
        self,
        symbol: str,
        price: Decimal,
        opening_grace_active: bool = False,
    ) -> bool:
        """Block chasing a name that's already sitting at today's high.

        A crossover that only confirms once price is already at the peak
        of a fast spike is buying the top, not the move - require some
        room below today's high before allowing a fresh entry. Right after
        the open, today's high is barely established yet and gets set/reset
        constantly, so the grace window shrinks the room required (smaller
        buffer = more lenient) instead of dropping the check entirely.
        """
        high = self.metrics.get(symbol, {}).get("high")
        if not high:
            return True
        try:
            high_decimal = Decimal(str(high))
        except Exception:
            return True
        if high_decimal <= 0:
            return True
        extension_percent = self.config.stock_entry_max_extension_percent
        if opening_grace_active:
            extension_percent /= self.config.opening_grace_extension_multiplier
        return price <= high_decimal * (Decimal("1") - extension_percent)

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
            # Same flat-fee-to-per-share conversion as stock_decision, but
            # one contract represents 100 shares, so the fee is spread over
            # quantity * 100, not quantity alone.
            fee_per_share = self.config.sell_fee_dollars / (quantity * 100)
            target = average_cost + self.config.option_take_profit_price + fee_per_share
            stop = average_cost * (
                Decimal("1") - self.config.option_stop_loss_percent
            )
            if average_cost > 0 and price <= stop:
                return Decision("LOSS", "option percentage stop reached", price)
            if average_cost > 0 and price >= target:
                return Decision("PROFIT", "option profit target reached", target)
            return Decision("HOLD", "option waiting for profit", target)
        return (
            Decision("BUY", "EMA entry confirmed")
            if trend == "BUY"
            else Decision("HOLD", "EMA entry not ready")
        )

    @staticmethod
    def minimum_lot_size(price: Decimal) -> int:
        """Webull rejects any order under 100 shares outright for stocks
        priced $0.10-$0.999 (OAUTH_OPENAPI_CANT_TRADE_FOR_PRICE_BETWEEN_
        0099_AND_0999) - a plain per-order STOCK_QUANTITY of 1 would fail
        every single time in that band, not just occasionally.
        """
        if Decimal("0.10") <= price <= Decimal("0.999"):
            return 100
        return 1

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
        quantity = min(self.config.stock_quantity, affordable, notional_limit)
        min_lot = self.minimum_lot_size(price)
        if quantity < min_lot:
            quantity = (
                min_lot
                if min_lot <= affordable and min_lot <= notional_limit
                else 0
            )
        return quantity, buffered_price

    def fractional_stock_quantity(
        self,
        price: Decimal,
        buying_power: Decimal,
    ) -> Decimal:
        """Fallback sizing for when buying_power can't afford even one whole
        share: Webull's fractional orders are quantity-capped to (0, 1] and
        must clear a minimum order value, so this returns Decimal("0") -
        meaning "skip, don't place a fractional order" - whenever either
        constraint can't be met, rather than rounding into an invalid order.

        A stock priced $0.10-$0.999 needs a 100-share minimum order size
        (see minimum_lot_size) that no fractional order (always <= 1 share)
        can ever satisfy, so fractional sizing is skipped entirely there.
        """
        if price <= 0 or self.minimum_lot_size(price) > 1:
            return Decimal("0")
        min_notional = self.config.fractional_shares_min_notional
        affordable_notional = min(buying_power, price)
        if affordable_notional < min_notional:
            return Decimal("0")
        quantity = (affordable_notional / price).quantize(
            Decimal("0.0001"),
            rounding=ROUND_DOWN,
        )
        quantity = min(quantity, Decimal("1"))
        if quantity <= 0 or quantity * price < min_notional:
            return Decimal("0")
        return quantity

    def dollar_stock_quantity(
        self,
        price: Decimal,
        target_notional: Decimal,
    ) -> tuple[Decimal, Decimal]:
        """Core-session entry sizing: convert a dollar budget directly into a
        decimal share quantity for a fractional MARKET order, instead of
        picking a share count first and checking whether it's affordable.
        Unlike fractional_stock_quantity, this is not capped at one share -
        Webull's QTY-type fractional orders accept decimal quantities above 1
        (only its separate, unused AMOUNT order type is capped under one
        share's price). Skips the $0.10-$0.999 lot-restricted band entirely
        (see minimum_lot_size) since Webull requires a 100-share lot there
        that no decimal-quantity order can satisfy.
        """
        buffered_price = price * Decimal("1.03")
        if price <= 0 or self.minimum_lot_size(price) > 1:
            return Decimal("0"), buffered_price
        if target_notional < self.config.fractional_shares_min_notional:
            return Decimal("0"), buffered_price
        quantity = (target_notional / buffered_price).quantize(
            Decimal("0.0001"), rounding=ROUND_DOWN
        )
        if quantity <= 0:
            return Decimal("0"), buffered_price
        return quantity, buffered_price

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

    def position_unrealized_pnl(self, position: dict) -> Decimal:
        """Net of the flat sell fee this position hasn't paid yet - it's
        still open, but closing it will cost that fee, so showing the raw
        pre-fee mark-to-market number would overstate what selling right
        now actually nets.
        """
        reported = position.get("unrealized_profit_loss")
        if reported not in (None, ""):
            try:
                return Decimal(str(reported)) - self.config.sell_fee_dollars
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
            return (price - cost) * quantity * multiplier - self.config.sell_fee_dollars
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
