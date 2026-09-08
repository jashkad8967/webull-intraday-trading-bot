import logging
from decimal import Decimal, ROUND_UP
from uuid import uuid4


def open_orders(self) -> list[dict]:
    return self._call(
        lambda: self.trade.order_v3.get_order_open(
            self.config.account_id,
            page_size=100,
        ),
        "account",
    )


def open_order_ids(groups: list[dict]) -> list[str]:
    order_ids: list[str] = []
    for group in groups or []:
        if group.get("client_order_id"):
            order_ids.append(str(group["client_order_id"]))
        else:
            order_ids.extend(
                str(order["client_order_id"])
                for order in (group.get("orders") or [])
                if order.get("client_order_id")
            )
    return list(dict.fromkeys(order_ids))


def cancel(self, client_order_id: str) -> None:
    self._call(
        lambda: self.trade.order_v3.cancel_order(
            self.config.account_id,
            client_order_id,
        ),
        "order",
    )


def order_detail(self, client_order_id: str) -> dict:
    return self._call(
        lambda: self.trade.order_v3.get_order_detail(
            self.config.account_id,
            client_order_id,
        ),
        "order",
    )


def order_status(detail: dict) -> str | None:
    """Best-effort terminal status of a fetched order (SUBMITTED,
    CANCELLED, FAILED, FILLED, PARTIAL_FILLED per the SDK's
    OrderStatus enum).

    Confirmed live shape: get_order_detail's top level carries no
    status field at all - it's nested one level down, inside the
    first entry of an "orders" list (the same grouped shape
    open_orders() returns), e.g.
    {"client_order_id": ..., "orders": [{"status": "CANCELLED",
    "filled_quantity": "0", ...}]}. The flat top-level keys are kept
    as a fallback in case a different order type/response variant
    ever puts it there instead. An unrecognized/missing shape
    returns None - callers must fail open (never treat None as
    either filled or cancelled) rather than guess.
    """
    orders = detail.get("orders") or []
    if orders and isinstance(orders[0], dict):
        for field in ("status", "order_status", "orderStatus"):
            value = orders[0].get(field)
            if value not in (None, ""):
                return str(value).upper()
    for field in ("status", "order_status", "orderStatus"):
        value = detail.get(field)
        if value not in (None, ""):
            return str(value).upper()
    return None


def cancel_all_orders(self) -> list[str]:
    unique = self.open_order_ids(self.open_orders())
    for order_id in unique:
        try:
            self.cancel(order_id)
        except Exception as exc:
            logging.getLogger("webull-bot").error(
                "CANCEL | id=%s | %s",
                order_id,
                exc,
            )
    return unique


def place_stock(
    self,
    symbol: str,
    side: str,
    quantity: int | Decimal,
    limit_price: Decimal | None = None,
    fractional: bool = False,
    market: bool = False,
) -> str:
    """fractional=True places a fixed-quantity MARKET order for a
    quantity in (0, 1] - Webull only supports fractional share trading
    as a MARKET order during core hours, never LIMIT and never extended
    hours, so those overrides are forced regardless of limit_price.

    market=True is the same MARKET/CORE-only override for a plain
    whole-share order - for a caller that wants a guaranteed-fill exit
    (e.g. an urgent manual sell) without going through the fractional-
    quantity machinery above.
    """
    client_order_id = uuid4().hex
    order = {
        "combo_type": "NORMAL",
        "client_order_id": client_order_id,
        "symbol": symbol,
        "instrument_type": "EQUITY",
        "market": "US",
        "order_type": (
            "MARKET" if fractional or market or limit_price is None else "LIMIT"
        ),
        "quantity": str(quantity),
        "support_trading_session": "CORE" if (fractional or market) else "ALL",
        "side": side,
        "time_in_force": "DAY",
        "entrust_type": "QTY",
    }
    if limit_price is not None and not fractional and not market:
        order["limit_price"] = str(
            limit_price.quantize(
                self.price_tick_size(limit_price), rounding=ROUND_UP
            )
        )
    self._call(
        lambda: self.trade.order_v3.place_order(
            self.config.account_id,
            [order],
        ),
        "order",
        retry=False,
    )
    return client_order_id


