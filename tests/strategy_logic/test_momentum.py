import sys
import time
import unittest
import unittest.mock
from decimal import Decimal
from types import SimpleNamespace

from webull_bot.strategy import TradingStrategy
from webull_bot.webull_api import WebullAPI

from support.fixtures import StrategyConfigMixin


class HistoricalVolatilityTests(unittest.TestCase):
    def test_amplitudes_parse_across_shapes_and_skip_bad_bars(self):
        page = [
            {
                "symbol": "aaa",
                "bars": [
                    {"high": 11, "low": 9, "close": 10},
                    {"high": 10.5, "low": 9.5, "close": 10},
                ],
            },
            {"symbol": "BBB", "candles": [{"high": 5.5, "low": 4.5, "close": 5}]},
            {"symbol": "CCC"},
            {"symbol": "", "bars": [{"high": 2, "low": 1, "close": 1.5}]},
            "junk",
        ]
        amplitudes = WebullAPI._parse_amplitudes(page, days=20)
        self.assertAlmostEqual(amplitudes["AAA"], 15.0)
        self.assertAlmostEqual(amplitudes["BBB"], 20.0)
        self.assertNotIn("CCC", amplitudes)
        self.assertNotIn("", amplitudes)

    def test_average_amplitude_ignores_unusable_rows(self):
        bars = [
            {"high": "x", "low": 1, "close": 1},  # unparseable
            {"high": 9, "low": 10, "close": 10},  # high < low
            {"high": 12, "low": 8, "close": 10},  # 40%
        ]
        self.assertAlmostEqual(WebullAPI._average_amplitude(bars, days=20), 40.0)
        self.assertIsNone(WebullAPI._average_amplitude([], days=20))

    def test_average_close_ignores_unusable_rows(self):
        bars = [
            {"close": "x"},  # unparseable
            {"close": "0"},  # non-positive, excluded
            {"close": "10"},
            {"close": "20"},
        ]
        self.assertAlmostEqual(WebullAPI._average_close(bars, days=20), 15.0)
        self.assertIsNone(WebullAPI._average_close([], days=20))

    def test_sma_trend_parses_batched_daily_bars(self):
        api = WebullAPI.__new__(WebullAPI)
        fake_category = SimpleNamespace(US_STOCK=SimpleNamespace(name="US_STOCK"))
        fake_timespan = SimpleNamespace(D=SimpleNamespace(name="DAY"))

        def fake_call(callback, group):
            return callback()

        def fake_get_batch_history_bar(symbols, category, timespan, count):
            return [
                {
                    "symbol": "NVDA",
                    "bars": [{"close": "100"}, {"close": "80"}],
                }
            ]

        api._call = fake_call
        api.data = SimpleNamespace(
            market_data=SimpleNamespace(
                get_batch_history_bar=fake_get_batch_history_bar
            )
        )

        with unittest.mock.patch.dict(
            sys.modules,
            {
                "webull.data.common.category": SimpleNamespace(
                    Category=fake_category
                ),
                "webull.data.common.timespan": SimpleNamespace(
                    Timespan=fake_timespan
                ),
            },
        ):
            sma = api.sma_trend(["NVDA"], days=2)

        self.assertEqual(sma, {"NVDA": 90.0})

    def test_recent_minute_closes_parses_bars_oldest_first(self):
        api = WebullAPI.__new__(WebullAPI)
        fake_timespan = SimpleNamespace(M1=SimpleNamespace(name="M1"))

        def fake_call(callback, group):
            return callback()

        def fake_get_batch_history_bar(symbols, category, timespan, count):
            self.assertEqual(timespan, "M1")
            return [
                {
                    # Newest-first, same convention as the daily-bar
                    # parsing this mirrors - must come back reversed.
                    "symbol": "WILD",
                    "bars": [
                        {"close": "10.3"},
                        {"close": "10.1"},
                        {"close": "10.0"},
                    ],
                },
                {"symbol": "FLAT", "candles": [{"close": "x"}, {"close": "0"}]},
                {"symbol": ""},
            ]

        api._call = fake_call
        api.data = SimpleNamespace(
            market_data=SimpleNamespace(
                get_batch_history_bar=fake_get_batch_history_bar
            )
        )

        with unittest.mock.patch.dict(
            sys.modules,
            {
                "webull.data.common.timespan": SimpleNamespace(
                    Timespan=fake_timespan
                ),
            },
        ):
            closes = api.recent_minute_closes(["WILD", "FLAT"], "US_STOCK", count=30)

        self.assertEqual(closes, {"WILD": [10.0, 10.1, 10.3]})

    def test_refresh_sma_trend_merges_into_existing_cache(self):
        from webull_bot.bot import AutoTrader

        fake_bot = AutoTrader.__new__(AutoTrader)
        fake_bot.config = SimpleNamespace(
            sma_trend_filter_enabled=True, sma_trend_days=50
        )
        fake_bot.strategy = SimpleNamespace(sma_trend={"OLD": Decimal("5")})
        fake_bot.api = SimpleNamespace(
            sma_trend=lambda symbols, days: {"NVDA": 123.45}
        )
        refresh = AutoTrader.refresh_sma_trend.__get__(fake_bot)

        refresh(["NVDA"])

        self.assertEqual(fake_bot.strategy.sma_trend["NVDA"], Decimal("123.45"))
        self.assertEqual(fake_bot.strategy.sma_trend["OLD"], Decimal("5"))

    def test_refresh_sma_trend_noop_when_disabled(self):
        from webull_bot.bot import AutoTrader

        fake_bot = AutoTrader.__new__(AutoTrader)
        fake_bot.config = SimpleNamespace(sma_trend_filter_enabled=False)

        def boom(*args, **kwargs):
            raise AssertionError("must not call the API when disabled")

        fake_bot.api = SimpleNamespace(sma_trend=boom)
        refresh = AutoTrader.refresh_sma_trend.__get__(fake_bot)

        refresh(["NVDA"])

    def test_refresh_sma_trend_keeps_prior_values_on_failure(self):
        from webull_bot.bot import AutoTrader

        fake_bot = AutoTrader.__new__(AutoTrader)
        fake_bot.config = SimpleNamespace(
            sma_trend_filter_enabled=True, sma_trend_days=50
        )
        fake_bot.strategy = SimpleNamespace(sma_trend={"OLD": Decimal("5")})

        def boom(*args, **kwargs):
            raise RuntimeError("Webull API error 500: boom")

        fake_bot.api = SimpleNamespace(sma_trend=boom)
        refresh = AutoTrader.refresh_sma_trend.__get__(fake_bot)

        refresh(["NVDA"])

        self.assertEqual(fake_bot.strategy.sma_trend, {"OLD": Decimal("5")})

    def test_refresh_recent_momentum_merges_into_existing_cache(self):
        from webull_bot.bot import AutoTrader

        fake_bot = AutoTrader.__new__(AutoTrader)
        fake_bot.config = SimpleNamespace(
            recent_momentum_filter_enabled=True,
            recent_momentum_refresh_seconds=120,
            recent_momentum_lookback_minutes=10,
        )
        fake_bot.strategy = SimpleNamespace(recent_momentum={"OLD": Decimal("0.01")})
        fake_bot.last_recent_momentum_refresh = 0.0
        fake_bot.api = SimpleNamespace(
            recent_minute_closes=lambda symbols, category, count: {
                "NVDA": [100.0, 102.0, 105.0]
            }
        )
        refresh = AutoTrader.refresh_recent_momentum.__get__(fake_bot)

        with unittest.mock.patch("time.monotonic", return_value=1000.0):
            refresh(["NVDA"])

        self.assertEqual(
            fake_bot.strategy.recent_momentum["NVDA"], Decimal("0.05")
        )
        self.assertEqual(
            fake_bot.strategy.recent_momentum["OLD"], Decimal("0.01")
        )

    def test_refresh_recent_momentum_noop_when_disabled(self):
        from webull_bot.bot import AutoTrader

        fake_bot = AutoTrader.__new__(AutoTrader)
        fake_bot.config = SimpleNamespace(recent_momentum_filter_enabled=False)

        def boom(*args, **kwargs):
            raise AssertionError("must not call the API when disabled")

        fake_bot.api = SimpleNamespace(recent_minute_closes=boom)
        refresh = AutoTrader.refresh_recent_momentum.__get__(fake_bot)

        refresh(["NVDA"])

    def test_refresh_recent_momentum_respects_its_own_throttle(self):
        from webull_bot.bot import AutoTrader

        fake_bot = AutoTrader.__new__(AutoTrader)
        fake_bot.config = SimpleNamespace(
            recent_momentum_filter_enabled=True,
            recent_momentum_refresh_seconds=120,
            recent_momentum_lookback_minutes=10,
        )
        fake_bot.strategy = SimpleNamespace(recent_momentum={})
        fake_bot.last_recent_momentum_refresh = 999.0

        def boom(*args, **kwargs):
            raise AssertionError("must not call the API inside the throttle window")

        fake_bot.api = SimpleNamespace(recent_minute_closes=boom)
        refresh = AutoTrader.refresh_recent_momentum.__get__(fake_bot)

        with unittest.mock.patch("time.monotonic", return_value=1000.0):
            refresh(["NVDA"])

    def test_refresh_recent_momentum_keeps_prior_values_on_failure(self):
        from webull_bot.bot import AutoTrader

        fake_bot = AutoTrader.__new__(AutoTrader)
        fake_bot.config = SimpleNamespace(
            recent_momentum_filter_enabled=True,
            recent_momentum_refresh_seconds=120,
            recent_momentum_lookback_minutes=10,
        )
        fake_bot.strategy = SimpleNamespace(recent_momentum={"OLD": Decimal("0.01")})
        fake_bot.last_recent_momentum_refresh = 0.0

        def boom(*args, **kwargs):
            raise RuntimeError("Webull API error 500: boom")

        fake_bot.api = SimpleNamespace(recent_minute_closes=boom)
        refresh = AutoTrader.refresh_recent_momentum.__get__(fake_bot)

        with unittest.mock.patch("time.monotonic", return_value=1000.0):
            refresh(["NVDA"])

        self.assertEqual(
            fake_bot.strategy.recent_momentum, {"OLD": Decimal("0.01")}
        )


