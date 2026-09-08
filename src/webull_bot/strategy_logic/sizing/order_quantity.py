from decimal import Decimal, ROUND_DOWN


def minimum_lot_size(price: Decimal) -> int:
    """Webull rejects any order under 100 shares outright for stocks
    priced $0.10-$0.999 (OAUTH_OPENAPI_CANT_TRADE_FOR_PRICE_BETWEEN_
    0099_AND_0999) - a plain per-order STOCK_QUANTITY of 1 would fail
    every single time in that band, not just occasionally.
    """
    if Decimal("0.10") <= price <= Decimal("0.999"):
        return 100
    return 1


def exit_blocked_by_lot_restriction(cls, quantity: Decimal, price: Decimal) -> bool:
    """True only when price sits in the $0.10-$0.999 band AND quantity
    can't clear that band's 100-share minimum - NOT whenever quantity
    is merely less than minimum_lot_size's return value.

    minimum_lot_size returns 1 (a no-op floor) for every price outside
    that band, so a bare `quantity < minimum_lot_size(price)` comparison
    - three separate call sites in bot.py used to write it exactly that
    way - reads as "true for practically every fractional position at
    a normal price", since a fractional quantity is by definition under
    1. That silently blocked every fractional position's PROFIT/LOSS/
    stall-breaker exit at a normal price, indefinitely: not a rare
    edge case, the common one, since fractional entries are dollar-
    sized slices of ordinarily-priced stocks, not usually penny stocks.
    """
    min_lot = cls.minimum_lot_size(price)
    return min_lot > 1 and quantity < min_lot


def risk_based_share_count(
    self,
    price: Decimal,
    stop_price: Decimal,
    buying_power: Decimal,
    risk_fraction: Decimal,
) -> int:
    """The professional 1-2% position-sizing rule: size an entry so
    that hitting the stop costs no more than risk_fraction of
    buying_power, not however many shares a fixed dollar budget
    happens to afford. `risk_dollars = buying_power * risk_fraction`,
    `shares = risk_dollars / abs(price - stop_price)`, floored to a
    whole share.

    By design, this is a CAP layered on top of the existing
    affordability/max_order_notional caps (see stock_order_quantity),
    not a replacement for them - a caller takes the min() of this
    and its other sizing result. stock_risk_per_trade_fraction is
    set deliberately above the professional 1-2% standard for this
    account's size: a very small account's fixed per-trade costs
    (spread, fees) make 1% barely actionable, so a somewhat larger
    fraction is a documented, deliberate tradeoff, not a hidden
    compromise.

    Returns 0 if the stop distance is zero/invalid (can't size
    against an undefined risk) - same "no data -> don't trade"
    convention as every other gate in this file.
    """
    stop_distance = abs(price - stop_price)
    if stop_distance <= 0 or buying_power <= 0:
        return 0
    risk_dollars = buying_power * risk_fraction
    return int((risk_dollars / stop_distance).to_integral_value(rounding=ROUND_DOWN))


def stock_order_quantity(
    self,
    price: Decimal,
    buying_power: Decimal,
) -> tuple[int, Decimal]:
    buffered_price = price * Decimal("1.03")
    affordable = int(
        (buying_power / buffered_price).to_integral_value(
            rounding=ROUND_DOWN
        )
    )
    notional_limit = int(
        (self.config.max_order_notional / price).to_integral_value(
            rounding=ROUND_DOWN
        )
    )
    quantity = min(self.config.stock_quantity, affordable, notional_limit)
    min_lot = self.minimum_lot_size(price)
    if quantity < min_lot:
        quantity = (
            min_lot
            if min_lot <= affordable and min_lot <= notional_limit
            else 0
        )
    return quantity, buffered_price


def fractional_stock_quantity(
    self,
    price: Decimal,
    buying_power: Decimal,
) -> Decimal:
    """Fallback sizing for when buying_power can't afford even one whole
    share: Webull's fractional orders are quantity-capped to (0, 1] and
    must clear a minimum order value, so this returns Decimal("0") -
    meaning "skip, don't place a fractional order" - whenever either
    constraint can't be met, rather than rounding into an invalid order.

    A stock priced $0.10-$0.999 needs a 100-share minimum order size
    (see minimum_lot_size) that no fractional order (always <= 1 share)
    can ever satisfy, so fractional sizing is skipped entirely there.
    """
    if price <= 0 or self.minimum_lot_size(price) > 1:
        return Decimal("0")
    min_notional = self.config.fractional_shares_min_notional
    affordable_notional = min(buying_power, price)
    if affordable_notional < min_notional:
        return Decimal("0")
    quantity = (affordable_notional / price).quantize(
        Decimal("0.0001"),
        rounding=ROUND_DOWN,
    )
    quantity = min(quantity, Decimal("1"))
    if quantity <= 0 or quantity * price < min_notional:
        return Decimal("0")
    return quantity


def dollar_stock_quantity(
    self,
    price: Decimal,
    target_notional: Decimal,
) -> tuple[Decimal, Decimal]:
    """Core-session entry sizing: convert a dollar budget directly into a
    decimal share quantity for a fractional MARKET order, instead of
    picking a share count first and checking whether it's affordable.
    Unlike fractional_stock_quantity, this is not capped at one share -
    Webull's QTY-type fractional orders accept decimal quantities above 1
    (only its separate, unused AMOUNT order type is capped under one
    share's price). Skips the $0.10-$0.999 lot-restricted band entirely
    (see minimum_lot_size) since Webull requires a 100-share lot there
    that no decimal-quantity order can satisfy.
    """
    buffered_price = price * Decimal("1.03")
    if price <= 0 or self.minimum_lot_size(price) > 1:
        return Decimal("0"), buffered_price
    if target_notional < self.config.fractional_shares_min_notional:
        return Decimal("0"), buffered_price
    quantity = (target_notional / buffered_price).quantize(
        Decimal("0.0001"), rounding=ROUND_DOWN
    )
    if quantity <= 0:
        return Decimal("0"), buffered_price
    return quantity, buffered_price


def option_order_quantity(
    self,
    limit_price: Decimal,
    buying_power: Decimal,
) -> tuple[int, Decimal]:
    contract_cost = limit_price * 100
    affordable = int(
        (buying_power / contract_cost).to_integral_value(
            rounding=ROUND_DOWN
        )
    )
    notional_limit = int(
        (
            self.config.max_order_notional / contract_cost
        ).to_integral_value(rounding=ROUND_DOWN)
    )
    # Never risk more than this fraction of buying power on one entry -
    # a defined-risk-per-trade cap on top of (not instead of) the
    # option_quantity/max_order_notional caps above.
    risk_cap = int(
        (
            buying_power * self.config.option_capital_fraction / contract_cost
        ).to_integral_value(rounding=ROUND_DOWN)
    )
    return (
        min(self.config.option_quantity, affordable, notional_limit, risk_cap),
        contract_cost,
    )
