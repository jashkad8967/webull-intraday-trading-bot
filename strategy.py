from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal


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
    """All entry and normal-session exit policy lives here."""

    def __init__(self, fast: int, slow: int):
        if fast >= slow:
            raise ValueError("fast EMA must be lower than slow EMA")
        self.fast = fast
        self.slow = slow
        self.history = defaultdict(lambda: deque(maxlen=slow + 1))

    @staticmethod
    def _ema(values: list[float], period: int) -> float:
        weight = 2 / (period + 1)
        result = values[0]
        for value in values[1:]:
            result = value * weight + result * (1 - weight)
        return result

    def trend_signal(
        self,
        key: str,
        price: Decimal,
        reenter_on_trend: bool,
    ) -> str:
        values = self.history[key]
        values.append(float(price))
        if len(values) < self.slow + 1:
            return "HOLD"
        series = list(values)
        previous = series[:-1]
        old_spread = self._ema(previous[-self.slow :], self.fast) - self._ema(
            previous[-self.slow :], self.slow
        )
        new_spread = self._ema(series[-self.slow :], self.fast) - self._ema(
            series[-self.slow :], self.slow
        )
        if old_spread <= 0 < new_spread:
            return "BUY"
        if reenter_on_trend and new_spread > 0:
            return "BUY"
        return "HOLD"

    def decide(
        self,
        key: str,
        price: Decimal,
        quantity: int,
        average_cost: Decimal,
        take_profit: Decimal,
        reenter_on_trend: bool,
        agent_values: dict | None = None,
        agent_required: bool = False,
        agent_min_confidence: float = 0.65,
        agent_min_entry_score: float = 0.15,
        agent_max_downside_risk: float = 0.55,
        agent_max_liquidity_risk: float = 0.50,
    ) -> Decision:
        trend = self.trend_signal(key, price, reenter_on_trend)
        if quantity > 0:
            target = average_cost + take_profit
            if average_cost > 0 and price >= target:
                return Decision("PROFIT", "profit target reached", target)
            return Decision("HOLD", "position waiting for profit", target)

        if trend != "BUY":
            return Decision("HOLD", "EMA entry not ready")

        if agent_required:
            if not agent_values:
                return Decision("RESEARCH", "agent values pending")
            score = self.agent_entry_score(agent_values)
            if (
                agent_values.get("action") != "BUY"
                or float(agent_values.get("confidence", 0))
                < agent_min_confidence
                or score < agent_min_entry_score
                or float(agent_values.get("downside_risk", 1))
                > agent_max_downside_risk
                or float(agent_values.get("liquidity_risk", 1))
                > agent_max_liquidity_risk
            ):
                return Decision("RESEARCH", f"agent entry score={score:.2f}")

        return Decision("BUY", "EMA entry confirmed")

    @staticmethod
    def agent_entry_score(values: dict) -> float:
        expected_move = max(
            -1.0,
            min(1.0, float(values.get("expected_move_percent", 0)) / 5.0),
        )
        score = (
            0.25 * float(values.get("news_sentiment", 0))
            + 0.25 * float(values.get("catalyst_strength", 0))
            + 0.20 * float(values.get("market_direction", 0))
            + 0.15 * expected_move
            - 0.25 * float(values.get("downside_risk", 1))
            - 0.15 * float(values.get("liquidity_risk", 1))
        )
        return max(-1.0, min(1.0, score))

    @staticmethod
    def portfolio_decision(
        position_states: list[dict],
        minimum_losers: int,
        loss_threshold: Decimal,
        low_outlook_fraction: float,
        agent_min_confidence: float,
    ) -> PortfolioDecision:
        losing = [
            item
            for item in position_states
            if Decimal(str(item.get("unrealized_pnl", "0"))) < 0
        ]
        if len(losing) < minimum_losers:
            return PortfolioDecision("HOLD", "loss cluster below position threshold")

        total_loss = -sum(
            Decimal(str(item["unrealized_pnl"]))
            for item in losing
        )
        low_outlook = sum(
            1
            for item in losing
            if item.get("agent_action") in ("HOLD", "AVOID")
            and float(item.get("agent_confidence", 0)) >= agent_min_confidence
        )
        outlook_fraction = low_outlook / len(losing)
        severe_spree = total_loss >= loss_threshold * 2
        agent_confirmed = (
            total_loss >= loss_threshold
            and outlook_fraction >= low_outlook_fraction
        )
        if severe_spree or agent_confirmed:
            reason = (
                "severe simultaneous loss spree"
                if severe_spree
                else "clustered losses with low agent outlook"
            )
            return PortfolioDecision(
                "LIQUIDATE",
                reason,
                len(losing),
                total_loss,
            )
        return PortfolioDecision(
            "HOLD",
            "loss cluster below liquidation threshold",
            len(losing),
            total_loss,
        )
