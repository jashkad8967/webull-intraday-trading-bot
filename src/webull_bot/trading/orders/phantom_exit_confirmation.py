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
