import logging

log = logging.getLogger("webull-bot")


def handle_broker_conflict(self, symbol: str, exc: Exception) -> None:
    self.broker_conflict_symbols.add(symbol)
    self.pending_stock_exits.discard(symbol)
    self.pending_option_exits.discard(symbol)
    self.stop_exit_submitted.pop(symbol, None)
    self.stop_loss_escalated.discard(symbol)
    self.stop_condition_since.pop(symbol, None)
    log.error(
        "CONFLICT | %-8s | broker rejected order as a position reverse "
        "- our view of this position doesn't match the account. Pausing "
        "automated action on it for the rest of the day; check the "
        "Webull app for a stuck order or unexpected position on %s. | %s",
        symbol,
        symbol,
        exc,
    )
