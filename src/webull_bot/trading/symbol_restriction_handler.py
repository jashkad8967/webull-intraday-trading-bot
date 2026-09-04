import logging

log = logging.getLogger("webull-bot")


def handle_symbol_restricted_to_closing_only(
    self, symbol: str, exc: Exception
) -> None:
    if symbol in self.entry_restricted_symbols:
        return
    self.entry_restricted_symbols.add(symbol)
    log.warning(
        "GUARD  | %-8s | restricted to closing orders only by the "
        "broker - blocking new entries for %s for the rest of the "
        "day (existing positions still exit normally) | %s",
        symbol,
        symbol,
        exc,
    )
