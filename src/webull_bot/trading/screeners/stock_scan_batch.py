import logging
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from webull_bot.webull_api import MarketDataPermissionError, WebullAPI

log = logging.getLogger("webull-bot")


def _prepare_stock_scan_batch(
    self,
    positions: list[dict],
    buying_power: Decimal,
    opening_grace_active: bool,
    core_session_active: bool,
):
    """Builds this cycle's stock scan batch and fetches quotes for it,
    plus every other once-per-cycle (not per-symbol) gate/multiplier
    trade_stocks' per-symbol loop needs. See trade_stocks for how each
    of these is used.

    Returns a tuple of everything the per-symbol loop needs on success,
    or None if this cycle's quote batch could not be built at all -
    callers should treat None the same as the "return buying_power
    unchanged" case it replaces. A MarketDataPermissionError (or an
    alternate-category quote fetch re-raising one) propagates out of
    this function exactly as it did inline before this extraction -
    that is a systemic, account-level problem, not a per-cycle one.
    """
    open_count = self.strategy.open_position_count(positions)
    # Superseded by the hard "no volatility scalp in extended hours"
    # gate on the fresh-entry and averaging-down blocks below (by
    # request, after pre-market losses) - fresh entries and
    # averaging now only ever fire when core_session_active, so
    # full intensity is always correct here; there's no longer a
    # dampened outside-core-hours case to compute.
    volatility_scalp_intensity = Decimal("1")
    volatility_scalp_effective_max_concurrent = max(
        1,
        int(
            self.config.volatility_scalp_max_concurrent_positions
            * volatility_scalp_intensity
        ),
    )
    volatility_scalp_effective_max_averaging = int(
        self.config.volatility_scalp_max_averaging_buys
        * volatility_scalp_intensity
    )
    # freqtrade-style StoplossGuard: too many recent stop-losses pauses
    # NEW entries only (unlike handle_portfolio_circuit_breaker, this
    # never liquidates existing positions) - see stop_loss_guard_active.
    guard_active = self.stop_loss_guard_active()
    # Generalizes option_market_regime_ok's VIXY-rolling-percentile
    # gate to stock entries - self.vixy_history is populated by
    # trade_options (which runs after trade_stocks each cycle), so
    # this reads one cycle stale; negligible for a slow-moving
    # market-wide signal. Computed once per cycle, not per symbol -
    # this is a market-wide read, not a per-symbol one.
    regime_gate_active = self.config.regime_gate_enabled and not (
        self.strategy.stock_market_regime_ok(
            self.vixy_history,
            self.vixy_history[-1] if self.vixy_history else None,
            self.config.regime_gate_reject_percentile,
        )
    )
    # Keeping cash deployed outranks entry quality, but only
    # progressively - see idle_cash_ramp_progress(). ramp_progress is
    # 0 right after any entry, climbing to 1 the longer buying_power
    # sits idle above MIN_CASH_RESERVE_DOLLARS with nothing bought.
    ramp_progress = self.idle_cash_ramp_progress(buying_power)
    idle_relaxation_multiplier = Decimal("1") + ramp_progress * (
        self.config.idle_cash_max_gate_multiplier - Decimal("1")
    )
    idle_relaxation_amount = ramp_progress * self.config.idle_cash_max_tick_relaxation
    # By request: "stay in a significant profit until eod" -> "let
    # winners run further before taking profit." See stock_
    # decision's profit_target_multiplier param.
    profit_target_multiplier = self.profit_target_multiplier(
        self.daily_realized_pnl,
        self.cached_account_value,
        self.config.daily_significant_profit_fraction,
        self.config.profit_target_widen_multiplier,
    )
    # By request: "when we have a certain profit we should also
    # not allow stops to be too low." Same trigger as above.
    stop_tighten_multiplier = self.stop_tighten_multiplier(
        self.daily_realized_pnl,
        self.cached_account_value,
        self.config.daily_significant_profit_fraction,
        self.config.stop_tighten_multiplier,
    )
    # By request, after live evidence (WNW/WKHS stopping out
    # shortly after core hours ended): block FRESH entries only
    # (not averaging down, not any exit) once fewer than
    # stock_entry_blackout_minutes_before_close minutes remain in
    # the core session - see the general BUY/SHORT and volatility-
    # scalp fresh-entry gates below. Computed once per cycle, not
    # per symbol - a market-wide clock read, not a per-symbol one,
    # same convention as regime_gate_active above. Irrelevant (and
    # harmless) whenever core_session_active is already False -
    # the existing "only established/popular symbols trade outside
    # core hours" gate already covers that case.
    moment = self.now()
    option_close_moment = self.session_moment(
        moment, self.config.option_market_close_time
    )
    minutes_until_close = (
        option_close_moment - moment
    ).total_seconds() / 60
    fresh_entry_blackout_active = self.fresh_entry_blackout_active(
        minutes_until_close,
        float(self.config.stock_entry_blackout_minutes_before_close),
        core_session_active,
    )
    # By request: "keep it separate, all the bp should be for
    # option, then at 9am cst whatever is remaining can be used
    # for stocks" -> refined to "first we want an option trade to
    # occur, and the stock trading should start later, although
    # you can sell stocks anytime." Same fresh-entries-only scope
    # as the blackout above (reuses the same fresh_entry_blackout_
    # active gate at every one of its existing call sites below;
    # exits are never affected by any of this) - blocks new stock
    # positions from claiming equity/margin until EITHER a real
    # option entry has landed today (self.option_entry_occurred_
    # today, set by record_trade) OR stock_entry_options_priority_
    # minutes have passed, whichever comes first. The time fallback
    # exists so a day with zero qualifying option candidates
    # doesn't lock fresh stock entries out for the entire session.
    option_open_moment = self.session_moment(
        moment, self.config.option_market_open_time
    )
    minutes_since_open = (moment - option_open_moment).total_seconds() / 60
    fresh_entry_blackout_active = (
        fresh_entry_blackout_active
        or (
            not self.option_entry_occurred_today
            and self.options_priority_window_active(
                minutes_since_open,
                float(self.config.stock_entry_options_priority_minutes),
                core_session_active,
            )
        )
    )
    # By request: "do not allow more than 20% in stocks." Same
    # fresh-entries-only scope as everything else folded into
    # fresh_entry_blackout_active above - blocks new stock
    # positions (and averaging down) once total stock exposure
    # already meets or exceeds the cap; every exit is unaffected.
    fresh_entry_blackout_active = (
        fresh_entry_blackout_active
        or self.stock_total_exposure_at_cap(
            self.cached_account_value,
            positions,
            self.config.stock_max_total_exposure_fraction,
        )
    )
    # By request: "start transitioning away from core hours
    # strategy around 30 minutes before end of core hours." Softer
    # than fresh_entry_blackout_active above (which HARD-blocks
    # every fresh entry once inside its own, shorter window) - this
    # instead makes the last late_core_session_transition_minutes
    # of core hours behave like extended hours for entry-quality
    # purposes: only the POPULAR bucket gets fresh entries (same
    # "only established/popular symbols trade outside core hours"
    # gate the general BUY/SHORT paths already have), and the
    # spread tolerance widens the same way it does outside core
    # hours (via effective_core_session_active, passed to stock_
    # decision in place of the real core_session_active). Position
    # management/exits are completely unaffected - only entry
    # QUALITY winds down early, nothing stops working.
    late_core_session_transition_active = (
        core_session_active
        and 0
        <= minutes_until_close
        < float(self.config.late_core_session_transition_minutes)
    )
    effective_core_session_active = (
        core_session_active and not late_core_session_transition_active
    )
    # See evaluate_held_stock_exits/cached_opening_grace_active's
    # __init__ comment - refreshed every cycle so the fast
    # position-protection thread always reads this cycle's values.
    self.cached_opening_grace_active = opening_grace_active
    self.cached_idle_relaxation_multiplier = idle_relaxation_multiplier
    self.cached_idle_relaxation_amount = idle_relaxation_amount
    self.cached_effective_core_session_active = effective_core_session_active
    self.cached_profit_target_multiplier = profit_target_multiplier
    self.cached_stop_tighten_multiplier = stop_tighten_multiplier
    self.refresh_agent_discoveries()
    if self.analyst_service is not None:
        # One cycle stale relative to any fetch a symbol's own
        # request() call below just queued - same acceptable
        # staleness as regime_gate_active's vixy_history read above,
        # and more so here since analyst data moves far slower than
        # VIXY. Cheap, in-memory only - see AnalystDataService.snapshot.
        self.strategy.analyst_priority = self.analyst_service.snapshot()
    # By request: "scan through all [the universe]... split it up
    # in parallel streams... as many as needed to scan everything
    # and filter it down, then dynamically less as it is filtered
    # down... does not need to be as intense in extended hours."
    # One rotation of prioritized_stock_batch previously covered
    # only a single STOCK_BATCH_SIZE slice of a universe that can
    # be much larger (up to MAX_SYMBOLS) - see
    # stock_scan_concurrent_batches for how the rotation count
    # scales with the universe size, dynamically fewer once it
    # stops growing, and reduced further outside core hours.
    # Deduped while preserving order across rotations (a symbol
    # could legitimately repeat if the cursor wraps within one
    # cycle on a small/shrunk universe).
    scan_watch_symbols = (
        self.seed_popular_symbols | self.agent_popular_symbols | self.user_watchlist
    )
    concurrent_batches = self.strategy.stock_scan_concurrent_batches(
        len(self.stock_symbols), core_session_active
    )
    batch = []
    seen_in_batch: set[str] = set()
    for _ in range(concurrent_batches):
        rotation, self.stock_cursor = self.strategy.prioritized_stock_batch(
            self.stock_symbols,
            self.stock_cursor,
            positions,
            self.agent_assessment,
            scan_watch_symbols,
        )
        if not rotation:
            break
        for symbol in rotation:
            if symbol not in seen_in_batch:
                seen_in_batch.add(symbol)
                batch.append(symbol)
    if self.priority_scan_symbols:
        # A symbol just added via the dashboard has zero accumulated
        # activity score yet, so it ranks at the very bottom of
        # prioritized_stock_batch's popular/penny scoring and can
        # lose out to every already-active watchlist symbol every
        # single cycle - live incident: HOWL, added manually, never
        # once appeared in a scan batch. Force it into THIS batch
        # once, regardless of ranking, so a manual add is guaranteed
        # to actually get looked at.
        injected = [
            symbol
            for symbol in self.priority_scan_symbols
            if symbol in self.stock_symbols and symbol not in batch
        ]
        if injected:
            batch = list(batch) + injected
        self.priority_scan_symbols.clear()
    # premarket_gainers included here too - by request, "get the
    # top gainers before the day starts and look to invest in that
    # for quick profit" - today's actual pre-market movers get
    # scanned every cycle instead of only via prioritized_stock_
    # batch's normal ranking, same reasoning as the volatility-
    # scalp cohort just below.
    force_scan = (
        self.volatility_scalp_symbols
        | self.volatility_scalp_recently_eligible
        | self.premarket_gainers
        | self.agent_predicted_gainers
    )
    if force_scan:
        # Left to prioritized_stock_batch's normal ranking, any one
        # of these might only get re-evaluated once every several
        # cycles, which can't support "multiple times a minute"
        # trading. Force every one of them into every single cycle's
        # batch (not one-time, unlike priority_scan_symbols above) so
        # entry/exit decisions always run through the same, single,
        # correct code path below - no separate/duplicated logic
        # needed. Covers both the curated cohort (a small priority
        # subset) AND every symbol that was volatility-scalp
        # eligible the last time it was scanned - by request, "make
        # sure the data is received as frequently as possible" for
        # the whole broadened set, not just the curated handful.
        missing = [
            symbol
            for symbol in force_scan
            if symbol in self.stock_symbols and symbol not in batch
        ]
        if missing:
            batch = list(batch) + missing
    # Safety net, unconditional: ANY symbol with a real nonzero
    # EQUITY position must get an exit decision every single cycle,
    # regardless of whether it's currently in stock_symbols at all.
    # Live incident: MYND, a real held position, fell out of the
    # daily volatility-filtered scan universe (VOLFILT keeps only
    # ~200 of the full universe) and simply stopped being evaluated
    # - no stop-loss, no profit-target, nothing - while it kept
    # sliding to an 11%+ unrealized loss with zero protective
    # action taken. A position already being risked with real money
    # must never depend on still being in the day's scan list to
    # get managed.
    held_symbols = [
        str(item.get("symbol", "")).upper()
        for item in positions
        if item.get("instrument_type") == "EQUITY"
        and Decimal(str(item.get("quantity", "0"))) != 0
    ]
    unmanaged_held = [
        symbol for symbol in held_symbols if symbol and symbol not in batch
    ]
    if unmanaged_held:
        # Throttled to once per symbol while the condition persists
        # (same pattern as wash_skip_logged) - this still fires
        # every single cycle underneath (the batch-injection itself
        # is unconditional and unaffected), only the WARNING log
        # line is deduped. Live incident: BSEM/GWRS sat outside the
        # scan universe for hours, logging the identical warning
        # every ~5s (3000+ times in one session) and burying real
        # signal in the noise, even though the guard itself was
        # working correctly the whole time.
        newly_unmanaged = sorted(
            set(unmanaged_held) - self.unmanaged_held_logged
        )
        if newly_unmanaged:
            log.warning(
                "GUARD  | %s held position(s) fell out of the scanned "
                "universe - forcing them back into the batch so exit "
                "management resumes | %s",
                len(newly_unmanaged),
                ",".join(newly_unmanaged),
            )
        self.unmanaged_held_logged = set(unmanaged_held)
        batch = list(batch) + unmanaged_held
    else:
        # Cleared once every held symbol is back in the scan
        # universe, so a later recurrence (a different symbol, or
        # the same one falling out again after recovering) logs
        # again instead of staying silent forever.
        self.unmanaged_held_logged = set()
    # Live incident: force-injecting the curated cohort AND every
    # volatility-scalp-eligible symbol (self.volatility_scalp_
    # symbols | self.volatility_scalp_recently_eligible, above) on
    # top of an already-full stock_batch_size batch pushed the
    # combined batch size past Webull's own hard 100-symbol snapshot
    # limit (WebullAPI.stock_quotes) - the ENTIRE quote fetch for
    # that cycle then raised and failed, losing price data for every
    # symbol in the batch, not just the extra ones.
    batch = self.cap_batch_to_snapshot_limit(
        batch,
        unmanaged_held,
        limit=concurrent_batches * WebullAPI.STOCK_SNAPSHOT_MAX_SYMBOLS,
    )
    self.last_scan_batch_size = len(batch)
    # Scoped to this cycle's actual scan batch, not the whole
    # (possibly thousands-strong) universe - refresh_recent_momentum
    # already throttles itself, but a full-universe fetch every 120s
    # would be real, avoidable API load for symbols that aren't even
    # being evaluated for entries this cycle.
    self.refresh_recent_momentum(list(batch))
    self.refresh_multi_day_momentum(list(batch))
    bucket_remaining = {
        bucket: buying_power * fraction
        for bucket, fraction in self.config.stock_capital_fractions().items()
    }
    bucket_slot_limits = self.config.stock_bucket_slot_limits()
    bucket_position_counts = {bucket: 0 for bucket in bucket_slot_limits}
    # Two independent capital pools for this cycle, computed once (not
    # re-derived from a live-shrinking buying_power on every candidate)
    # so fractional and whole-share sizing genuinely run side by side -
    # see size_stock_entry. Previously fractional sizing was tried for
    # every eligible candidate and almost always succeeded, so
    # whole-share sizing (a LARGER capital slice than fractional's) was
    # essentially unreachable during core hours.
    fractional_remaining = (
        buying_power * self.config.stock_core_session_position_fraction
    )
    whole_share_remaining = (
        buying_power * self.config.stock_whole_share_core_session_fraction
    )
    max_fractional_positions = self.max_fractional_position_slots(
        self.config.max_open_positions,
        self.config.stock_core_session_position_fraction,
        self.config.stock_whole_share_core_session_fraction,
    )
    fractional_position_count = 0
    known_popular = self.seed_popular_symbols | self.agent_popular_symbols | self.user_watchlist
    for position in positions:
        if (
            position.get("instrument_type") != "EQUITY"
            or Decimal(str(position.get("quantity", "0"))) == 0
        ):
            continue
        if self.is_fractional_quantity(Decimal(str(position.get("quantity", "0")))):
            fractional_position_count += 1
        position_symbol = str(position.get("symbol", "")).upper()
        bucket = self.position_buckets.get(position_symbol)
        if bucket not in bucket_position_counts:
            position_price = Decimal(
                str(
                    self.strategy.prices.get(
                        position_symbol,
                        position.get("cost_price", "0"),
                    )
                )
            )
            if position_symbol in known_popular:
                bucket = "POPULAR"
            elif (
                position_price > 0
                and position_price < self.config.penny_stock_max_price
            ):
                bucket = "PENNY"
            else:
                bucket = "DISCOVERY"
            self.position_buckets[position_symbol] = bucket
        bucket_position_counts[bucket] += 1
    quotes: list[dict] = []
    invalid: set[str] = set()
    grouped: dict[str, list[str]] = {"US_STOCK": [], "US_ETF": []}
    for symbol in batch:
        grouped[self.stock_categories.get(symbol, "US_STOCK")].append(symbol)
    # By request: "scan through all [the universe]... split it up
    # in parallel streams" - a large batch (now up to
    # concurrent_batches * STOCK_SNAPSHOT_MAX_SYMBOLS symbols, see
    # above) is chunked back down to Webull's own per-call cap and
    # every chunk's quote fetch fires CONCURRENTLY, instead of one
    # chunk waiting out the previous chunk's full round-trip first.
    # A single chunk's own failure only drops that chunk's symbols
    # (same "one group's failure shouldn't cost every other
    # group's data" convention _batched_quotes already uses) -
    # except MarketDataPermissionError, which is a systemic
    # account-level problem, not a per-chunk one, and must still
    # propagate/stop the bot exactly like before this change.
    chunks: list[tuple[str, list[str]]] = []
    for category, category_symbols in grouped.items():
        for start in range(0, len(category_symbols), WebullAPI.STOCK_SNAPSHOT_MAX_SYMBOLS):
            chunk_symbols = category_symbols[
                start : start + WebullAPI.STOCK_SNAPSHOT_MAX_SYMBOLS
            ]
            if chunk_symbols:
                chunks.append((category, chunk_symbols))
    chunk_results: list[tuple[list[dict], set[str]] | None] = [None] * len(chunks)
    chunk_errors: list[Exception | None] = [None] * len(chunks)

    def _fetch_chunk(index: int) -> None:
        category, chunk_symbols = chunks[index]
        try:
            chunk_results[index] = self.api.stock_quotes_resilient(
                chunk_symbols, category
            )
        except Exception as exc:
            chunk_errors[index] = exc

    if chunks:
        with ThreadPoolExecutor(
            max_workers=min(len(chunks), self.config.stock_scan_max_concurrent_batches)
        ) as pool:
            list(pool.map(_fetch_chunk, range(len(chunks))))
    permission_error = next(
        (exc for exc in chunk_errors if isinstance(exc, MarketDataPermissionError)),
        None,
    )
    if permission_error is not None:
        raise permission_error
    if chunks and all(err is not None for err in chunk_errors):
        # Every single chunk failed (not just one) - same "give up
        # this cycle" behavior the old single-call version had on
        # any failure, since there's no usable data at all.
        log.error(
            "STOCKS | quote batch failed | %s", chunk_errors[0]
        )
        return None
    for index, (category, _chunk_symbols) in enumerate(chunks):
        if chunk_errors[index] is not None:
            log.warning(
                "STOCKS | quote chunk failed | %s | %s",
                category,
                chunk_errors[index],
            )
            continue
        category_quotes, category_invalid = chunk_results[index]
        quotes.extend(category_quotes)
        if category_invalid:
            if self.config.exclude_etfs and category == "US_STOCK":
                invalid.update(category_invalid)
                continue
            alternate = "US_ETF" if category == "US_STOCK" else "US_STOCK"
            try:
                alternate_quotes, alternate_invalid = (
                    self.api.stock_quotes_resilient(
                        sorted(category_invalid),
                        alternate,
                    )
                )
            except Exception as exc:
                if isinstance(exc, MarketDataPermissionError):
                    raise
                log.warning(
                    "STOCKS | alternate-category quote fetch failed | %s | %s",
                    alternate,
                    exc,
                )
                invalid.update(category_invalid)
                continue
            quotes.extend(alternate_quotes)
            corrected = category_invalid - alternate_invalid
            for symbol in corrected:
                self.stock_categories[symbol] = alternate
            invalid.update(alternate_invalid)
    if invalid:
        self.invalid_stock_symbols.update(invalid)
        self.invalid_symbols.add(invalid)
        self.stock_symbols = [
            symbol for symbol in self.stock_symbols if symbol not in invalid
        ]
        replacements = self.backfill_stock_symbols(len(invalid))
        self.stock_cursor %= max(1, len(self.stock_symbols))
        log.warning(
            "SKIP   | invalid=%s | %s | backfilled=%s",
            len(invalid),
            ",".join(sorted(invalid)),
            replacements,
        )
    quote_by_symbol = {
        str(quote.get("symbol", "")).upper(): quote for quote in quotes
    }
    if (
        self.config.volatility_scalp_enabled
        and self.config.volatility_scalp_bar_seed_enabled
    ):
        self.seed_volatility_windows(
            [symbol for symbol in batch if symbol in quote_by_symbol]
        )
    batch_moment = time.monotonic()
    return (
        batch,
        quote_by_symbol,
        batch_moment,
        open_count,
        guard_active,
        regime_gate_active,
        idle_relaxation_multiplier,
        idle_relaxation_amount,
        profit_target_multiplier,
        stop_tighten_multiplier,
        fresh_entry_blackout_active,
        effective_core_session_active,
        bucket_remaining,
        bucket_slot_limits,
        bucket_position_counts,
        fractional_remaining,
        whole_share_remaining,
        max_fractional_positions,
        fractional_position_count,
        volatility_scalp_effective_max_concurrent,
        volatility_scalp_effective_max_averaging,
    )
