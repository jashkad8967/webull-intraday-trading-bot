import logging
import time
from decimal import Decimal

from webull_bot.trading.orders.locks import _working_orders_lock
from webull_bot.trading.orders.manual_touch import _manual_touch_active
from webull_bot.trading.orders.rate_limit_retry import _retry_once_on_rate_limit

log = logging.getLogger("webull-bot")


def monitor_working_orders(self) -> None:
    now = time.monotonic()
    if (
        now - self.last_order_monitor
        < float(self.config.order_monitor_seconds)
    ):
        return
    self.last_order_monitor = now
    groups = self.api.open_orders()
    open_ids = set(self.api.open_order_ids(groups))

    with _working_orders_lock(self):
        known_order_ids = set(self.working_orders)

    for order_id in open_ids:
        if order_id in self.submitted_order_ids_today:
            # A bot-submitted order, just not currently in
            # working_orders - live incident: the fast volatility-
            # scalp entry/exit repricers cancel-and-replace roughly
            # every second, and there's a real window right after
            # cancel() where the OLD order_id can still show up in
            # open_orders() (broker-side latency) even though
            # working_orders already dropped it in favor of the new
            # replacement order_id. Without this check, that window
            # got misread as "the bot's own order is unrecognized -
            # must be manual," mislabeling normal repricing as a
            # manual action. submitted_order_ids_today (already
            # maintained for reconcile_order_history) never shrinks
            # intraday, so it reliably distinguishes "ours, just
            # untracked right now" from "genuinely never ours."
            continue
        if order_id not in known_order_ids:
            # An order the bot never submitted itself and doesn't
            # already know about - almost always a manual action
            # taken directly in the Webull app (a dashboard-driven
            # manual buy/sell already calls record_trade immediately,
            # so it would already be in working_orders by the time
            # this runs). By request: don't just log an opaque
            # order_id - fetch the real symbol/side/quantity and run
            # it through the same record_trade tracking a bot-driven
            # trade gets, so it actually factors into last_trade/
            # last_exit_at, symbol_pnl_history, the idle-cash ramp,
            # and the dashboard's trade log, instead of sitting
            # invisible to all of that.
            symbol = ""
            side = ""
            quantity = None
            try:
                detail = self.api.order_detail(order_id)
                orders = detail.get("orders") or []
                first = (
                    orders[0]
                    if orders and isinstance(orders[0], dict)
                    else detail
                )
                symbol = str(first.get("symbol") or "").upper()
                side = str(first.get("side") or "").upper()
                raw_quantity = first.get("total_quantity")
                if raw_quantity not in (None, ""):
                    quantity = Decimal(str(raw_quantity))
            except Exception as exc:
                log.warning(
                    "ORDER  | could not fetch detail for an "
                    "unrecognized order | id=%s | %s",
                    order_id,
                    exc,
                )
            if symbol:
                action = "MANUAL_SELL" if side in ("SELL", "COVER") else "MANUAL_BUY"
                self.record_trade(
                    f"STOCK:{symbol}", order_id, action, quantity=quantity
                )
                log.info(
                    "ORDER  | monitoring manual order | %-8s | side=%s "
                    "| id=%s",
                    symbol,
                    side or "?",
                    order_id,
                )
            else:
                with _working_orders_lock(self):
                    self.working_orders[order_id] = {
                        "submitted_at": now,
                        "key": "",
                        "action": "UNKNOWN",
                        "cancel_requested_at": None,
                    }
                log.info(
                    "ORDER  | monitoring broker order | id=%s",
                    order_id,
                )

    with _working_orders_lock(self):
        snapshot = list(self.working_orders.items())

    for order_id, order in snapshot:
        if order_id not in open_ids:
            self._release_pending_order(order)
            with _working_orders_lock(self):
                self.working_orders.pop(order_id, None)
            self.last_account_refresh = 0.0
            pnl = order.get("pnl")
            if pnl:
                self._reverse_if_never_filled(order_id, order, pnl)
            continue

        age = now - float(order["submitted_at"])
        if age < float(self.config.order_timeout_seconds):
            continue
        # By request: "it is still cancelling after 2 mins when i
        # place an order for a stock, it should not" / "when i
        # touch a stock stop doing anything with it while i am
        # there." Missed this call site when the manual-touch pause
        # first shipped - this generic order_timeout_seconds (120s)
        # cancel loop runs for ANY tracked order regardless of who
        # placed it, so a symbol the user just manually touched
        # (including one they placed themselves, if it ever ends up
        # tracked here) still got auto-cancelled on the same 120s
        # clock as everything else.
        order_key = str(order.get("key") or "")
        if order_key.startswith("STOCK:") and _manual_touch_active(
            self, order_key.split(":", 1)[1]
        ):
            continue
        last_cancel = order.get("cancel_requested_at")
        if last_cancel is not None and now - float(last_cancel) < 30:
            continue
        try:
            _retry_once_on_rate_limit(self.api.cancel, order_id)
            with _working_orders_lock(self):
                live_order = self.working_orders.get(order_id)
                if live_order is not None:
                    live_order["cancel_requested_at"] = now
            log.warning(
                "CANCEL | unfilled after %ss | id=%s",
                self.config.order_timeout_seconds,
                order_id,
            )
        except Exception as exc:
            if self.is_order_not_cancelable(exc):
                log.warning(
                    "CANCEL | id=%s | order already resolving | %s",
                    order_id,
                    exc,
                )
            else:
                log.error("CANCEL | id=%s | %s", order_id, exc)