class MultiDayMomentumExtensionGuardTests(StrategyConfigMixin, unittest.TestCase):
    """multi_day_momentum_supports_entry's extension check - live
    incident: MGN was bought as a scalp dip entry at $0.2071 while
    still ~74% above its prior close of ~$0.1196 (a pullback mid-
    unwind of an intraday spike, not a real dip), fell straight
    through the hard stop within ~2 minutes; FAMI hit the same pattern
    minutes later. This guards the well-documented "don't chase/buy a
    stock still extended far above its prior close" pattern.
    """

    def config(self):
        cfg = super().config()
        cfg.multi_day_momentum_filter_enabled = True
        cfg.multi_day_momentum_max_decline_1d = Decimal("0.15")
        cfg.multi_day_momentum_max_decline_5d = Decimal("0.30")
        cfg.multi_day_momentum_max_decline_month = Decimal("0.50")
        cfg.multi_day_momentum_max_extension_1d = Decimal("0.50")
        return cfg

    def test_blocks_a_buy_still_extended_far_above_prior_close(self):
        strategy = TradingStrategy(self.config())
        strategy.daily_closes["MGN"] = [0.1196, 0.1186, 0.1098, 0.111, 0.109]
        self.assertFalse(
            strategy.multi_day_momentum_supports_entry(
                "MGN", "BUY", Decimal("0.2071")
            )
        )

    def test_allows_a_buy_close_to_prior_close(self):
        strategy = TradingStrategy(self.config())
        strategy.daily_closes["CALM"] = [1.00, 0.99, 0.98, 1.01, 1.00]
        self.assertTrue(
            strategy.multi_day_momentum_supports_entry(
                "CALM", "BUY", Decimal("1.05")
            )
        )

    def test_fails_open_without_a_price(self):
        strategy = TradingStrategy(self.config())
        strategy.daily_closes["MGN"] = [0.1196, 0.1186, 0.1098, 0.111, 0.109]
        self.assertTrue(
            strategy.multi_day_momentum_supports_entry("MGN", "BUY")
        )

    def test_fails_open_when_filter_disabled(self):
        cfg = self.config()
        cfg.multi_day_momentum_filter_enabled = False
        strategy = TradingStrategy(cfg)
        strategy.daily_closes["MGN"] = [0.1196, 0.1186, 0.1098, 0.111, 0.109]
        self.assertTrue(
            strategy.multi_day_momentum_supports_entry(
                "MGN", "BUY", Decimal("0.2071")
            )
        )

    def test_extension_check_does_not_apply_to_short_direction(self):
        strategy = TradingStrategy(self.config())
        strategy.daily_closes["MGN"] = [0.1196, 0.1186, 0.1098, 0.111, 0.109]
        self.assertTrue(
            strategy.multi_day_momentum_supports_entry(
                "MGN", "SHORT", Decimal("0.2071")
            )
        )


