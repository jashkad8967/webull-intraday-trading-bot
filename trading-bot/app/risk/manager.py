from decimal import Decimal
from app.broker.base import Broker
from app.config.settings import Settings
from app.models.domain import OrderRequest, Side
class RiskError(ValueError): pass
class RiskManager:
    def __init__(self, broker: Broker, settings: Settings):
        self.broker, self.s, self.daily_realized_loss = broker, settings, Decimal("0")
    async def validate(self, order: OrderRequest, stop_price: Decimal | None = None) -> None:
        if order.quantity <= 0: raise RiskError("Quantity must be positive")
        if self.daily_realized_loss >= self.s.max_daily_loss: raise RiskError("Daily-loss circuit breaker is open")
        positions = await self.broker.positions()
        if order.side is Side.BUY and order.symbol not in {p.symbol for p in positions} and len(positions) >= self.s.max_open_positions: raise RiskError("Maximum open positions reached")
        quote = await self.broker.quote(order.symbol); cost = quote.last * order.quantity; bp = await self.broker.buying_power()
        if order.side is Side.BUY and cost > bp * self.s.max_buying_power_fraction: raise RiskError("Buying-power limit exceeded")
        if stop_price is not None and order.side is Side.BUY:
            risk = (quote.last - stop_price) * order.quantity
            if risk <= 0 or risk > bp * self.s.max_risk_per_trade: raise RiskError("Invalid or excessive stop-loss risk")
