import logging

log = logging.getLogger("webull-bot")


def add_to_watchlist(self, symbol: str) -> None:
    symbol = str(symbol).upper().strip()
    if not symbol:
        return
    self.user_watchlist.add(symbol)
    if symbol not in self.stock_categories:
        try:
            categories = self.api.stock_categories([symbol])
        except Exception as exc:
            log.error(
                "CMD    | watchlist category lookup failed | %-8s | %s",
                symbol,
                exc,
            )
            categories = {}
        self.stock_categories[symbol] = categories.get(symbol, "US_STOCK")
    if symbol not in self.stock_symbols:
        self.stock_symbols.append(symbol)
    self.priority_scan_symbols.add(symbol)
    log.warning("CMD    | added %-8s to watchlist from dashboard", symbol)
