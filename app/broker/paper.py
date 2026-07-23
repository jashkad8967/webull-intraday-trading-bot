from decimal import Decimal
from uuid import uuid4
from app.broker.base import Broker
from app.models.domain import OrderRequest, Position, Quote, Side
class PaperBroker(Broker):
    """Local simulator; inject quotes before submitting orders."""
    def __init__(self, cash: Decimal):
        self.cash, self._quotes, self._positions, self.orders = cash, {}, {}, {}
    def set_quote(self, symbol: str, price: Decimal) -> None:
        self._quotes[symbol] = Quote(symbol, price, price, price)
    async def buying_power(self) -> Decimal: return self.cash
    async def positions(self) -> list[Position]: return list(self._positions.values())
    async def quote(self, symbol: str) -> Quote:
        if symbol not in self._quotes: raise ValueError(f"No paper quote for {symbol}")
        return self._quotes[symbol]
    async def submit(self, order: OrderRequest) -> str:
        q = await self.quote(order.symbol); price = order.limit_price or q.last; value = price * order.quantity
        pos = self._positions.get(order.symbol, Position(order.symbol, 0, Decimal("0")))
        if order.side is Side.BUY:
            if value > self.cash: raise ValueError("Insufficient paper buying power")
            qty = pos.quantity + order.quantity
            avg = ((pos.average_cost * pos.quantity) + value) / qty
            self.cash -= value; self._positions[order.symbol] = Position(order.symbol, qty, avg)
        else:
            if order.quantity > pos.quantity: raise ValueError("Paper short selling is disabled")
            qty = pos.quantity - order.quantity; self.cash += value
            if qty: self._positions[order.symbol] = Position(order.symbol, qty, pos.average_cost)
            else: self._positions.pop(order.symbol, None)
        oid = str(uuid4()); self.orders[oid] = {"status": "FILLED", "order": order, "price": str(price)}; return oid
    async def cancel(self, order_id: str) -> None:
        raise ValueError("Paper orders fill immediately")
