from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
class Signal(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
@dataclass(frozen=True)
class Quote:
    symbol: str
    last: Decimal
    bid: Decimal | None = None
    ask: Decimal | None = None
@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: Side
    quantity: int
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: int
    average_cost: Decimal
