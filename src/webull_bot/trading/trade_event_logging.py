import logging

log = logging.getLogger("webull-bot")


def log_trade_events(self) -> None:
    """Phase 0 of the polling-to-streaming migration (see the plan):
    drains and logs whatever TradeEventStreamService received since
    the last cycle. Purely observational - no trading state is
    touched here. The goal is to document the real payload schema
    from live traffic (the SDK source only confirms one field,
    request_id) before any later phase parses these events for
    anything that matters.
    """
    if self.trade_event_service is None:
        return
    for event_type, subscribe_type, payload in self.trade_event_service.drain():
        log.info(
            "EVENTS | event_type=%s | subscribe_type=%s | %s",
            event_type,
            subscribe_type,
            payload,
        )
