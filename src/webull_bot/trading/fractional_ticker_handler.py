import logging

log = logging.getLogger("webull-bot")


def handle_fractional_ticker_unsupported(self, symbol: str, exc: Exception) -> None:
    if symbol in self.fractional_unsupported_symbols:
        return
    self.fractional_unsupported_symbols.add(symbol)
    log.warning(
        "FRACT  | %-8s | this security doesn't support fractional "
        "trading - falling back to whole-share sizing for %s for the "
        "rest of this run | %s",
        symbol,
        symbol,
        exc,
    )
