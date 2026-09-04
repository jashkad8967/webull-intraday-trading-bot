import logging

log = logging.getLogger("webull-bot")


def safe_top_losers(self, limit: int, page_size: int) -> dict[str, dict]:
    try:
        return self.api.top_losers(limit, page_size)
    except Exception as exc:
        log.warning("LOAD   | top-losers screener failed this cycle | %s", exc)
        return {}
