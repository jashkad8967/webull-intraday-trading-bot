"""End-of-day guardrail.

Call `flatten()` from a trusted scheduler after the configured cutoff. The
function is deliberately broker-neutral and will only operate when the live
broker's official closing-order implementation has been enabled.
"""
from app.broker.base import Broker
from app.models.domain import OrderRequest, Side
from app.risk.manager import RiskManager

class EndOfDayFlattener:
    def __init__(self, broker: Broker, risk: RiskManager):
        self.broker, self.risk = broker, risk
    async def flatten(self) -> list[str]:
        order_ids: list[str] = []
        for position in await self.broker.positions():
            if position.quantity <= 0:
                continue
            order = OrderRequest(position.symbol, Side.SELL, position.quantity)
            await self.risk.validate(order)
            order_ids.append(await self.broker.submit(order))
        return order_ids
