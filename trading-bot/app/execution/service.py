from app.broker.base import Broker
from app.models.domain import OrderRequest, OrderType, Side
from app.risk.manager import RiskManager
class ExecutionService:
    def __init__(self, broker: Broker, risk: RiskManager): self.broker, self.risk = broker, risk
    async def buy(self, symbol: str, quantity: int) -> str:
        order = OrderRequest(symbol, Side.BUY, quantity, OrderType.MARKET); await self.risk.validate(order); return await self.broker.submit(order)
    async def sell(self, symbol: str, quantity: int) -> str:
        order = OrderRequest(symbol, Side.SELL, quantity, OrderType.MARKET); await self.risk.validate(order); return await self.broker.submit(order)
    async def cancel_order(self, order_id: str) -> None: await self.broker.cancel(order_id)
