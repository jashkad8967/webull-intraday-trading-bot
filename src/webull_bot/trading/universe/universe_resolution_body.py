import logging
from datetime import datetime
from decimal import Decimal

log = logging.getLogger("webull-bot")


def _resolve_targets_work_body(self, moment: datetime) -> None:
    requested_stocks = self.config.stocks()
    if requested_stocks == ["ALL"]:
        full_limit = self.config.stock_universe_limit()
        pool = self.config.stock_universe_pool()
        # By request: start with a small, fast initial universe so
        # trading can begin almost immediately, then grow toward
        # the full MAX_SYMBOLS in the background (see
        # _grow_stock_universe, kicked off at the end of this
        # function) instead of blocking every new entry on
        # downloading and VOLFILT-scoring the whole universe first.
        limit = min(full_limit, self.config.stock_universe_initial_limit)
        initial_pool = min(pool, max(limit, self.config.stock_universe_page_size))
        self.stock_categories, self.stock_symbols, self.reserve_symbols = (
            self._download_and_filter_universe(limit, initial_pool)
        )
    else:
        log.info("LOAD   | resolving %s configured symbols", len(requested_stocks))
        requested_stocks = [
            symbol
            for symbol in requested_stocks
            if symbol not in self.invalid_symbols
        ]
        self.stock_symbols = (
            requested_stocks
            if self.config.max_symbols == 0
            else requested_stocks[: self.config.max_symbols]
        )
        self.reserve_symbols = []
        self.stock_categories = self.api.stock_categories(self.stock_symbols)
        for symbol in self.stock_symbols:
            self.stock_categories.setdefault(symbol, "US_STOCK")
        if self.config.exclude_etfs:
            self.stock_symbols = [
                symbol
                for symbol in self.stock_symbols
                if self.stock_categories.get(symbol) != "US_ETF"
            ]
    self.stock_symbols, pairs_excluded = self.exclude_pairs_symbols(
        self.stock_symbols
    )
    self.reserve_symbols, _ = self.exclude_pairs_symbols(self.reserve_symbols)
    if pairs_excluded:
        log.info(
            "LOAD   | excluded %s pairs-strategy symbols from the main "
            "universe scan (managed separately) | %s",
            len(pairs_excluded),
            ",".join(pairs_excluded),
        )
    missing_watchlist = [
        symbol for symbol in self.user_watchlist if symbol not in self.stock_symbols
    ]
    if missing_watchlist:
        uncategorized = [
            symbol
            for symbol in missing_watchlist
            if symbol not in self.stock_categories
        ]
        if uncategorized:
            # A single batched lookup (stock_categories chunks internally)
            # instead of one throttled call per symbol - this list can be
            # 100+ symbols long (the default watchlist alone), and that
            # throttle is ~3.3s/call, so doing it one at a time would
            # stall startup for minutes.
            try:
                categories = self.api.stock_categories(uncategorized)
            except Exception as exc:
                log.error(
                    "LOAD   | watchlist category lookup failed | %s",
                    exc,
                )
                categories = {}
            for symbol in uncategorized:
                self.stock_categories[symbol] = categories.get(symbol, "US_STOCK")
        self.stock_symbols.extend(missing_watchlist)
        log.info(
            "LOAD   | reinstated %s user-watchlist symbols | %s",
            len(missing_watchlist),
            ",".join(missing_watchlist),
        )
    self.refresh_sma_trend(self.stock_symbols)
    self.option_contracts = self.api.resolve_options()
    self.discover_all_options = "ALL" in self.config.option_roots()
    self.strategy.clear_market_state()
    self.volatility_scalp_recently_eligible.clear()
    if (
        self.config.volatility_scalp_enabled
        and self.config.volatility_scalp_bar_seed_enabled
    ):
        # By request: pick the volatility-scalp cohort very early in
        # the day, not whenever organic scanning happens to reach
        # enough symbols - left to the normal per-batch seeding
        # alone, the first cohort selection would only ever see
        # whichever ~100 of 300+ symbols happened to be scanned
        # first (a rotating batch, not the whole universe), biasing
        # it toward scan order instead of genuine volatility. Bar-
        # seeding the WHOLE day's candidate pool once, right here,
        # means the very first select_volatility_scalp_symbols()
        # call (right after this function returns) already has full
        # visibility across every symbol, not a scan-order-biased
        # slice of it.
        log.info(
            "SCALP  | seeding volatility windows for %s symbols ahead "
            "of the day's first cohort selection",
            len(self.stock_symbols),
        )
        self.seed_volatility_windows(self.stock_symbols)
    self.volatility_windows_seeded_date = moment.date()
    self.stock_cursor = 0
    self.option_cursor = 0
    self.option_discovery_cursor = 0
    self.option_discovery_attempted.clear()
    self.invalid_stock_symbols.clear()
    self.resolved_date = moment.date()
    available = set(self.stock_symbols)
    self.seed_popular_symbols = set(self.config.popular_stocks()) & available
    self.agent_popular_symbols.clear()
    self.daily_realized_loss = Decimal("0")
    self.daily_realized_pnl = Decimal("0")
    self.daily_pnl.reset()
    self.daily_loss_breaker_triggered = False
    self.submitted_order_ids_today.clear()
    self.reconciliation_flagged_order_ids.clear()
    if self.broker_conflict_symbols:
        log.info(
            "CONFLICT | daily reset | resuming automated action on | %s",
            ",".join(sorted(self.broker_conflict_symbols)),
        )
    self.broker_conflict_symbols.clear()
    log.info(
        "READY  | stocks=%s | popular seeds=%s | options=%s | option scan=%s",
        len(self.stock_symbols),
        len(self.seed_popular_symbols),
        len(self.option_contracts),
        "ON" if self.discover_all_options else "OFF",
    )
    # Trading is already unblocked at this point (stock_symbols is
    # populated, resolved_date is set) - continue growing toward
    # the full universe on this same background thread, still
    # never blocking the main loop. No-ops immediately if
    # STOCK_SYMBOLS isn't "ALL" or the initial pass already covered
    # the full configured size.
    if requested_stocks == ["ALL"]:
        self._grow_stock_universe(moment)
