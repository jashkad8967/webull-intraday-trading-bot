from decimal import Decimal
import pytest
from app.broker.paper import PaperBroker
from app.config.settings import Settings
from app.models.domain import OrderRequest, Side
from app.risk.manager import RiskManager, RiskError
@pytest.mark.asyncio
async def test_paper_buy_updates_position():
    broker=PaperBroker(Decimal("1000")); broker.set_quote("AAPL", Decimal("100"))
    await broker.submit(OrderRequest("AAPL", Side.BUY, 2))
    assert (await broker.positions())[0].quantity == 2
@pytest.mark.asyncio
async def test_risk_rejects_oversized_trade():
    broker=PaperBroker(Decimal("1000")); broker.set_quote("AAPL", Decimal("100"))
    with pytest.raises(RiskError):
        await RiskManager(broker, Settings(max_buying_power_fraction=Decimal("0.5"))).validate(OrderRequest("AAPL", Side.BUY, 6))
