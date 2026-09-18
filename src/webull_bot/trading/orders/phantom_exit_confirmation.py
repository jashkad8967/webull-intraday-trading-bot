import logging
from decimal import Decimal

log = logging.getLogger("webull-bot")


def _reverse_if_never_filled(
    self, order_id: str, order: dict, pnl: Decimal
) -> None:
    """An exit order that dropped out of the open-orders list is
    usually a fill, but it can also be a cancel/reject the broker
    processed on its own (e.g. a fat-finger price, a stale quote) -
    record_realized_exit already counted its pnl as if it filled, at
    submission time. Confirm via order_detail before trusting that;
    only reverse on an explicit CANCELLED/FAILED status, and fail
    open (assume filled, leave the pnl as-is) on any fetch error or
    an unrecognized/missing status field, since the field name isn't
    confirmed against a live payload yet - a false reversal would be
    worse than an occasional unconfirmed phantom.
    """
    try:
        detail = self.api.order_detail(order_id)
        status = self.api.order_status(detail)
    except Exception as exc:
        log.error(
            "ORDER  | could not confirm fill status | id=%s | %s",
            order_id,
            exc,
        )
        return
    if status in ("CANCELLED", "FAILED"):
        self.reverse_phantom_exit(pnl, order_id)
        self._note_exit_failure(order.get("key", ""))
        log.warning(
            "ORDER  | %s | never filled (%s) - reversing $%s phantom "
            "realized pnl | id=%s",
            order.get("key", ""),
            status,
            pnl,
            order_id,
        )
        return
    # It filled - but at WHAT price? record_realized_exit priced this
    # from the submitted limit at submission time, which is exact for
    # a limit fill but not for a fractional stock order (Webull routes
    # those as MARKET orders). Live incident, by explicit request:
    # FIGR submitted at 35.81 on 1.2067 shares actually filled at
    # 35.71, so a recorded "+$0.05 PROFIT" was really a loss. The same
    # order_detail already fetched above carries the real executed
    # price, so correct the estimate against it here rather than
    # leaving a wrong number in the running totals and on the
    # dashboard.
    _correct_to_actual_fill(self, order_id, order, pnl, detail)


def _correct_to_actual_fill(
    self, order_id: str, order: dict, pnl: Decimal, detail: dict
) -> None:
    actual = self.api.order_filled_price(detail)
    estimated = order.get("limit_price")
    quantity = order.get("quantity")
    if actual is None or estimated is None or not quantity:
        # Fail open on anything unparseable - an unconfirmed estimate
        # is better than "correcting" toward a guess, same convention
        # as order_status's own missing-field handling.
        return
    try:
        estimated = Decimal(str(estimated))
        quantity = Decimal(str(quantity))
    except (ArithmeticError, ValueError, TypeError):
        return
    if actual == estimated:
        return
    multiplier = Decimal("100") if str(order.get("key", "")).startswith(
        "OPTION:"
    ) else Decimal("1")
    delta = (actual - estimated) * quantity * multiplier
    self.correct_realized_exit(order_id, pnl, delta)
    log.warning(
        "ORDER  | %s | filled at %s, not the submitted %s - corrected "
        "realized pnl by $%s | id=%s",
        order.get("key", ""),
        actual,
        estimated,
        delta,
        order_id,
    )
