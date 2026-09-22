from decimal import Decimal


def priority_score(self, symbol: str, assessment: dict | None) -> float:
    score = self.activity.get(symbol, 0.0)
    # Reward symbols with a recurring back-and-forth pattern today (a
    # capped count of EMA direction flips) so the scanner keeps favoring
    # stocks that repeatedly create fresh scalp setups over ones that
    # already made their one move for the day.
    oscillation = min(20, self.crossover_counts.get(symbol, 0))
    score += oscillation * float(self.config.stock_oscillation_weight)
    if symbol in self.most_active_symbols:
        score += float(self.config.most_active_priority_bonus)
    score += self.analyst_priority.get(symbol, 0.0)
    if not assessment:
        return score
    confidence = float(assessment.get("confidence", 0))
    research_priority = float(assessment.get("priority", 0))
    spread_opportunity = float(
        assessment.get("spread_opportunity", 0)
    )
    quick_trade_score = float(assessment.get("quick_trade_score", 0))
    symbol_volatility = float(assessment.get("symbol_volatility", 0))
    expected_move = min(
        1.0,
        abs(float(assessment.get("expected_move_percent", 0))) / 5.0,
    )
    catalyst = abs(float(assessment.get("catalyst_strength", 0)))
    volatility = float(assessment.get("market_volatility", 0))
    return score + 2.0 * confidence * (
        research_priority
        + 1.5 * quick_trade_score
        + 1.25 * symbol_volatility
        + spread_opportunity
        + expected_move
        + catalyst
        + volatility
    )


def analyst_priority_bonus(
    price: Decimal,
    target_mean: Decimal | None,
    rating: dict | None,
    bonus_max: Decimal,
) -> Decimal:
    """A soft, two-sided priority_score nudge (see analyst_priority) -
    never blocks or forces anything, just re-ranks candidates already
    eligible on every other gate. Combines two independent -1..1
    signals, averaged:

    - Upside: how far below the analyst mean target the current price
      sits, clipped to +-50% so one stale or outlier target can't
      dominate, then rescaled so a 25%+ gap already reads as "fully"
      bullish on this axis (a bigger gap adds no further weight).
    - Rating lean: (bullish - bearish) analyst counts as a fraction of
      total coverage.

    Neutral (0) with no coverage on either signal - this must never
    become a de facto exclusion filter for the many penny/micro-cap
    names this bot trades that analysts simply don't cover.
    """
    if price <= 0:
        return Decimal("0")
    rating = rating or {}
    strong_buy = int(rating.get("strong_buy", 0))
    buy = int(rating.get("buy", 0))
    hold = int(rating.get("hold", 0))
    sell = int(rating.get("sell", 0))
    under_perform = int(rating.get("under_perform", 0))
    total = strong_buy + buy + hold + sell + under_perform
    rating_lean = (
        Decimal(strong_buy + buy - sell - under_perform) / Decimal(total)
        if total > 0
        else Decimal("0")
    )
    if target_mean and target_mean > 0:
        upside = (target_mean - price) / price
        upside = max(Decimal("-0.5"), min(Decimal("0.5"), upside))
        upside_lean = max(
            Decimal("-1"), min(Decimal("1"), upside / Decimal("0.25"))
        )
    else:
        upside_lean = Decimal("0")
    return (rating_lean + upside_lean) / Decimal("2") * bonus_max


