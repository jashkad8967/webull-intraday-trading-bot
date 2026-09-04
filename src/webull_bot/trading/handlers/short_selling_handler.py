import logging

log = logging.getLogger("webull-bot")


def handle_short_selling_unsupported(self, exc: Exception) -> None:
    if not self.short_selling_supported:
        return
    self.short_selling_supported = False
    log.error(
        "SHORT  | short selling rejected - this account is under "
        "Webull's $2,000 equity minimum for short selling. Disabling "
        "new short entries for the rest of this run; restart the bot "
        "once equity clears that minimum to re-enable them. | %s",
        exc,
    )
