import logging
import time

log = logging.getLogger("webull-bot")


def _manual_cancel_order(self, command: dict) -> None:
    order_id = str(command.get("order_id", "")).strip()
    if not order_id:
        return
    order = self.working_orders.get(order_id)
    if not order:
        log.info(
            "CMD    | cancel skipped | id=%s | no longer a tracked working order",
            order_id,
        )
        return
    if order.get("cancel_requested_at") is not None:
        log.info(
            "CMD    | cancel skipped | id=%s | already cancel-requested",
            order_id,
        )
        return
    try:
        self.api.cancel(order_id)
    except Exception as exc:
        log.error("CMD    | cancel failed | id=%s | %s", order_id, exc)
        return
    order["cancel_requested_at"] = time.monotonic()
    # Reconciliation (releasing pending_stock_exits/pending_option_exits
    # for a cancelled STOP/PROFIT, dropping it from working_orders) is
    # handled the next time monitor_working_orders sees the order has
    # actually disappeared from the broker's open-order list - the same
    # path an automatic timeout cancel already goes through, so a
    # manual cancel doesn't need its own separate cleanup logic.
    log.warning(
        "CMD    | manual cancel requested from dashboard | %s | id=%s",
        order.get("key", "?"),
        order_id,
    )
