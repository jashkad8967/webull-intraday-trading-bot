import logging

log = logging.getLogger("webull-bot")


def handle_fractional_trading_not_enabled(self, exc: Exception) -> None:
    if not self.fractional_trading_enabled:
        return
    self.fractional_trading_enabled = False
    log.error(
        "FRACT  | fractional orders rejected - this Webull account "
        "hasn't agreed to fractional trading yet. Falling back to "
        "whole-share sizing for the rest of this run; open the "
        "agreement link below in the Webull app/website once, then "
        "restart the bot to re-enable dollar-sized core-session "
        "entries and the fractional-shares fallback. | %s",
        exc,
    )
