from decimal import Decimal

from webull_bot.strategy_logic.types import PortfolioDecision


def open_position_count(positions: list[dict]) -> int:
    return sum(
        1
        for item in positions
        if Decimal(str(item.get("quantity", "0"))) != 0
    )


def position_unrealized_pnl(self, position: dict) -> Decimal:
    """Net of the flat sell fee this position hasn't paid yet - it's
    still open, but closing it will cost that fee, so showing the raw
    pre-fee mark-to-market number would overstate what selling right
    now actually nets.

    Since cost, not since today - a position held across several
    sessions accumulates this the whole time it's open. See
    position_day_pnl for the today-only figure the dashboard's "P&L
    Today" panel actually wants.
    """
    reported = position.get("unrealized_profit_loss")
    if reported not in (None, ""):
        try:
            return Decimal(str(reported)) - self.config.sell_fee_dollars
        except Exception:
            pass
    try:
        quantity = Decimal(str(position.get("quantity", "0")))
        cost = Decimal(str(position.get("cost_price", "0")))
        price = Decimal(
            str(
                position.get("last_price")
                or position.get("market_price")
                or "0"
            )
        )
        multiplier = (
            Decimal("100")
            if position.get("instrument_type") == "OPTION"
            else Decimal("1")
        )
        return (price - cost) * quantity * multiplier - self.config.sell_fee_dollars
    except Exception:
        return Decimal("0")


def position_day_pnl(self, position: dict) -> Decimal:
    """This position's mark-to-market move since the prior session's
    4pm ET close, net of the flat sell fee not yet paid - Webull's own
    day_profit_loss field, which resets independently of when the
    position was originally opened (unlike unrealized_profit_loss,
    which accumulates since cost for as long as the position is held).

    A whole-share position opened before today has no local record of
    yesterday's close to fall back to, so an unreported field returns
    0 (unknown) rather than silently substituting the since-cost
    figure under a "today" label. A fractional position is the one
    exception: Webull's fractional order type is core-hours-only and
    cannot be held overnight, so a fractional position was, by
    construction, always opened earlier the same day - "since cost"
    and "since today" are the same number for it, making the
    since-cost fallback exact rather than a guess. Live complaint:
    the dashboard's open/daily P&L read wrong specifically for
    fractional holdings - this is the gap that explains it, since
    Webull doesn't always report day_profit_loss for a fractional
    position, and the unconditional-0 fallback silently understated
    (or hid entirely) exactly those positions' contribution.
    """
    reported = position.get("day_profit_loss")
    if reported not in (None, ""):
        try:
            return Decimal(str(reported)) - self.config.sell_fee_dollars
        except Exception:
            return Decimal("0")
    try:
        quantity = Decimal(str(position.get("quantity", "0")))
    except Exception:
        return Decimal("0")
    if quantity != quantity.to_integral_value():
        return self.position_unrealized_pnl(position)
    return Decimal("0")


def portfolio_decision(
    position_states: list[dict],
    minimum_losers: int,
    loss_threshold: Decimal,
) -> PortfolioDecision:
    losing = [
        item
        for item in position_states
        if Decimal(str(item.get("unrealized_pnl", "0"))) < 0
    ]
    if len(losing) < minimum_losers:
        return PortfolioDecision(
            "HOLD",
            "loss cluster below position threshold",
        )
    total_loss = -sum(
        Decimal(str(item["unrealized_pnl"]))
        for item in losing
    )
    if total_loss >= loss_threshold:
        return PortfolioDecision(
            "LIQUIDATE",
            "simultaneous loss threshold reached",
            len(losing),
            total_loss,
        )
    return PortfolioDecision(
        "HOLD",
        "loss cluster below liquidation threshold",
        len(losing),
        total_loss,
    )
