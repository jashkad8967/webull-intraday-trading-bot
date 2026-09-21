import logging
import time
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

    By explicit request, restated in full after two live incidents
    picked names with no real option market: "the stocks we pick
    should be like fortune 500, or snp, or dow stocks, popular,
    known, established, and then look for volume, volatility,
    momentum, news, and make the pick, then scan contracts for the
    pick that are also volatile and popular, but within account
    buying power." GRML (286.7% gap) and GRAL both cleared the gap/
    volume/spread gates below - genuinely unusual moves - but neither
    is the kind of name that carries a real, liquid options market;
    the gates measured whether the STOCK looked interesting, not
    whether it was a name any options desk would recognize. The
    candidate pool is therefore intersected with config.option_
    candidates() - the same curated, large-cap-heavy S&P/Dow-style
    list discover_option_contracts already uses, established and
    liquid by construction - rather than the raw union of every
    gainer/mover screener, which includes any thinly-traded name that
    happened to move today regardless of whether it has options at
    all. daily_batch_require_established_symbols is the off switch,
    default on.

    Note what is NOT gated here: relative volume. RVOL is derived
    from volume_delta_latest/volume_delta_ema, which are built by
    diffing consecutive intraday snapshots - at daily_batch_refresh_
    time there simply aren't enough regular-session samples for that
    to mean anything.
    RVOL is therefore enforced at focus-lock time instead, where it
    is real. This stage gates on the gap, which IS meaningful
    pre-market.
    """
    if self.daily_batch_date == moment.date():
        return
    if not self.config.focus_mode_enabled:
        return
    if self.daily_batch_first_attempt_date != moment.date():
        # A new day - a leftover attempt timestamp from a prior day's
        # give-up would otherwise make time.monotonic()'s elapsed
        # value look enormous (monotonic time never resets), which
        # would trip the retry-minutes give-up on literally the first
        # attempt of the new day.
        self.daily_batch_first_attempt_date = moment.date()
        self.daily_batch_first_attempt_at = None
    if moment < self.session_moment(moment, self.config.daily_batch_refresh_time):
        # Not yet - the pre-market tape this reads isn't meaningful
        # until the 08:30-09:30 ET window (a real market-structure
        # fact, independent of whatever zone this bot's own clock
        # runs on) where volume and catalyst releases actually
        # cluster. Deliberately NOT date-stamped on
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
    if self.config.daily_batch_require_established_symbols:
        candidates &= set(self.config.option_candidates())
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
        # Live incident: give-up was originally tied to focus_lock_
        # time (09:45) directly against wall-clock `moment`. That
        # silently broke on any restart landing after 09:45 - which
        # covers every mid-session redeploy, exactly the case this
        # whole retry mechanism exists for - because the FIRST
        # attempt post-restart was already past the cutoff, giving up
        # permanently with zero real retries. Track elapsed time
        # since THIS attempt-loop's own first try instead, so a late
        # restart still gets a real warm-up window regardless of what
        # the wall clock already says. option_eod_close_time remains
        # a hard backstop - a batch built with no real session left
        # to trade it in is pointless regardless of elapsed time.
        # The retry window is anchored at focus_lock_time, not at the
        # first attempt of the morning, so pre-market attempts can
        # never burn it. The batch's first attempt is at daily_batch_
        # refresh_time, a full hour before the lock, against a default
        # 20-minute window - so counting from then, a quiet pre-market
        # marked the whole day done ~40 minutes BEFORE the lock and ~25
        # before the opening bell had even printed the regular-session
        # gaps this screens on, and nothing could trade for the rest of
        # that session. Anchoring at the lock also avoids the opposite
        # error of merely gating give-up on the lock: the window would
        # already be spent by 08:45, so the batch would quit at the
        # very instant the post-bell data it needs became available.
        # "Nothing qualifies yet at 07:45" is the normal quiet pre-
        # market state, not a verdict.
        past_lock = moment >= self.session_moment(
            moment, self.config.focus_lock_time
        )
        if past_lock and self.daily_batch_first_attempt_at is None:
            self.daily_batch_first_attempt_at = time.monotonic()
        elapsed_minutes = (
            0.0
            if self.daily_batch_first_attempt_at is None
            else (time.monotonic() - self.daily_batch_first_attempt_at) / 60
        )
        give_up = (
            (past_lock and elapsed_minutes >= self.config.daily_batch_retry_minutes)
            or moment >= self.session_moment(
                moment, self.config.option_eod_close_time
            )
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
    self.daily_batch_first_attempt_at = None
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
