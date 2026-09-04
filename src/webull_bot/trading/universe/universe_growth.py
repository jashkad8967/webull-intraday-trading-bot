import logging
import time
from datetime import datetime

log = logging.getLogger("webull-bot")


def _grow_stock_universe(self, moment: datetime) -> None:
    """Continues growing today's universe toward the full
    MAX_SYMBOLS in the background, after _resolve_targets_work_body's
    fast initial pass has already unblocked trading. By request,
    after live evidence: at a large MAX_SYMBOLS, downloading and
    VOLFILT-scoring the WHOLE universe before AutoTrader.stock_
    symbols was populated at all took 15-20 minutes - position
    protection never blocked on this (see resolve_targets), but no
    NEW entry could fire the entire time either, since trade_stocks
    had nothing to scan. Only ever called once per day, right after
    the initial pass, from the same background thread - never blocks
    the main loop either, same as resolve_targets itself.

    Re-downloads at a progressively larger limit each step (simpler
    and safer than trying to resume Webull's own pagination cursor
    across separate calls) and MERGES newly-discovered symbols into
    the already-active self.stock_symbols/self.stock_categories -
    never replaces or resets what's already scanning, only adds to
    it. Stops once the configured MAX_SYMBOLS is reached, or the
    universe genuinely has no more symbols to add.
    """
    full_limit = self.config.stock_universe_limit()
    initial_limit = min(full_limit, self.config.stock_universe_initial_limit)
    if full_limit <= initial_limit:
        return
    pool = self.config.stock_universe_pool()
    current_limit = initial_limit
    batch = self.config.stock_universe_growth_batch_size
    interval = self.config.stock_universe_growth_interval_seconds
    while current_limit < full_limit and self.resolved_date == moment.date():
        time.sleep(interval)
        current_limit = min(full_limit, current_limit + batch)
        try:
            categories, symbols, reserve = self._download_and_filter_universe(
                current_limit, pool
            )
        except Exception as exc:
            log.error("LOAD   | universe growth step failed | %s", exc)
            continue
        if self.resolved_date != moment.date():
            # A new trading day started (or a fresh resolve_targets
            # kicked off) while this growth step was in flight -
            # abandon it rather than merge stale-day data into a
            # new day's universe.
            return
        existing = set(self.stock_symbols)
        new_symbols = [s for s in symbols if s not in existing]
        if new_symbols:
            self.stock_categories.update(categories)
            self.stock_symbols = self.stock_symbols + new_symbols
            self.reserve_symbols = reserve
            log.info(
                "LOAD   | universe grown | +%s symbols | total=%s/%s",
                len(new_symbols),
                len(self.stock_symbols),
                full_limit,
            )
        if current_limit >= full_limit or len(symbols) < current_limit:
            # Reached the configured cap, or the real universe is
            # simply smaller than the cap - nothing more to grow.
            return
