from decimal import Decimal


def size_stock_entry(
    self,
    price: Decimal,
    entry_budget: Decimal,
    fractional_remaining: Decimal,
    whole_share_remaining: Decimal,
    core_session_active: bool,
    fractional_slot_available: bool = True,
    fractional_supported: bool = True,
    symbol: str = "",
    buying_power: Decimal | None = None,
) -> tuple[Decimal, Decimal, bool]:
    """Splits capital between fractional and whole-share entry sizing
    instead of one style claiming every candidate during core hours.

    fractional_remaining/whole_share_remaining are each computed ONCE
    per trade_stocks cycle (buying_power * their respective fraction)
    and decremented by the caller as buys land - passing a live,
    already-shrinking buying_power in here instead would let fractional
    sizing succeed for nearly every candidate (its own cap barely
    shrinks relative to total buying power), leaving whole-share
    sizing's larger capital slice essentially unreachable during core
    hours. fractional_slot_available additionally gates fractional
    sizing on a reserved position-count budget (see trade_stocks) - a
    fractional position can't be exited outside core hours, so
    fractional entries alone filling every MAX_OPEN_POSITIONS slot
    would strand the account with no room for entries of any style for
    the rest of the day. fractional_supported is False for a specific
    symbol Webull has already rejected with
    FRACT_TICKER_DONT_SUPPORT_TRADE (see
    handle_fractional_ticker_unsupported) - a per-security
    restriction, distinct from fractional_trading_enabled's
    account-wide one.
    Returns (quantity, buffered_price, is_fractional).
    """
    if (
        core_session_active
        and self.fractional_trading_enabled
        and fractional_supported
        and fractional_slot_available
        and fractional_remaining > 0
    ):
        target_notional = min(
            fractional_remaining,
            entry_budget,
            self.config.max_order_notional,
        )
        quantity, buffered_price = self.strategy.dollar_stock_quantity(
            price, target_notional
        )
        if quantity > 0:
            return quantity, buffered_price, True
    whole_share_budget = (
        min(entry_budget, whole_share_remaining)
        if core_session_active
        else entry_budget
    )
    quantity, buffered_price = self.strategy.stock_order_quantity(
        price, whole_share_budget
    )
    # stock_order_quantity is typed -> tuple[int, Decimal] (its own
    # affordability math is integer-based) - normalize to Decimal
    # here since every downstream caller of this whole-share
    # quantity (is_fractional_quantity, record_trade's
    # working_orders["quantity"], etc.) expects one. Without this,
    # a plain int leaking into working_orders["quantity"] crashed
    # a later is_fractional_quantity(quantity) call with "'int'
    # object has no attribute 'to_integral_value'" - live evidence,
    # traced to this line.
    quantity = Decimal(quantity)
    # By request: risk-based position sizing (the professional 1-2%
    # rule, adapted for this account's size - see
    # stock_risk_per_trade_fraction) - an ADDITIONAL cap layered on
    # top of the affordability/notional caps above, not a
    # replacement for them. Sizes against the account's real total
    # buying power (not the bucket-allocated entry_budget slice
    # above), same as how the professional rule is normally stated
    # ("risk 1% of the account"), using the stop distance
    # stock_decision itself will use for this symbol.
    if quantity > 0 and symbol and buying_power is not None:
        stop_percent = self.strategy.adaptive_stop_percent(symbol)
        stop_price = price * (Decimal("1") - stop_percent)
        risk_cap = self.strategy.risk_based_share_count(
            price,
            stop_price,
            buying_power,
            self.config.stock_risk_per_trade_fraction,
        )
        if risk_cap < quantity:
            quantity = Decimal(risk_cap)
            min_lot = self.strategy.minimum_lot_size(price)
            if 0 < quantity < min_lot:
                # The risk cap alone can't afford even the exchange-
                # mandated minimum lot for this price band - skip
                # rather than place an order the broker would
                # reject, same convention stock_order_quantity
                # itself already uses.
                quantity = Decimal("0")
    return quantity, buffered_price, False
