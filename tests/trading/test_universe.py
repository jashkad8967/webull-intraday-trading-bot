import unittest
import unittest.mock
from datetime import datetime
from types import SimpleNamespace



class CapBatchToSnapshotLimitTests(unittest.TestCase):
    """Live incident: force-injecting the curated cohort AND every
    volatility-scalp-eligible symbol on top of an already-full
    stock_batch_size batch pushed the combined size past Webull's own
    hard 100-symbol snapshot limit - the ENTIRE quote fetch for that
    cycle raised and failed (logged live: "STOCKS | quote batch failed
    | Webull stock snapshots accept at most 100 symbols"), losing price
    data for every symbol in the batch, not just the extra ones.
    cap_batch_to_snapshot_limit caps the final batch, always keeping
    held positions first.
    """

    def test_under_the_limit_is_unchanged(self):
        from webull_bot.bot import AutoTrader

        batch = [f"S{i}" for i in range(50)]
        result = AutoTrader.cap_batch_to_snapshot_limit(batch, [])
        self.assertEqual(result, batch)

    def test_over_the_limit_is_trimmed_to_exactly_the_cap(self):
        from webull_bot.bot import AutoTrader

        batch = [f"S{i}" for i in range(150)]
        result = AutoTrader.cap_batch_to_snapshot_limit(batch, [])
        self.assertEqual(len(result), 100)

    def test_held_positions_are_never_trimmed_even_over_the_limit(self):
        from webull_bot.bot import AutoTrader

        held = [f"HELD{i}" for i in range(10)]
        batch = held + [f"S{i}" for i in range(150)]
        result = AutoTrader.cap_batch_to_snapshot_limit(batch, held)
        self.assertEqual(len(result), 100)
        for symbol in held:
            self.assertIn(symbol, result)

    def test_a_large_held_count_still_keeps_every_held_position(self):
        """Even if held positions alone exceed the cap (not expected in
        practice given max_open_positions, but not this function's job
        to enforce), protecting real positions wins over trimming to
        exactly the cap.
        """
        from webull_bot.bot import AutoTrader

        held = [f"HELD{i}" for i in range(120)]
        result = AutoTrader.cap_batch_to_snapshot_limit(held, held)
        self.assertEqual(len(result), 120)

    def test_a_custom_limit_allows_more_than_the_single_call_cap(self):
        """By request: "scan through all [the universe]... split it up
        in parallel streams" - trade_stocks now passes a multiple of
        the single-call cap when firing several concurrent quote
        batches this cycle (see stock_scan_concurrent_batches).
        """
        from webull_bot.bot import AutoTrader

        batch = [f"S{i}" for i in range(250)]
        result = AutoTrader.cap_batch_to_snapshot_limit(batch, [], limit=300)
        self.assertEqual(result, batch)

    def test_a_custom_limit_still_trims_past_it(self):
        from webull_bot.bot import AutoTrader

        batch = [f"S{i}" for i in range(350)]
        result = AutoTrader.cap_batch_to_snapshot_limit(batch, [], limit=300)
        self.assertEqual(len(result), 300)


