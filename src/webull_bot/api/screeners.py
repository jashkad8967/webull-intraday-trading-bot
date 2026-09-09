def _screener_number(item: dict, field: str) -> float:
    try:
        value = float(item.get(field, ""))
    except (TypeError, ValueError):
        return 0.0
    return value if value == value and value not in (float("inf"), float("-inf")) else 0.0


def _page_screener(self, fetch_page, total_limit: int, page_size: int) -> dict[str, dict]:
    """Shared pagination/parsing for Webull screener endpoints.

    The SDK doesn't publish a typed response model for these endpoints, so
    the page-of-results and has-more-pages fields are read defensively
    across the field names Webull's own docs/SDK docstrings reference.
    """
    results: dict[str, dict] = {}
    page_index = 1
    while len(results) < total_limit:
        remaining = total_limit - len(results)
        requested_size = min(page_size, remaining)
        response = self._call(
            lambda page_index=page_index, requested_size=requested_size: (
                fetch_page(page_index, requested_size)
            ),
            "market",
        )
        if isinstance(response, list):
            items, has_more = response, len(response) >= requested_size
        elif isinstance(response, dict):
            items = (
                response.get("list")
                or response.get("data")
                or response.get("items")
                or []
            )
            has_more = bool(response.get("has_more", len(items) >= requested_size))
        else:
            items, has_more = [], False
        if not items:
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol", "")).upper()
            if not symbol:
                continue
            results[symbol] = {
                "market_value": self._screener_number(item, "market_value"),
                "volume": self._screener_number(item, "volume"),
                "change_ratio": self._screener_number(item, "change_ratio"),
                "amplitude": self._screener_number(item, "amplitude"),
            }
        if not has_more or len(results) >= total_limit:
            break
        page_index += 1
    return results


def top_active_stocks(
    self,
    total_limit: int,
    page_size: int,
    rank_type: str = "VOLUME",
    sort_by: str = "MARKET_VALUE",
) -> dict[str, dict]:
    """Page through Webull's most-active screener for a large, market-cap-
    tagged stock universe (no ETFs - the screener's US_STOCK category is
    stocks only).
    """
    from webull.data.common.category import Category

    return self._page_screener(
        lambda page_index, requested_size: self.data.screener.get_most_active(
            category=Category.US_STOCK.name,
            rank_type=rank_type,
            sort_by=sort_by,
            direction="DESC",
            page_index=page_index,
            page_size=requested_size,
        ),
        total_limit,
        page_size,
    )


def top_gainers(
    self,
    total_limit: int,
    page_size: int,
    rank_type: str = "DAY_1",
) -> dict[str, dict]:
    """Page through Webull's gainers screener (stocks up the most today by
    price change), so the selection universe includes actual current
    uptrends/momentum names rather than only high-volume names.
    """
    from webull.data.common.category import Category

    return self._page_screener(
        lambda page_index, requested_size: self.data.screener.get_gainers_losers(
            rank_type=rank_type,
            category=Category.US_STOCK.name,
            sort_by="CHANGE_RATIO",
            direction="DESC",
            page_index=page_index,
            page_size=requested_size,
        ),
        total_limit,
        page_size,
    )


def top_losers(
    self,
    total_limit: int,
    page_size: int,
    rank_type: str = "DAY_1",
) -> dict[str, dict]:
    """Same screener as top_gainers, ranked ascending instead (today's
    biggest decliners).
    """
    from webull.data.common.category import Category

    return self._page_screener(
        lambda page_index, requested_size: self.data.screener.get_gainers_losers(
            rank_type=rank_type,
            category=Category.US_STOCK.name,
            sort_by="CHANGE_RATIO",
            direction="ASC",
            page_index=page_index,
            page_size=requested_size,
        ),
        total_limit,
        page_size,
    )