def order_history(self, start_date: str, end_date: str) -> list[dict]:
    """Every combo-order entry Webull has on record for the account
    in [start_date, end_date] (each "YYYY-MM-DD") - feeds
    AutoTrader.reconcile_order_history's log-only audit. Webull
    rejects a page_size under 10 outright, so this always requests
    the max useful page and paginates via last_client_order_id.

    Deliberately throttled under the "account" group, not "order" -
    this is a read-only, once-per-ORDER_HISTORY_RECONCILE_SECONDS
    (default 30 min) audit call, not a live trading action. Sharing
    the "order" group with actual order placement/cancellation meant
    it competed for the same rate budget as real-time trading
    activity - live incident: with volatility-scalp's fast cent-by-
    cent repricing (cancel+replace every ~1s per eligible position)
    now adding real load there, this non-critical audit call was
    getting starved out with a sustained 429 across every retry
    attempt. An audit falling a cycle behind is harmless; a live
    order call losing its rate-limit slot to an audit query is not.
    """
    orders: list[dict] = []
    cursor = None
    while True:
        page = self._call(
            lambda: self.trade.order_v3.get_order_history(
                self.config.account_id,
                page_size=100,
                start_date=start_date,
                end_date=end_date,
                last_client_order_id=cursor,
            ),
            "account",
        )
        if not page:
            break
        orders.extend(page)
        if len(page) < 100:
            break
        cursor = page[-1].get("client_order_id")
        if not cursor:
            break
    return orders


def close_all_positions(
    self,
    instrument_types: set[str] | None = None,
    loss_callback=None,
    exclude_symbols: set[str] | None = None,
) -> list[str]:
    positions = [
        position
        for position in self.positions()
        if (
            instrument_types is None
            or position.get("instrument_type") in instrument_types
        )
        and (
            not exclude_symbols
            or str(position.get("symbol", "")).upper() not in exclude_symbols
        )
    ]
    if not positions:
        return []
    self.cancel_all_orders()
    submitted: list[str] = []

    def close_one(position: dict, quantity: Decimal) -> None:
        if position.get("instrument_type") == "EQUITY":
            side = "SELL" if quantity > 0 else "BUY"
            # Pricing side is distinct from the broker order side above:
            # covering a short still submits as a plain "BUY" (Webull's
            # order API has no fourth side value), but it needs the
            # urgent cross-the-ask "COVER" pricing branch, not the
            # passive mid-price one plain "BUY" entries use.
            pricing_side = "SELL" if quantity > 0 else "COVER"
            quote = self.stock_quote(position["symbol"])
            market_price = self.quote_price(quote)
            average_cost = Decimal(str(position.get("cost_price") or "0"))
            loss_exit = (
                quantity > 0
                and average_cost > 0
                and market_price < average_cost
            ) or (
                quantity < 0
                and average_cost > 0
                and market_price > average_cost
            )
            limit_price = self.stock_limit_price(quote, pricing_side)
            fractional = abs(quantity) != abs(quantity).to_integral_value()
            submitted.append(
                self.place_stock(
                    position["symbol"],
                    side,
                    abs(quantity),
                    limit_price,
                    fractional=fractional,
                )
            )
            if loss_exit and loss_callback:
                loss_callback(position["symbol"], "loss closeout submitted")
        elif position.get("instrument_type") == "OPTION":
            contract = self.contract_from_position(position)
            if not contract:
                logging.getLogger("webull-bot").error(
                    "CLOSE  | unresolved option=%s",
                    position.get("symbol", "UNKNOWN"),
                )
                return
            side = "SELL" if quantity > 0 else "BUY"
            intent = "SELL_TO_CLOSE" if quantity > 0 else "BUY_TO_CLOSE"
            quote = self.option_quote(contract["symbol"])
            average_cost = Decimal(str(position.get("cost_price") or "0"))
            market_price = self.quote_price(quote)
            loss_exit = (
                quantity > 0
                and average_cost > 0
                and market_price < average_cost
            ) or (
                quantity < 0
                and average_cost > 0
                and market_price > average_cost
            )
            limit_price = self.option_limit_price(quote, side)
            submitted.append(
                self.place_option(
                    contract,
                    side,
                    abs(quantity),
                    limit_price,
                    intent,
                )
            )
            if loss_exit and loss_callback:
                loss_callback(
                    contract["underlying_symbol"],
                    "option loss closeout submitted",
                )

    for position in positions:
        quantity = Decimal(str(position.get("quantity", "0")))
        if not quantity:
            continue
        try:
            close_one(position, quantity)
        except Exception as exc:
            # One position rejected (e.g. a sub-100-share position
            # stuck in Webull's $0.10-$0.999 lot-restricted band, which
            # rejects orders on ANY size below 100 there) must never
            # abort closing every other position in this batch - this
            # loop previously had no exception handling at all, so an
            # unwrapped raise here silently skipped the rest of the EOD
            # closeout, options included, for the whole account.
            logging.getLogger("webull-bot").error(
                "CLOSE  | %s | close order failed, continuing with "
                "remaining positions | %s",
                position.get("symbol", "UNKNOWN"),
                exc,
            )
    return submitted