class ResolveTargetsNonBlockingTests(unittest.TestCase):
    """resolve_targets - by request, after confirming live that real
    open positions sat completely unmonitored for 15-20 minutes on
    every restart: the once-daily universe/VOLFILT/SMA refresh must
    never block the main loop's position-protection calls. Dispatches
    to a background thread and returns immediately instead.
    """

    @staticmethod
    def _fake_bot(started_dates):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            resolved_date=None,
            _resolve_targets_in_progress_for=None,
        )

        def fake_work(moment):
            started_dates.append(moment.date())

        fake_bot._resolve_targets_work_body = fake_work
        fake_bot._resolve_targets_work = AutoTrader._resolve_targets_work.__get__(
            fake_bot
        )
        fake_bot.resolve_targets = AutoTrader.resolve_targets.__get__(fake_bot)
        return fake_bot

    def test_returns_immediately_without_waiting_for_the_thread(self):
        import threading
        import time as time_module

        started_dates = []
        release = threading.Event()

        def slow_work(moment):
            release.wait(timeout=2)
            started_dates.append(moment.date())

        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            resolved_date=None, _resolve_targets_in_progress_for=None
        )
        fake_bot._resolve_targets_work_body = slow_work
        fake_bot._resolve_targets_work = AutoTrader._resolve_targets_work.__get__(
            fake_bot
        )
        resolve_targets = AutoTrader.resolve_targets.__get__(fake_bot)

        call_started = time_module.monotonic()
        resolve_targets(datetime(2026, 8, 27))
        elapsed = time_module.monotonic() - call_started

        self.assertLess(elapsed, 0.5)
        self.assertEqual(started_dates, [])
        release.set()

    def test_does_not_dispatch_a_second_thread_while_one_is_in_flight(self):
        started_dates = []
        fake_bot = self._fake_bot(started_dates)
        import threading

        gate = threading.Event()
        original = fake_bot._resolve_targets_work_body

        def gated_work(moment):
            gate.wait(timeout=2)
            original(moment)

        fake_bot._resolve_targets_work_body = gated_work
        moment = datetime(2026, 8, 27)

        fake_bot.resolve_targets(moment)
        fake_bot.resolve_targets(moment)
        fake_bot.resolve_targets(moment)

        self.assertEqual(fake_bot._resolve_targets_in_progress_for, moment.date())
        gate.set()

    def test_skips_entirely_once_already_resolved_for_the_date(self):
        started_dates = []
        fake_bot = self._fake_bot(started_dates)
        moment = datetime(2026, 8, 27)
        fake_bot.resolved_date = moment.date()

        fake_bot.resolve_targets(moment)

        self.assertEqual(started_dates, [])

    def test_clears_in_progress_flag_on_failure_so_the_next_cycle_retries(self):
        from webull_bot.bot import AutoTrader

        def boom(moment):
            raise RuntimeError("boom")

        fake_bot = SimpleNamespace(
            resolved_date=None, _resolve_targets_in_progress_for=None
        )
        fake_bot._resolve_targets_work_body = boom
        work = AutoTrader._resolve_targets_work.__get__(fake_bot)

        work(datetime(2026, 8, 27))

        self.assertIsNone(fake_bot._resolve_targets_in_progress_for)
        self.assertIsNone(fake_bot.resolved_date)


