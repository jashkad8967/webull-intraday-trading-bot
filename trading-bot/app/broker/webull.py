"""Official Webull OpenAPI SDK adapter.

The SDK signs every request with the App Secret; do not send the secret as an
HTTP header or commit it. Start with api.sandbox.webull.com, then explicitly
change the environment only after integration tests and operator approval.
"""
import asyncio
from decimal import Decimal
from uuid import uuid4
from app.broker.base import Broker
from app.config.settings import Settings
from app.models.domain import OrderRequest, OrderType, Position, Quote, Side

class WebullBroker(Broker):
    def __init__(self, settings: Settings):
        self.s = settings
        if not settings.account_id:
            raise ValueError("ACCOUNT_ID is required for Webull mode")
        try:
            from webull.core.client import ApiClient
            from webull.trade.trade_client import TradeClient
            from webull.data.data_client import DataClient
        except ImportError as exc:
            raise RuntimeError("Install webull-openapi-python-sdk before using Webull mode") from exc
        api_client = ApiClient(settings.webull_app_key, settings.webull_app_secret, settings.webull_environment)
        api_client.add_endpoint(settings.webull_environment, settings.webull_api_endpoint)
        self.trade, self.data = TradeClient(api_client), DataClient(api_client)

    @staticmethod
    def _body(response):
        if response.status_code not in (200, 201):
            raise RuntimeError(f"Webull API error {response.status_code}: {response.text}")
        return response.json()

    async def buying_power(self) -> Decimal:
        data = await asyncio.to_thread(lambda: self._body(self.trade.account_v2.get_account_balance(self.s.account_id)))
        # Webull responses preserve decimal numbers as strings; tolerate envelope variations.
        payload = data.get("data", data) if isinstance(data, dict) else data
        return Decimal(str(payload.get("buying_power", payload.get("buyingPower"))))

    async def positions(self) -> list[Position]:
        data = await asyncio.to_thread(lambda: self._body(self.trade.account_v2.get_account_position(self.s.account_id)))
        payload = data.get("data", data) if isinstance(data, dict) else data
        return [Position(p["symbol"], int(Decimal(str(p.get("quantity", p.get("position_qty", 0))))), Decimal(str(p.get("average_cost", p.get("avg_cost", 0))))) for p in payload]

    async def quote(self, symbol: str) -> Quote:
        from webull.data.common.category import Category
        result = await asyncio.to_thread(lambda: self._body(self.data.market_data.get_stock_snapshot([symbol], Category.US_STOCK.name, False, False)))
        item = result[0] if isinstance(result, list) else result.get("data", result)[0]
        return Quote(symbol, Decimal(str(item["price"])))

    async def submit(self, order: OrderRequest) -> str:
        if self.s.mode != "LIVE": raise RuntimeError("Webull submit requires MODE=LIVE")
        if order.order_type is OrderType.MARKET: webull_type = "MARKET"
        elif order.order_type is OrderType.LIMIT: webull_type = "LIMIT"
        elif order.order_type is OrderType.STOP: webull_type = "STOP_LOSS"
        else: raise ValueError("Unsupported order type")
        payload = {"combo_type":"NORMAL", "client_order_id":uuid4().hex, "symbol":order.symbol,
          "instrument_type":"EQUITY", "market":"US", "order_type":webull_type,
          "quantity":str(order.quantity), "support_trading_session":"CORE", "side":order.side.value,
          "time_in_force":"DAY", "entrust_type":"QTY"}
        if order.limit_price is not None: payload["limit_price"] = str(order.limit_price)
        if order.stop_price is not None: payload["stop_price"] = str(order.stop_price)
        response = await asyncio.to_thread(lambda: self.trade.order_v3.place_order(self.s.account_id, [payload]))
        self._body(response); return payload["client_order_id"]

    async def cancel(self, order_id: str) -> None:
        if self.s.mode != "LIVE": raise RuntimeError("Webull cancellation requires MODE=LIVE")
        await asyncio.to_thread(lambda: self._body(self.trade.order_v3.cancel_order(self.s.account_id, order_id)))

    async def submit_option(self, order: dict) -> str:
        """Submit a validated Webull OPTION payload (SINGLE or supported multi-leg)."""
        if self.s.mode != "LIVE" or not self.s.options_enabled: raise RuntimeError("Options require MODE=LIVE and OPTIONS_ENABLED=true")
        order.setdefault("client_order_id", uuid4().hex); order["instrument_type"] = "OPTION"; order["market"] = "US"
        await asyncio.to_thread(lambda: self._body(self.trade.order_v3.place_order(self.s.account_id, [order])))
        return order["client_order_id"]
