import logging

log = logging.getLogger("webull-bot")


def handle_otc_extended_hours_unsupported(self, symbol: str, exc: Exception) -> None:
    if symbol in self.otc_extended_hours_unsupported_symbols:
        return
    self.otc_extended_hours_unsupported_symbols.add(symbol)
    log.warning(
        "OTC    | %-8s | this security has no extended-hours session "
        "- skipping pre/post-market entries for %s for the rest of "
        "this run (core-hours trading is unaffected) | %s",
        symbol,
        symbol,
        exc,
    )
