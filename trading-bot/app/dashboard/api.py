from decimal import Decimal
from fastapi import FastAPI, HTTPException
from app.broker.paper import PaperBroker
from app.broker.webull import WebullBroker
from app.config.settings import get_settings
from app.execution.service import ExecutionService
from app.risk.manager import RiskManager
settings = get_settings()
broker = PaperBroker(settings.paper_starting_cash) if settings.mode == "PAPER" else WebullBroker(settings)
service = ExecutionService(broker, RiskManager(broker, settings))
app = FastAPI(title="Trading Bot", version="0.1.0")
@app.get("/health")
async def health(): return {"status": "ok", "mode": settings.mode, "live_ready": settings.live_ready()}
@app.get("/account")
async def account(): return {"buying_power": str(await broker.buying_power()), "positions": [p.__dict__ for p in await broker.positions()]}
@app.post("/paper/quote/{symbol}/{price}")
async def set_paper_quote(symbol: str, price: Decimal):
    if not isinstance(broker, PaperBroker): raise HTTPException(403, "Paper endpoint unavailable")
    broker.set_quote(symbol.upper(), price); return {"ok": True}
@app.post("/orders/{side}/{symbol}/{quantity}")
async def place(side: str, symbol: str, quantity: int):
    if side.lower() not in {"buy", "sell"}: raise HTTPException(422, "side must be buy or sell")
    try: return {"order_id": await (service.buy(symbol.upper(), quantity) if side.lower() == "buy" else service.sell(symbol.upper(), quantity))}
    except (ValueError, RuntimeError) as exc: raise HTTPException(422, str(exc))