def stock_scan_concurrent_batches(
    self,
    total_symbols: int,
    core_session_active: bool,
    focus_mode_suppressing_entries: bool = False,
) -> int:
    """How many STOCK_BATCH_SIZE-sized quote batches trade_stocks
    should fetch CONCURRENTLY this cycle - by request: "scan
    through all [the universe]... as many as needed to scan
    everything and filter it down, then dynamically less as it is
    filtered down... does not need to be as intense in extended
    hours."

    Scales with the current universe size: enough concurrent
    batches to cover the WHOLE universe once within roughly
    stock_scan_target_full_coverage_cycles cycles. As the universe
    shrinks (or simply stops growing once the daily download
    finishes), fewer batches are needed to hit the same coverage
    target - this is what "dynamically less as it is filtered
    down" means in terms of this function's only input, since
    prioritized_stock_batch's own activity-based ranking (not this
    function) is what actually concentrates real candidates within
    each batch.

    Halved (rounded down, floor 1) outside core hours via
    stock_scan_extended_hours_concurrency_fraction - by request,
    matches the existing "no volatility scalp in extended hours...
    only established/popular symbols" philosophy of scanning less
    aggressively when there's less real opportunity to act on it.

    Bounded by stock_scan_max_concurrent_batches regardless of
    universe size - a hard safety cap on real Webull API request
    volume, given it already returned live 429 TOO_MANY_REQUESTS
    errors this session.

    By explicit request ("i want this to be faster more high
    frequency trades"): pinned to a single batch when focus mode has
    suspended new stock entries. Live evidence showed "direction
    signals" (the option entry timing signal) refreshing only every
    40-90+ seconds despite poll_seconds being 0.25s - the actual gate
    was wall-clock time PER CYCLE, and full multi-batch universe
    coverage exists to feed the general multi-symbol stock strategy,
    which is exactly what's suspended in this state. The one
    underlying that still needs fresh data (the locked focus symbol)
    is separately guaranteed into every single-batch scan via
    prioritized_stock_batch's force_include - so this doesn't lose
    that symbol's freshness, it only stops paying for full-universe
    coverage nothing is currently allowed to act on, freeing real
    cycle time for trade_options to run again sooner.
    """
    if total_symbols <= 0 or self.config.stock_batch_size <= 0:
        return 1
    if focus_mode_suppressing_entries:
        return 1
    cycles = max(1, self.config.stock_scan_target_full_coverage_cycles)
    needed = -(-total_symbols // (self.config.stock_batch_size * cycles))
    needed = max(1, needed)
    if not core_session_active:
        needed = max(
            1,
            int(
                needed
                * self.config.stock_scan_extended_hours_concurrency_fraction
            ),
        )
    return min(needed, self.config.stock_scan_max_concurrent_batches)


def prioritized_stock_batch(
    self,
    symbols: list[str],
    cursor: int,
    positions: list[dict],
    assessment_for,
    research_symbols: set[str] | None = None,
    force_include: set[str] | None = None,
    max_size: int | None = None,
) -> tuple[list[str], int]:
    if not symbols:
        return [], 0
    batch_size = self.config.stock_batch_size if max_size is None else max_size
    size = min(batch_size, len(symbols))
    available = set(symbols)
    research_symbols = (research_symbols or set()) & available
    held = [
        str(item.get("symbol", "")).upper()
        for item in positions
        if item.get("instrument_type") == "EQUITY"
        and Decimal(str(item.get("quantity", "0"))) != 0
        and str(item.get("symbol", "")).upper() in available
    ]
    # By explicit request ("i want this to be faster more high
    # frequency trades") - focus mode commits the whole account to
    # options on ONE underlying, but that underlying is usually never
    # itself an EQUITY position (the bot holds the option contract,
    # not the stock), so it was getting diluted into the same ~100-
    # symbol rotation as every other candidate - its price/EMA history
    # (and therefore the direction signal option entries gate on)
    # only refreshed once every several cycles instead of every
    # single one. Guaranteed inclusion here, same "always in the
    # batch" treatment as an open position, is a genuine speedup (a
    # real EMA cross gets DETECTED sooner) rather than a quality
    # relaxation (the signal itself is unchanged) - it costs one quote
    # slot per cycle, negligible against stock_batch_size.
    for symbol in force_include or set():
        if symbol in available and symbol not in held:
            held.append(symbol)
    ranked = sorted(
        (symbol for symbol in self.activity if symbol in available),
        key=lambda symbol: self.priority_score(
            symbol,
            assessment_for(symbol),
        ),
        reverse=True,
    )
    penny = [
        symbol
        for symbol in ranked
        if self.prices.get(symbol, Decimal("Infinity"))
        < self.config.penny_stock_max_price
    ]
    liquid_popular = [
        symbol
        for symbol in ranked
        if self.prices.get(symbol, Decimal("0"))
        >= self.config.penny_stock_max_price
        and self.metrics.get(symbol, {}).get("volume", 0)
        >= self.config.popular_stock_min_volume
        and Decimal(
            str(self.metrics.get(symbol, {}).get("spread_percent", "999"))
        )
        <= self.config.popular_stock_max_spread_percent
    ]
    researched = sorted(
        research_symbols,
        key=lambda symbol: self.priority_score(
            symbol,
            assessment_for(symbol),
        ),
        reverse=True,
    )
    popular = list(dict.fromkeys(researched + liquid_popular))
    penny_count = int(size * self.config.stock_penny_fraction)
    popular_count = int(size * self.config.stock_priority_fraction)
    popular_selected = popular[:popular_count]
    penny_selected = penny[:penny_count]
    # Reserve a guaranteed slice of every batch for fresh exploration so
    # the scanner keeps paging the whole universe instead of re-scanning
    # the same top-ranked names each cycle.
    explore_floor = max(1, size - popular_count - penny_count)
    priority = list(dict.fromkeys(held + popular_selected + penny_selected))
    # held (open positions + force_include) is NOT subject to the
    # batch-size truncation below. Live incident 2026-09-22: focus
    # mode caps the batch at focus_mode_stock_batch_size (20) while
    # stock entries are suspended, and its config comment states the
    # focus names are "guaranteed into every scan regardless of this
    # cap - see prioritized_stock_batch's force_include". They were
    # not: this slice cut them like anything else. The daily batch can
    # only score a symbol the scanner has quoted, so the candidate
    # pool went unquoted and selection saw 13 names out of 222 - no
    # threshold could fix that, because the data was never collected.
    # Truncating only the ranked/exploratory remainder keeps the cap
    # doing its real job (bounding per-cycle quote cost) without
    # silently starving the pipeline it exists to serve.
    guaranteed = [symbol for symbol in priority if symbol in set(held)]
    discretionary = [symbol for symbol in priority if symbol not in set(held)]
    room = max(0, size - explore_floor - len(guaranteed))
    priority = guaranteed + discretionary[:room]
    # Request exactly the open exploration slots, skipping symbols already
    # in the priority slice so exploration always keeps paging forward
    # through fresh names instead of re-picking (and then discarding)
    # names the priority slice already covered.
    exploration_slots = max(0, size - len(priority))
    priority_set = set(priority)
    exploration: list[str] = []
    attempts = 0
    while len(exploration) < exploration_slots and attempts < len(symbols):
        probe, cursor = self.rotating_batch(symbols, cursor, 1)
        attempts += 1
        if probe and probe[0] not in priority_set:
            exploration.append(probe[0])
    selected = list(dict.fromkeys(priority + exploration))
    # Same reasoning as the priority slice above: the final cut must
    # not drop a guaranteed name either, or the batch cap silently
    # un-guarantees what force_include promised.
    held_set_for_cut = set(held)
    required = [symbol for symbol in selected if symbol in held_set_for_cut]
    optional = [symbol for symbol in selected if symbol not in held_set_for_cut]
    selected = required + optional[: max(0, size - len(required))]
    self.selection_buckets = {}
    held_set = set(held)
    popular_set = set(popular_selected)
    penny_set = set(penny_selected)
    for symbol in selected:
        if symbol in held_set:
            self.selection_buckets[symbol] = "HELD"
        elif symbol in popular_set:
            self.selection_buckets[symbol] = "POPULAR"
        elif symbol in penny_set:
            self.selection_buckets[symbol] = "PENNY"
        else:
            self.selection_buckets[symbol] = "DISCOVERY"
    return selected, cursor


def selection_bucket(self, symbol: str) -> str:
    return self.selection_buckets.get(symbol, "DISCOVERY")


def research_candidates(
    self,
    limit: int,
    excluded: set[str],
    assessment_for,
    blocked_until,
) -> list[dict]:
    ranked = sorted(
        self.activity,
        key=lambda symbol: self.priority_score(
            symbol,
            assessment_for(symbol),
        ),
        reverse=True,
    )
    results = []
    for symbol in ranked:
        if symbol in excluded or blocked_until(symbol):
            continue
        price = self.prices.get(symbol)
        if not price:
            continue
        results.append(
            {
                "symbol": symbol,
                "type": (
                    "PENNY"
                    if price < self.config.penny_stock_max_price
                    else "POPULAR_VOLATILE"
                ),
                "price": str(price),
                **self.metrics.get(symbol, {}),
            }
        )
        if len(results) >= limit:
            break
    return results
