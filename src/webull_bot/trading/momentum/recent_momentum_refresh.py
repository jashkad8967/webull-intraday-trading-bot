import logging
import time
from decimal import Decimal

log = logging.getLogger("webull-bot")


def refresh_recent_momentum(self, symbols: list[str]) -> None:
    """By request: "look at tickers in the last 10 mins for
    momentum... to analyze the upcoming trend." Real 1-minute bar
    closes (same source recent_minute_closes/seed_volatility_windows
    already use), NOT the blended live-tick volatility_price_history
    window - that window's sample spacing tracks scan cadence, not
    wall-clock time, so it can't reliably represent a genuine "last
    10 minutes." Own throttle (RECENT_MOMENTUM_REFRESH_SECONDS, much
    more frequent than the once-daily SMA refresh, since a 10-minute
    signal goes stale fast) - see its call site in run(). Merges
    into the existing cache rather than replacing it outright, same
    "a partial/failed refresh degrades gracefully" convention as
    refresh_sma_trend.
    """
    if not self.config.recent_momentum_filter_enabled or not symbols:
        return
    now = time.monotonic()
    if (
        now - self.last_recent_momentum_refresh
        < float(self.config.recent_momentum_refresh_seconds)
    ):
        return
    self.last_recent_momentum_refresh = now
    try:
        closes_by_symbol = self.api.recent_minute_closes(
            symbols,
            "US_STOCK",
            self.config.recent_momentum_lookback_minutes,
        )
    except Exception as exc:
        log.warning("MOMENTUM| recent refresh failed this cycle | %s", exc)
        return
    if not closes_by_symbol:
        log.warning("MOMENTUM| no coverage this cycle | keeping prior values")
        return
    updated = 0
    for symbol, closes in closes_by_symbol.items():
        if len(closes) < 2 or closes[0] <= 0:
            continue
        self.strategy.recent_momentum[symbol] = Decimal(
            str((closes[-1] - closes[0]) / closes[0])
        )
        updated += 1
    log.info(
        "MOMENTUM| recent momentum refreshed | %s/%s symbols | lookback=%sm",
        updated,
        len(symbols),
        self.config.recent_momentum_lookback_minutes,
    )
