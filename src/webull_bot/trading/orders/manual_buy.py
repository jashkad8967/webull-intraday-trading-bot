import logging
from decimal import Decimal

log = logging.getLogger("webull-bot")


def _manual_buy(
    self,
    command: dict,
    positions: list[dict],
    buying_power: Decimal,
    core_session_active: bool = False,
) -> Decimal:
    """Stocks only for now, mirroring the same entry sizing/pricing the
    automatic strategy uses (dollar-sized during core hours, fixed
    STOCK_QUANTITY sizing otherwise) rather than a separate ad-hoc
    path, so a manual buy still respects the account's normal risk
    limits (MAX_ORDER_NOTIONAL, the $0.10-$0.999 lot rule, etc).
    """
    symbol = str(command.get("symbol", "")).upper()
    if not symbol:
        return buying_power
    if symbol in self.broker_conflict_symbols:
        log.info(
            "CMD    | manual buy skipped | %-8s | broker conflict blacklisted",
            symbol,
        )
        return buying_power
    quantity, _cost = self.api.stock_position(symbol, positions)
    if quantity > 0:
        log.info(
            "CMD    | manual buy skipped | %-8s | already holding a position",
            symbol,
        )
        return buying_power
    blocked_until = self.wash_sales.blocked_until(symbol)
    if blocked_until:
        log.info(
            "CMD    | manual buy skipped | %-8s | wash-sale blocked until %s",
            symbol,
            blocked_until.strftime("%Y-%m-%d"),
        )
        return buying_power
    if self.strategy.open_position_count(positions) >= self.config.max_open_positions:
        log.info(
            "CMD    | manual buy skipped | %-8s | at MAX_OPEN_POSITIONS",
            symbol,
        )
        return buying_power
    try:
        quote = self.api.stock_quote(symbol)
        price = self.api.quote_price(quote)
    except Exception as exc:
        log.error("CMD    | manual buy failed | %-8s | %s", symbol, exc)
        return buying_power
    self.strategy.update_stock_snapshot(quote, price)
    fractional = False
    if (
        core_session_active
        and self.fractional_trading_enabled
        and self.config.stock_core_session_position_fraction > 0
    ):
        target_notional = min(
            buying_power * self.config.stock_core_session_position_fraction,
            buying_power,
            self.config.max_order_notional,
        )
        buy_quantity, buffered_price = self.strategy.dollar_stock_quantity(
            price, target_notional
        )
        fractional = buy_quantity > 0
    else:
        buy_quantity, buffered_price = self.strategy.stock_order_quantity(
            price, buying_power
        )
        if (
            buy_quantity == 0
            and self.config.fractional_shares_enabled
            and core_session_active
            and self.fractional_trading_enabled
        ):
            fractional_quantity = self.strategy.fractional_stock_quantity(
                price, buying_power
            )
            if fractional_quantity > 0:
                buy_quantity = fractional_quantity
                buffered_price = price * Decimal("1.03")
                fractional = True
    if buy_quantity <= 0:
        log.info(
            "CMD    | manual buy skipped | %-8s | no affordable quantity",
            symbol,
        )
        return buying_power
    entry_price = self.api.stock_limit_price(quote, "BUY")
    try:
        order_id = self.api.place_stock(
            symbol,
            "BUY",
            buy_quantity,
            limit_price=entry_price,
            fractional=fractional,
        )
    except Exception as exc:
        if self.is_fractional_trading_not_enabled(exc):
            self.handle_fractional_trading_not_enabled(exc)
        elif self.is_fractional_ticker_unsupported(exc):
            self.handle_fractional_ticker_unsupported(symbol, exc)
        else:
            log.error("CMD    | manual buy failed | %-8s | %s", symbol, exc)
        return buying_power
    self.record_trade(
        f"STOCK:{symbol}",
        order_id,
        "MANUAL_BUY",
        entry_price=entry_price,
        quantity=buy_quantity,
    )
    self.position_buckets[symbol] = "MANUAL"
    log.warning(
        "CMD    | manual buy executed | %-8s | qty=%s",
        symbol,
        buy_quantity,
    )
    return max(Decimal("0"), buying_power - buffered_price * buy_quantity)
