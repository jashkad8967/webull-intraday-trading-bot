import logging

log = logging.getLogger("webull-bot")


def safe_top_gainers(self, limit: int, page_size: int) -> dict[str, dict]:
    """top_gainers() hits a live Webull endpoint during the once-daily
    universe rebuild; a screener hiccup here must never be allowed to
    crash the whole trading loop, so failures are logged and treated as
    "no gainers this cycle" instead of propagating.
    """
    try:
        return self.api.top_gainers(limit, page_size)
    except Exception as exc:
        log.warning("LOAD   | top-gainers screener failed this cycle | %s", exc)
        return {}
