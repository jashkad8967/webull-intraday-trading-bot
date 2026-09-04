import logging

log = logging.getLogger("webull-bot")


def safe_premarket_gainers(self, limit: int, page_size: int) -> dict[str, dict]:
    """Same screener as safe_top_gainers, but Webull's own
    rank_type="PRE_MARKET" (today's biggest movers in the
    pre-market session specifically) instead of the default
    DAY_1/regular-session ranking safe_top_gainers already feeds
    into the daily universe rebuild - see refresh_premarket_
    gainers. A screener hiccup here must never crash the trading
    loop either.
    """
    try:
        return self.api.top_gainers(limit, page_size, rank_type="PRE_MARKET")
    except Exception as exc:
        log.warning(
            "LOAD   | pre-market gainers screener failed | %s", exc
        )
        return {}
