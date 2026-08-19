import queue
import threading
import time
from decimal import Decimal


class AnalystDataService:
    """Background fetcher for analyst target price/rating, feeding
    TradingStrategy.priority_score's soft entry-priority nudge (see
    analyst_priority_bonus in strategy.py).

    Runs on its own daemon thread so a slow or rate-limited fundamentals
    lookup can never stall the fast trading poll loop - the main thread's
    only touchpoints are request() (a non-blocking, self-throttling queue
    put) and snapshot() (a lock-protected dict copy), both pure in-memory
    operations with no network I/O.
    """

    def __init__(self, api, config, log):
        self.api = api
        self.config = config
        self.log = log
        self._queue: queue.Queue = queue.Queue(maxsize=50)
        self._lock = threading.Lock()
        self._bonus: dict[str, float] = {}
        self._fetched_at: dict[str, float] = {}
        self._queued: set[str] = set()
        threading.Thread(target=self._worker, daemon=True).start()

    def request(self, symbol: str, price: Decimal) -> None:
        """No-op if a fetch for this symbol is already queued, still
        within ANALYST_DATA_CACHE_SECONDS of its last completed attempt,
        or the queue is momentarily full - in every case, safe to just
        skip, since the next cycle (or a later periodic sweep) tries
        again.
        """
        if not self.config.analyst_priority_enabled:
            return
        now = time.monotonic()
        with self._lock:
            if symbol in self._queued:
                return
            last = self._fetched_at.get(symbol, 0.0)
            if now - last < self.config.analyst_data_cache_seconds:
                return
            self._queued.add(symbol)
        try:
            self._queue.put_nowait((symbol, price))
        except queue.Full:
            with self._lock:
                self._queued.discard(symbol)

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return dict(self._bonus)

    def _worker(self) -> None:
        while True:
            symbol, price = self._queue.get()
            try:
                self._fetch(symbol, price)
            except Exception as exc:
                # No analyst coverage is the common case for the
                # penny/micro-cap names this bot trades a lot of - not
                # worth a warning-level line every time.
                self.log.debug("ANALYST| %-8s | fetch failed | %s", symbol, exc)
            finally:
                with self._lock:
                    self._queued.discard(symbol)
                    self._fetched_at[symbol] = time.monotonic()

    def _fetch(self, symbol: str, price: Decimal) -> None:
        from webull_bot.strategy import TradingStrategy

        target = self.api.analyst_target_price(symbol)
        rating = self.api.analyst_rating(symbol)
        bonus = TradingStrategy.analyst_priority_bonus(
            price, target, rating, self.config.analyst_priority_bonus_max
        )
        with self._lock:
            self._bonus[symbol] = float(bonus)