class ProgressiveUniverseLoadingTests(unittest.TestCase):
    """_download_and_filter_universe / _grow_stock_universe - by
    request, after live evidence: at a large MAX_SYMBOLS, downloading
    and VOLFILT-scoring the WHOLE universe before AutoTrader.stock_
    symbols was populated at all took 15-20 minutes, during which no
    NEW entry could fire (position protection was already fixed
    separately - see ResolveTargetsNonBlockingTests). Starts with a
    small, fast initial universe and grows it in the background.
    """

    @staticmethod
    def _fake_bot(universes_by_limit, config_overrides=None):
        """universes_by_limit maps a requested `limit` to the list of
        symbols api.stock_universe should return for that call (as if
        each limit produces its own, larger, superset page) - the fake
        stock_universe just returns categories for min(limit, len(all)).
        """
        from webull_bot.bot import AutoTrader

        all_symbols = universes_by_limit["all"]

        class FakeApi:
            @staticmethod
            def stock_universe(progress, limit):
                chosen = all_symbols[:limit]
                if progress:
                    progress("US_LISTED", len(chosen), limit)
                return {s: "US_STOCK" for s in chosen}

            @staticmethod
            def stock_categories(symbols):
                return {}

        config = SimpleNamespace(
            popular_stocks=lambda: [],
            top_gainers_limit=0,
            exclude_etfs=False,
            stock_universe_page_size=200,
            stock_universe_initial_limit=config_overrides.get("initial", 2)
            if config_overrides
            else 2,
            stock_universe_growth_batch_size=config_overrides.get("batch", 2)
            if config_overrides
            else 2,
            stock_universe_growth_interval_seconds=1,
        )
        config.stock_universe_limit = lambda: (
            config_overrides.get("full", len(all_symbols))
            if config_overrides
            else len(all_symbols)
        )
        config.stock_universe_pool = lambda: config.stock_universe_limit() + 10

        class FakeInvalidSymbols:
            symbols = set()

            def __contains__(self, symbol):
                return False

        fake_bot = SimpleNamespace(
            api=FakeApi(),
            config=config,
            invalid_symbols=FakeInvalidSymbols(),
            safe_top_gainers=lambda limit, page_size: {},
            filter_with_popular_reinstated=lambda candidates: candidates,
            stock_symbols=[],
            stock_categories={},
            reserve_symbols=[],
            resolved_date=None,
        )
        fake_bot._download_and_filter_universe = (
            AutoTrader._download_and_filter_universe.__get__(fake_bot)
        )
        fake_bot._grow_stock_universe = AutoTrader._grow_stock_universe.__get__(
            fake_bot
        )
        return fake_bot

    def test_download_and_filter_splits_into_symbols_and_reserve(self):
        fake_bot = self._fake_bot({"all": ["A", "B", "C", "D", "E"]})
        categories, symbols, reserve = fake_bot._download_and_filter_universe(3, 10)
        self.assertEqual(symbols, ["A", "B", "C"])
        self.assertEqual(reserve, ["D", "E"])
        self.assertEqual(len(categories), 5)

    def test_growth_noop_when_initial_pass_already_covers_the_full_limit(self):
        fake_bot = self._fake_bot(
            {"all": ["A", "B"]}, {"initial": 5, "full": 2, "batch": 2}
        )
        fake_bot.stock_symbols = ["A", "B"]
        moment = datetime(2026, 8, 27)
        fake_bot.resolved_date = moment.date()
        with unittest.mock.patch("time.sleep") as sleep_mock:
            fake_bot._grow_stock_universe(moment)
        sleep_mock.assert_not_called()

    def test_growth_merges_new_symbols_without_touching_existing_ones(self):
        all_symbols = [f"S{i}" for i in range(10)]
        fake_bot = self._fake_bot(
            {"all": all_symbols}, {"initial": 3, "full": 10, "batch": 3}
        )
        fake_bot.stock_symbols = all_symbols[:3]
        fake_bot.stock_categories = {s: "US_STOCK" for s in all_symbols[:3]}
        moment = datetime(2026, 8, 27)
        fake_bot.resolved_date = moment.date()
        with unittest.mock.patch("time.sleep"):
            fake_bot._grow_stock_universe(moment)
        # Original 3 symbols stay first/untouched, new ones appended -
        # never a wholesale replace of an already-scanning universe.
        self.assertEqual(fake_bot.stock_symbols[:3], all_symbols[:3])
        self.assertEqual(set(fake_bot.stock_symbols), set(all_symbols))

    def test_growth_stops_once_the_real_universe_is_smaller_than_the_cap(self):
        fake_bot = self._fake_bot(
            {"all": ["A", "B", "C"]}, {"initial": 1, "full": 100, "batch": 5}
        )
        fake_bot.stock_symbols = ["A"]
        fake_bot.stock_categories = {"A": "US_STOCK"}
        moment = datetime(2026, 8, 27)
        fake_bot.resolved_date = moment.date()
        with unittest.mock.patch("time.sleep"):
            fake_bot._grow_stock_universe(moment)
        self.assertEqual(set(fake_bot.stock_symbols), {"A", "B", "C"})

    def test_growth_abandons_if_a_new_trading_day_started_mid_flight(self):
        """A stale-day growth step must never merge into a fresh day's
        universe - resolved_date changing mid-loop means a brand new
        resolve_targets already took over.
        """
        all_symbols = [f"S{i}" for i in range(10)]
        fake_bot = self._fake_bot(
            {"all": all_symbols}, {"initial": 3, "full": 10, "batch": 3}
        )
        fake_bot.stock_symbols = all_symbols[:3]
        fake_bot.stock_categories = {s: "US_STOCK" for s in all_symbols[:3]}
        moment = datetime(2026, 8, 27)
        fake_bot.resolved_date = moment.date()

        call_count = {"n": 0}

        def sleep_and_flip(*a, **k):
            call_count["n"] += 1
            if call_count["n"] == 1:
                fake_bot.resolved_date = datetime(2026, 8, 28).date()

        with unittest.mock.patch("time.sleep", side_effect=sleep_and_flip):
            fake_bot._grow_stock_universe(moment)

        # Only the original 3 symbols remain - the in-flight step never
        # merged its (now-stale) results in.
        self.assertEqual(set(fake_bot.stock_symbols), set(all_symbols[:3]))
