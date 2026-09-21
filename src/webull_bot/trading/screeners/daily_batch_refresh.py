import logging
from datetime import datetime
from decimal import Decimal

log = logging.getLogger("webull-bot")


def refresh_daily_batch(self, moment: datetime) -> None:
    """By explicit request: "only take a good batch of stocks to look
    out for everyday, do online research for that."

    Stage one of a two-stage funnel - this builds the morning
    shortlist, and select_focus_symbol picks the single all-in name
    out of it once the opening range has resolved. Before this, the
    candidate pool for any given decision was "whatever the scanner
    happened to surface from a ~5000-symbol universe this cycle",
    which is both noisy and slow.

    Size is taken from day-trading practice rather than guessed: a
    daily momentum watchlist that changes every day should run 5-10
    names, and past ~50 the documented result is analysis paralysis
    and poor execution. daily_batch_size defaults to 8.

    On "do online research" - the sources below are deliberately
    DETERMINISTIC rather than a live web search. Webull's own
    rank_type="PRE_MARKET" screener is itself the catalyst signal: a
    stock gapping on real pre-market volume is gapping BECAUSE of
    news (earnings, guidance, FDA, an upgrade), so the gap captures
    the catalyst without needing to read the headline. The research
    agent was deliberately moved OFF web search earlier in this
    project (see research_agent_settings.py - Groq's tool-
    orchestration layer was "pure overhead... the actual source of
    the truncated/malformed/empty responses"), and re-adding it here
    would reintroduce that failure mode plus unbounded token spend
    against an 83-request/day budget. The agent keeps its current,
    working role: scoring and ranking this state via priority_score.

    Note what is NOT gated here: relative volume. RVOL is derived
    from volume_delta_latest/volume_delta_ema, which are built by
    diffing consecutive intraday snapshots - at 08:45 there simply
    aren't enough regular-session samples for that to mean anything.
    RVOL is therefore enforced at focus-lock time instead, where it
    is real. This stage gates on the gap, which IS meaningful
    pre-market.
    """
    if self.daily_batch_date == moment.date():
        return
    if not self.config.focus_mode_enabled:
        return
    if moment < self.session_moment(moment, self.config.daily_batch_refresh_time):
        # Not yet - the pre-market tape this reads isn't meaningful
        # until the 08:30-09:30 window where volume and catalyst
        # releases actually cluster. Deliberately NOT date-stamped on
        # this branch, so it retries on the next cycle instead of
        # marking the day done before the batch was ever built.
        return
    # Deliberately NOT date-stamped until a non-empty batch actually
    # exists (see the end of this function).
    #
    # Stamping up front looks equivalent and is not: this reads
    # strategy.metrics, which is populated by the scan loop, so a
    # container that STARTS after daily_batch_refresh_time - every
    # mid-session deploy - runs this on its very first cycle with
    # metrics still empty, produces an empty batch, marks the day
    # done, and then never trades again that session. Retrying
    # until focus_lock_time gives the scan loop time to warm up.
    # Every already-wired morning source, unioned. Each of these is
    # refreshed by its own once-daily screener before this runs.
    candidates = (
        set(self.premarket_gainers)
        | set(self.agent_predicted_gainers)
        | set(self.seed_popular_symbols)
        | set(self.agent_popular_symbols)
    )
    for bucket in self.market_pulse_cache.values():
        for row in bucket:
            symbol = str(row.get("symbol", "")).upper()
            if symbol:
                candidates.add(symbol)
    scored: list[tuple[float, str, dict]] = []
    for symbol in candidates:
        metrics = self.strategy.metrics.get(symbol)
        price = self.strategy.prices.get(symbol)
        if not metrics or price is None or price <= 0:
            # No quote yet this session - can't judge it, so it
            # doesn't make the batch. It stays in the universe and
            # can be picked up tomorrow.
            continue
        if not (self.config.focus_min_price <= price <= self.config.focus_max_price):
            continue
        if metrics.get("volume", 0) < self.config.popular_stock_min_volume:
            continue
        spread_percent = Decimal(str(metrics.get("spread_percent", "999")))
        if spread_percent > self.config.popular_stock_max_spread_percent:
            continue
        # change_ratio is already an absolute value (see update_stock_
        # snapshot, which maxes the regular and extended-hours ratios),
        # so a gap DOWN qualifies exactly like a gap up - this bot can
        # trade either direction via puts, so direction is the entry
        # signal's business, not the batch's.
        gap_percent = Decimal(str(metrics.get("change_ratio", 0))) * 100
        if gap_percent < self.config.daily_batch_min_gap_percent:
            continue
        score = self.strategy.priority_score(symbol, self.agent_assessment(symbol))
        scored.append(
            (
                score,
                symbol,
                {"gap": gap_percent, "spread": spread_percent, "score": score},
            )
        )
    scored.sort(key=lambda row: row[0], reverse=True)
    selected = scored[: self.config.daily_batch_size]
    self.daily_batch = [symbol for _, symbol, _ in selected]
    if not self.daily_batch:
        # Keep retrying while there is still time for a pick to
        # matter; give up (and stamp) once focus_lock_time has
        # passed, so a genuinely weak morning stops re-scanning
        # every cycle for the rest of the day.
        give_up = moment >= self.session_moment(
            moment, self.config.focus_lock_time
        )
        if give_up:
            self.daily_batch_date = moment.date()
        if not self.daily_batch_logged_empty_date == moment.date():
            self.daily_batch_logged_empty_date = moment.date()
            log.info(
                "BATCH  | no candidate cleared the daily-batch gates "
                "(gap>=%s%% | volume>=%s | spread<=%s%% | price %s-%s) | "
                "%s candidates considered | %s",
                self.config.daily_batch_min_gap_percent,
                self.config.popular_stock_min_volume,
                self.config.popular_stock_max_spread_percent,
                self.config.focus_min_price,
                self.config.focus_max_price,
                len(candidates),
                "giving up for today" if give_up else "will retry",
            )
        return
    self.daily_batch_date = moment.date()
    log.info(
        "BATCH  | %s of %s candidates | %s",
        len(self.daily_batch),
        len(candidates),
        " | ".join(
            f"{symbol} gap={detail['gap']:.1f}% "
            f"spread={detail['spread']:.2f}% score={detail['score']:.1f}"
            for _, symbol, detail in selected
        ),
    )
