import logging
import time

log = logging.getLogger("webull-bot")


def refresh_multi_day_momentum(self, symbols: list[str]) -> None:
    """By request: "also include not only short term patterns like
    5-10 mins, but also 1 day and 5 day and month." Real daily-bar
    closes (WebullAPI.daily_closes), refreshed on its own cadence
    (MULTI_DAY_MOMENTUM_REFRESH_SECONDS, default 30 min - far less
    volatile than the 10-minute momentum read, doesn't need to
    chase every scan cycle). Same "merge into the existing cache,
    degrade gracefully on a partial/failed refresh" convention as
    refresh_sma_trend/refresh_recent_momentum.

    Live incident (this bug): the throttle used to be a single
    GLOBAL timestamp gating the whole call, including for symbols
    with NO cached data at all - so after every restart (this
    session redeployed often), only whichever symbols happened to
    be in the very first post-restart scan batch ever got their
    daily_closes populated; everything scanned afterward sat with
    no data for up to the full 30-minute window, during which
    multi_day_momentum_supports_entry's extension guard (see its
    own docstring - built specifically to stop buying a "dip" that's
    still mid-unwind of a huge intraday spike) fails OPEN with no
    data and can't do anything. VIOT was bought ~74% above its
    prior close 10 minutes after a restart, in exactly this gap.
    Now symbols with no cached entry yet are ALWAYS fetched
    immediately regardless of the throttle (closing the cold-start
    gap); the throttle only limits how often an ALREADY-cached
    symbol gets re-fetched (daily bars barely change intra-session,
    so that part still doesn't need to chase every cycle).
    """
    if not self.config.multi_day_momentum_filter_enabled or not symbols:
        return
    now = time.monotonic()
    uncached = [
        symbol for symbol in symbols if symbol not in self.strategy.daily_closes
    ]
    if (
        not uncached
        and now - self.last_multi_day_momentum_refresh
        < float(self.config.multi_day_momentum_refresh_seconds)
    ):
        return
    to_fetch = uncached if uncached else symbols
    self.last_multi_day_momentum_refresh = now
    try:
        closes_by_symbol = self.api.daily_closes(
            to_fetch, self.config.multi_day_momentum_lookback_days
        )
    except Exception as exc:
        log.warning("MOMENTUM| multi-day refresh failed | %s", exc)
        return
    if not closes_by_symbol:
        log.warning(
            "MOMENTUM| no multi-day coverage this cycle | keeping prior "
            "values"
        )
        return
    self.strategy.daily_closes.update(closes_by_symbol)
    log.info(
        "MOMENTUM| multi-day momentum refreshed | %s/%s symbols | "
        "lookback=%sd",
        len(closes_by_symbol),
        len(to_fetch),
        self.config.multi_day_momentum_lookback_days,
    )
