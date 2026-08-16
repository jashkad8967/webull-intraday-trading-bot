import json
import logging
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from webull_bot.config import Settings
from webull_bot.webull_api import WebullAPI

log = logging.getLogger("webull-bot")


class PaperWebullAPI(WebullAPI):
    """Simulates every account/trading method against an in-memory ledger
    instead of Webull's trade API - market-data methods (quotes, screener,
    depth, historical bars, option chains, and every pricing/parsing
    helper) are inherited from WebullAPI unchanged, since those are
    read-only and safe to share with the live bot's same credentials, and
    paper mode needs real prices for a meaningful test. This class never
    calls self.trade.* anywhere - real order submission and every other
    trade-API method are overridden here to operate purely on local state.

    Fills happen immediately at submission time (no partial fills, no
    working/resting orders) - open_orders() always returns empty, so
    AutoTrader.monitor_working_orders() treats every order as filled on
    its very next poll, matching this simulation's semantics.
    """

    def __init__(self, config: Settings, market_data: WebullAPI | None = None):
        # market_data is accepted for API-documentation clarity (the paper
        # broker "wraps" a market-data source) but WebullAPI.__init__ is
        # cheap to call again here (client construction, no network I/O) -
        # inheriting directly keeps every market-data/pricing helper method
        # working unmodified without needing to proxy each one by hand.
        super().__init__(config)
        self._state_path = Path(config.paper_state_file)
        self._cash = Decimal("0")
        self._positions: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._cash = self.config.paper_starting_balance
            self._save()
            return
        except Exception as exc:
            log.warning(
                "PAPER  | state read failed - starting fresh at $%s | %s",
                self.config.paper_starting_balance,
                exc,
            )
            self._cash = self.config.paper_starting_balance
            return
        try:
            self._cash = Decimal(str(payload["cash"]))
            self._positions = {
                str(item["symbol"]): item for item in payload.get("positions", [])
            }
        except (KeyError, ArithmeticError, ValueError, TypeError):
            self._cash = self.config.paper_starting_balance
            self._positions = {}

    def _save(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps(
                    {
                        "cash": str(self._cash),
                        "positions": list(self._positions.values()),
                    }
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            log.warning("PAPER  | state write failed | %s", exc)

    def buying_power(self) -> Decimal:
        return self._cash

    def positions(self) -> list[dict]:
        return [dict(item) for item in self._positions.values()]

    def _apply_fill(
        self,
        symbol: str,
        instrument_type: str,
        side: str,
        quantity: Decimal,
        fill_price: Decimal,
    ) -> None:
        """Average-cost position accounting: BUY always adds to quantity,
        SELL/SHORT always subtract (SHORT opens/extends a negative
        position the same way SELL closes/reduces a positive one - both
        are "sell" price-wise). Reducing/closing keeps the existing cost
        basis; opening fresh or adding to a same-direction position
        recomputes the weighted average; a reversal through zero resets
        the basis for the remainder, matching how a real broker treats a
        reversal as close-then-open.
        """
        delta = quantity if side == "BUY" else -quantity
        fee = self.config.sell_fee_dollars if side != "BUY" else Decimal("0")
        existing = self._positions.get(symbol)
        old_quantity = Decimal(str(existing["quantity"])) if existing else Decimal("0")
        old_cost = Decimal(str(existing["cost_price"])) if existing else Decimal("0")
        new_quantity = old_quantity + delta
        if old_quantity == 0 or (old_quantity > 0) == (delta > 0):
            new_cost = (
                (old_quantity * old_cost + delta * fill_price) / new_quantity
                if new_quantity != 0
                else Decimal("0")
            )
        elif (old_quantity > 0 > new_quantity) or (old_quantity < 0 < new_quantity):
            new_cost = fill_price
        else:
            new_cost = old_cost
        multiplier = Decimal("100") if instrument_type == "OPTION" else Decimal("1")
        self._cash = max(
            Decimal("0"), self._cash - delta * fill_price * multiplier - fee
        )
        if new_quantity == 0:
            self._positions.pop(symbol, None)
        else:
            self._positions[symbol] = {
                "instrument_type": instrument_type,
                "symbol": symbol,
                "quantity": str(new_quantity),
                "cost_price": str(new_cost),
            }
        self._save()

    def place_stock(
        self,
        symbol: str,
        side: str,
        quantity: int | Decimal,
        limit_price: Decimal | None = None,
        fractional: bool = False,
        market: bool = False,
    ) -> str:
        quantity_decimal = Decimal(str(quantity))
        fill_price = limit_price
        if fill_price is None:
            fill_price = self.quote_price(self.stock_quote(symbol))
        if side == "BUY":
            cost = fill_price * quantity_decimal
            if cost > self._cash:
                raise RuntimeError(
                    f"BUYING_POWER_INSUFFICIENT: simulated cash ${self._cash} "
                    f"can't cover ${cost} for {quantity_decimal} {symbol}"
                )
        self._apply_fill(symbol, "EQUITY", side, quantity_decimal, fill_price)
        log.info(
            "PAPER  | simulated fill | STOCK | %-8s | %-5s | qty=%s @ $%s",
            symbol,
            side,
            quantity_decimal,
            fill_price,
        )
        return uuid4().hex

    def place_option(
        self,
        contract: dict,
        side: str,
        quantity: int,
        limit_price: Decimal,
        position_intent: str,
    ) -> str:
        symbol = contract["symbol"]
        quantity_decimal = Decimal(str(quantity))
        if side == "BUY":
            cost = limit_price * quantity_decimal * Decimal("100")
            if cost > self._cash:
                raise RuntimeError(
                    f"BUYING_POWER_INSUFFICIENT: simulated cash ${self._cash} "
                    f"can't cover ${cost} for {quantity_decimal} {symbol}"
                )
        self._apply_fill(symbol, "OPTION", side, quantity_decimal, limit_price)
        log.info(
            "PAPER  | simulated fill | OPTION | %-16s | %-5s | qty=%s @ $%s",
            symbol,
            side,
            quantity_decimal,
            limit_price,
        )
        return uuid4().hex

    def open_orders(self) -> list[dict]:
        # Fills happen immediately at submission - there is never a
        # working/resting order to report.
        return []

    def order_detail(self, client_order_id: str) -> dict:
        # Every order this broker ever returns an id for has already
        # "filled" - see order_status()'s expected shape (webull_api.py).
        return {"orders": [{"status": "FILLED", "filled_quantity": "0"}]}

    def cancel(self, client_order_id: str) -> None:
        return None

    def cancel_all_orders(self) -> list[str]:
        return []
