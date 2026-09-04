import logging
from decimal import Decimal

log = logging.getLogger("webull-bot")


def _manual_sell(
    self,
    command: dict,
    positions: list[dict],
    core_session_active: bool = False,
) -> None:
    symbol = str(command.get("symbol", "")).upper()
    instrument_type = command.get("instrument_type", "EQUITY")
    if not symbol:
        return
    position = next(
        (
            item
            for item in positions
            if item.get("instrument_type") == instrument_type
            and str(item.get("symbol", "")).upper() == symbol
        ),
        None,
    )
    if not position:
        log.info(
            "CMD    | manual sell skipped | %-8s | no matching open position",
            symbol,
        )
        return
    quantity = Decimal(str(position.get("quantity", "0")))
    cost = Decimal(str(position.get("cost_price", "0")))
    if quantity <= 0:
        return
    if instrument_type == "EQUITY":
        if symbol in self.pending_stock_exits:
            log.info(
                "CMD    | manual sell skipped | %-8s | exit already pending",
                symbol,
            )
            return
        is_fractional = self.is_fractional_quantity(quantity)
        if is_fractional and not core_session_active:
            log.info(
                "CMD    | manual sell skipped | %-8s | fractional "
                "position, Webull only allows an order on it during "
                "core hours",
                symbol,
            )
            return
        quote = self.api.stock_quote(symbol)
        # A manual sell is an urgent "get me out" click, not a patient
        # resting order - the old below-bid crossing price
        # (stock_limit_price's SELL side) shaved off an extra
        # STOCK_LIMIT_OFFSET on top of the spread, which could tip an
        # otherwise-flat or barely-profitable exit into a recorded
        # loss for no real reason. Price it at the ask (top of the
        # spread) instead, and place a genuine MARKET order whenever
        # one is actually usable (whole shares, core hours, account
        # allows fractional/MARKET orders) so it's not left resting
        # unfilled either.
        sell_price = self.api.quote_ask(quote) or self.api.stock_limit_price(
            quote, "SELL"
        )
        use_market = (
            core_session_active
            and not is_fractional
            and self.fractional_trading_enabled
        )
        order_id = self.api.place_stock(
            symbol,
            "SELL",
            quantity,
            limit_price=None if use_market else sell_price,
            fractional=is_fractional,
            market=use_market,
        )
        self.pending_stock_exits.add(symbol)
        pnl = self.record_realized_exit(cost, sell_price, quantity)
        self.record_trade(
            f"STOCK:{symbol}", order_id, "MANUAL_SELL", sell_price, pnl=pnl,
            entry_price=cost, quantity=quantity,
        )
        if pnl < 0:
            self.wash_sales.block(symbol, "manual sell at a loss")
    elif instrument_type == "OPTION":
        if symbol in self.pending_option_exits:
            log.info(
                "CMD    | manual sell skipped | %-8s | exit already pending",
                symbol,
            )
            return
        contract = self.api.contract_from_position(position)
        if not contract:
            log.error(
                "CMD    | manual sell failed | %-8s | could not resolve option contract",
                symbol,
            )
            return
        quote = self.api.option_quote(contract["symbol"])
        sell_price = self.api.quote_ask(quote) or self.api.option_limit_price(
            quote, "SELL"
        )
        order_id = self.api.place_option(
            contract,
            "SELL",
            quantity,
            sell_price,
            "SELL_TO_CLOSE",
        )
        self.pending_option_exits.add(symbol)
        pnl = self.record_realized_exit(cost, sell_price, quantity, multiplier=100)
        self.record_trade(
            f"OPTION:{symbol}", order_id, "MANUAL_SELL", sell_price, pnl=pnl,
            entry_price=cost, quantity=quantity,
        )
        if pnl < 0:
            self.wash_sales.block(
                contract["underlying_symbol"],
                "manual option sell at a loss",
            )
    else:
        return
    log.warning(
        "CMD    | manual sell executed | %-8s (%s) | qty=%s",
        symbol,
        instrument_type,
        quantity,
    )
