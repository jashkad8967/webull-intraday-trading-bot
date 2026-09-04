import logging
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("webull-bot")


def reconcile_order_history(self) -> None:
    """Log-only audit: cross-checks today's Webull order history
    against every order_id the bot itself submitted today (see
    submitted_order_ids_today). An order in Webull's history the bot
    never recorded is very likely a manual action taken directly in
    the Webull app - this never changes any bot state (sizing, pnl,
    gates), purely a visibility signal. Each unrecognized order is
    logged once per day (reconciliation_flagged_order_ids), not every
    cycle this runs.
    """
    if not self.config.order_history_reconcile_enabled:
        return
    now = time.monotonic()
    if now - self.last_order_history_reconcile < self.config.order_history_reconcile_seconds:
        return
    self.last_order_history_reconcile = now
    today = self.now().date()
    try:
        # Webull rejects a same-day start_date/end_date pair outright
        # (417 OAUTH_OPENAPI_PARAM_ERR) - a 1-day lookback is the
        # smallest range it accepts. Yesterday's orders are filtered
        # back out below (placed_today), so this doesn't widen what
        # actually gets flagged.
        history = self.api.order_history(
            (today - timedelta(days=1)).isoformat(), today.isoformat()
        )
    except Exception as exc:
        log.warning("RECON  | order history fetch failed | %s", exc)
        return
    # place_time_at is UTC (a trailing "Z"), not the bot's trading
    # timezone - comparing it against today's ET-local date string
    # would misclassify anything placed in the last few hours of ET
    # extended trading (already the next UTC calendar date) as
    # "yesterday" and silently skip it.
    today_prefix = datetime.now(timezone.utc).date().isoformat()
    for combo in history:
        client_order_id = combo.get("client_order_id")
        if not client_order_id:
            continue
        placed_today = any(
            str(order.get("place_time_at", "")).startswith(today_prefix)
            for order in combo.get("orders", [])
        )
        if not placed_today:
            continue
        if client_order_id in self.submitted_order_ids_today:
            continue
        if client_order_id in self.reconciliation_flagged_order_ids:
            continue
        self.reconciliation_flagged_order_ids.add(client_order_id)
        for order in combo.get("orders", []):
            log.warning(
                "RECON  | %-8s | order not recognized by the bot - "
                "likely a manual action outside it | side=%s status=%s "
                "qty=%s filled=%s id=%s",
                order.get("symbol", "?"),
                order.get("side"),
                order.get("status"),
                order.get("total_quantity"),
                order.get("filled_quantity"),
                client_order_id,
            )
