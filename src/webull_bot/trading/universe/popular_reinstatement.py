import logging

log = logging.getLogger("webull-bot")


def filter_with_popular_reinstated(self, candidates: list[str]) -> list[str]:
    """Volatility-filter candidates, then re-add any configured popular
    symbol the filter cut - so well-known names aren't silently dropped
    just because their historical amplitude sits under the floor.
    """
    filtered = self.filter_by_historical_volatility(candidates)
    available = set(candidates)
    kept = set(filtered)
    reinstated = [
        symbol
        for symbol in self.config.popular_stocks()
        if symbol in available and symbol not in kept
    ]
    if reinstated:
        log.info(
            "LOAD   | reinstated %s popular symbols the volatility filter "
            "would have dropped | %s",
            len(reinstated),
            ",".join(reinstated),
        )
        filtered = list(dict.fromkeys(reinstated + filtered))
    return filtered
