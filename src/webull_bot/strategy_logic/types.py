"""Shared dataclasses used by TradingStrategy and its extracted
strategy_logic/** helper modules. Kept in a separate module (rather than
in strategy.py itself) so extracted modules can import these types
without creating a circular import back into strategy.py, which itself
imports from strategy_logic/**.
"""
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