class RefreshMultiDayMomentumColdStartTests(unittest.TestCase):
    """AutoTrader.refresh_multi_day_momentum - live incident: VIOT was
    bought ~74% above its prior close 10 minutes after a restart. The
    old single GLOBAL throttle meant only whichever symbols happened
    to be in the very first post-restart scan batch ever got their
    daily_closes populated; a symbol scanned later sat with no data
    (and multi_day_momentum_supports_entry's extension guard fails
    open with no data) for up to the full 30-minute refresh window.
    Symbols with no cached entry yet must always be fetched
    immediately, bypassing the throttle - which only limits how often
    an ALREADY-cached symbol gets re-fetched.
    """

    def _fake_bot(self, daily_closes=None, last_refresh=0.0, fetch_calls=None):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                multi_day_momentum_filter_enabled=True,
                multi_day_momentum_lookback_days=25,
                multi_day_momentum_refresh_seconds=1800,
            ),
            strategy=SimpleNamespace(daily_closes=daily_closes or {}),
            last_multi_day_momentum_refresh=last_refresh,
            api=SimpleNamespace(
                daily_closes=lambda symbols, days: {
                    symbol: [1.0, 0.99, 0.98] for symbol in symbols
                }
            ),
        )
        if fetch_calls is not None:
            original = fake_bot.api.daily_closes

            def _tracking(symbols, days):
                fetch_calls.append(list(symbols))
                return original(symbols, days)

            fake_bot.api.daily_closes = _tracking
        return AutoTrader.refresh_multi_day_momentum.__get__(fake_bot), fake_bot

    def test_fetches_an_uncached_symbol_even_within_the_throttle_window(self):
        refresh, fake_bot = self._fake_bot(
            daily_closes={}, last_refresh=time.monotonic()
        )
        refresh(["VIOT"])
        self.assertIn("VIOT", fake_bot.strategy.daily_closes)

    def test_does_not_re_fetch_an_already_cached_symbol_within_the_throttle(self):
        calls = []
        refresh, fake_bot = self._fake_bot(
            daily_closes={"AAPL": [1.0, 0.99]},
            last_refresh=time.monotonic(),
            fetch_calls=calls,
        )
        refresh(["AAPL"])
        self.assertEqual(calls, [])

    def test_fetches_only_the_uncached_symbols_when_batch_is_mixed(self):
        calls = []
        refresh, fake_bot = self._fake_bot(
            daily_closes={"AAPL": [1.0, 0.99]},
            last_refresh=time.monotonic(),
            fetch_calls=calls,
        )
        refresh(["AAPL", "VIOT"])
        self.assertEqual(calls, [["VIOT"]])
        self.assertIn("VIOT", fake_bot.strategy.daily_closes)

    def test_refetches_everything_once_the_throttle_window_elapses(self):
        # time.monotonic() is relative to an arbitrary reference point
        # (often process/system start) - on a freshly-booted CI
        # container it can itself be a small number, so a hardcoded
        # last_refresh=0.0 doesn't reliably simulate "the throttle
        # window has elapsed" the way it does on a long-uptime dev
        # machine (live incident: this exact test failed in CI for
        # that reason). A large negative value keeps now - last_refresh
        # far past the throttle window regardless of what time.
        # monotonic() itself happens to read on any given machine.
        calls = []
        refresh, fake_bot = self._fake_bot(
            daily_closes={"AAPL": [1.0, 0.99]},
            last_refresh=-1_000_000.0,
            fetch_calls=calls,
        )
        refresh(["AAPL"])
        self.assertEqual(calls, [["AAPL"]])
