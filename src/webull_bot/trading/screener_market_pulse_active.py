import logging

log = logging.getLogger("webull-bot")


def safe_market_pulse_active(self, limit: int, page_size: int) -> dict[str, dict]:
    """Distinct from safe_top_active_stocks: that method's failure
    fallback is the prior day's whole trading universe (right for a
    once-daily universe rebuild), which would blow up market_pulse's
    small-fixed-size guarantee. This falls back to empty instead.
    """
    try:
        return self.api.top_active_stocks(limit, page_size)
    except Exception as exc:
        log.warning("LOAD   | most-active screener failed this cycle | %s", exc)
        return {}
