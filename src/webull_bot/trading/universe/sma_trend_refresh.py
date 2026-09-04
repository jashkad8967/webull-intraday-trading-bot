import logging
from decimal import Decimal

log = logging.getLogger("webull-bot")


def refresh_sma_trend(self, symbols: list[str]) -> None:
    """Once-daily higher-timeframe trend reference (see
    TradingStrategy.sma_trend_supports_entry) - a real daily-bar SMA,
    not something derivable from the bot's own few-second tick polls.
    Merges into the existing cache rather than replacing it outright,
    so a partial/failed refresh degrades to yesterday's (still roughly
    valid) SMA instead of going empty and disabling the filter.
    """
    if not self.config.sma_trend_filter_enabled or not symbols:
        return
    try:
        sma = self.api.sma_trend(symbols, self.config.sma_trend_days)
    except Exception as exc:
        log.warning("SMA    | trend refresh failed this cycle | %s", exc)
        return
    if not sma:
        log.warning("SMA    | no coverage this cycle | keeping prior values")
        return
    self.strategy.sma_trend.update(
        {symbol: Decimal(str(value)) for symbol, value in sma.items()}
    )
    log.info(
        "SMA    | trend reference refreshed | %s/%s symbols | lookback=%sd",
        len(sma),
        len(symbols),
        self.config.sma_trend_days,
    )
