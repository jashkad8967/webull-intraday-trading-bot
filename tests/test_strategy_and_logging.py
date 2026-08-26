import json
import logging
import queue
import shutil
import statistics
import sys
import threading
import time
import unittest
import unittest.mock
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from datetime import time as datetime_time
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from types import SimpleNamespace

from webull_bot.commands import CommandQueue
from webull_bot.config import Settings
from webull_bot.daily_logging import DatedDailyFileHandler
from webull_bot.daily_pnl import DailyPnlTracker
from webull_bot.market_agent import MarketResearchAgent
from webull_bot.pairs import (
    PAIRS_ENTRY_Z,
    PAIRS_EXIT_Z,
    PAIRS_MAX_HOLD_MINUTES,
    PAIRS_MIN_SAMPLES,
    PAIRS_STOP_Z,
    PairsStrategy,
)
from webull_bot.status import StatusWriter
from webull_bot.strategy import TradingStrategy
from webull_bot.strategy_tuning import (
    LEVER_SPECS,
    SAFETY_DENYLIST,
    StrategyTuningState,
    apply_lever_adjustment,
)
from webull_bot.wash_sale import WashSaleTracker
from webull_bot.webull_api import QuoteUnavailableError, WebullAPI


class StrategyConfigMixin:
    def config(self):
        return SimpleNamespace(
            ema_fast_period=3,
            ema_slow_period=8,
            stock_batch_size=5,
            stock_priority_fraction=0.6,
            stock_penny_fraction=0.2,
            stock_oscillation_weight=Decimal("0.5"),
            most_active_priority_bonus=Decimal("15"),
            penny_stock_max_price=Decimal("5"),
            popular_stock_min_volume=1_000_000,
            popular_stock_max_spread_percent=Decimal("0.50"),
            reenter_on_trend=True,
            reenter_confirmation_polls=2,
            tick_direction_enabled=True,
            tick_direction_window=10,
            tick_direction_veto_threshold=Decimal("0"),
            vwap_entry_band_percent=Decimal("0.001"),
            stock_min_net_profit_percent=Decimal("0.0001"),
            stock_estimated_round_trip_cost_percent=Decimal("0.002"),
            sell_fee_dollars=Decimal("0.02"),
            stock_stop_loss_min_percent=Decimal("0.0015"),
            stock_stop_loss_max_percent=Decimal("0.02"),
            stock_stop_loss_range_multiplier=Decimal("0.35"),
            stock_target_stop_multiple=Decimal("1.2"),
            stock_price_sanity_percent=Decimal("0.15"),
            stock_entry_max_spread_percent=Decimal("0.15"),
            stock_entry_max_extension_percent=Decimal("0.01"),
            stock_core_session_position_fraction=Decimal("0.10"),
            sma_trend_filter_enabled=False,
            short_selling_enabled=False,
            opening_grace_spread_multiplier=Decimal("2"),
            opening_grace_extension_multiplier=Decimal("2"),
            option_take_profit_percent=Decimal("0.75"),
            option_stop_loss_percent=Decimal("0.50"),
            option_min_hold_dte=2,
            option_capital_fraction=Decimal("0.05"),
            option_quantity=1,
            max_order_notional=Decimal("1000"),
            agent_exit_influence_enabled=True,
            agent_exit_min_confidence=Decimal("0.60"),
            agent_runner_bias_threshold=Decimal("0.50"),
            agent_runner_profit_percent=Decimal("0.01"),
            agent_derisk_bias_threshold=Decimal("-0.50"),
            time_aware_stop_enabled=False,
            time_aware_stop_widen_seconds=60,
            time_aware_stop_widen_multiplier=Decimal("1.5"),
            volatility_scalp_enabled=True,
            volatility_scalp_lookback_samples=20,
            volatility_scalp_min_stdev_percent=Decimal("0.015"),
            # Default 0 (no-op) so existing tests that feed a small,
            # arbitrary volume via _feed's "volume": "1000" aren't
            # affected - dedicated tests below override this to
            # exercise the floor itself.
            volatility_scalp_min_volume=0,
            volatility_scalp_dip_entry_percent=Decimal("0.005"),
            volatility_scalp_averaging_step_multiplier=Decimal("0.5"),
            volatility_scalp_vwap_band_percent=Decimal("0.05"),
            volatility_scalp_target_percent=Decimal("0.005"),
            volatility_scalp_momentum_stall_min_profit_fraction=Decimal("0.6"),
            volatility_scalp_hard_stop_percent=Decimal("0.05"),
            volatility_scalp_max_price=Decimal("5"),
            volatility_scalp_target_notional=Decimal("400"),
            volatility_scalp_target_notional_buying_power_fraction=Decimal("0.15"),
            volatility_scalp_breakout_k=Decimal("0.5"),
            heikin_ashi_bar_samples=3,
            heikin_ashi_bar_count=6,
            parabolic_sar_af_step=Decimal("0.02"),
            parabolic_sar_af_max=Decimal("0.2"),
        )


class StrategySelectionTests(StrategyConfigMixin, unittest.TestCase):
    def test_research_popular_names_are_first_without_blocking_discovery(self):
        strategy = TradingStrategy(self.config())
        symbols = ["NVDA", "CHEAP", "OTHER", "NEXT", "LAST"]
        strategy.prices.update(
            {
                "NVDA": Decimal("100"),
                "CHEAP": Decimal("2"),
                "OTHER": Decimal("20"),
            }
        )
        strategy.activity.update({"CHEAP": 20, "OTHER": 10})
        strategy.metrics.update(
            {
                "CHEAP": {"volume": 2_000_000, "spread_percent": 0.2},
                "OTHER": {"volume": 2_000_000, "spread_percent": 0.2},
            }
        )

        batch, _ = strategy.prioritized_stock_batch(
            symbols,
            0,
            [],
            lambda symbol: None,
            {"NVDA"},
        )

        self.assertEqual(batch[0], "NVDA")
        self.assertEqual(strategy.selection_bucket("NVDA"), "POPULAR")
        self.assertIn("CHEAP", batch)
        self.assertIn("NEXT", batch)

    def test_priority_score_boosts_a_symbol_on_the_most_active_screener(self):
        strategy = TradingStrategy(self.config())
        strategy.activity["TSLA"] = 5.0
        without_boost = strategy.priority_score("TSLA", None)
        strategy.most_active_symbols = {"TSLA"}
        with_boost = strategy.priority_score("TSLA", None)
        self.assertEqual(
            with_boost - without_boost, float(self.config().most_active_priority_bonus)
        )

    def test_priority_score_adds_the_analyst_priority_bonus(self):
        strategy = TradingStrategy(self.config())
        strategy.activity["TSLA"] = 5.0
        without_bonus = strategy.priority_score("TSLA", None)
        strategy.analyst_priority["TSLA"] = 3.5
        with_bonus = strategy.priority_score("TSLA", None)
        self.assertAlmostEqual(with_bonus - without_bonus, 3.5)

    def test_most_active_symbol_is_prioritized_over_an_otherwise_equal_one(self):
        """Regression coverage for "focus more on most-active for
        volatility": two symbols with identical activity/research inputs
        must still rank most-active first once
        TradingStrategy.most_active_symbols marks one of them.
        """
        strategy = TradingStrategy(self.config())
        symbols = ["ACTIVE", "TWIN", "FILLER1", "FILLER2"]
        strategy.prices.update(
            {
                "ACTIVE": Decimal("20"),
                "TWIN": Decimal("20"),
            }
        )
        strategy.activity.update({"ACTIVE": 5.0, "TWIN": 5.0})
        strategy.most_active_symbols = {"ACTIVE"}

        batch, _ = strategy.prioritized_stock_batch(
            symbols,
            0,
            [],
            lambda symbol: None,
            {"ACTIVE", "TWIN"},
        )

        self.assertEqual(batch[0], "ACTIVE")

    def test_strong_research_can_assist_but_missing_research_does_not_veto_ema(self):
        strategy = TradingStrategy(self.config())
        research = {
            "confidence": 0.9,
            "quick_trade_score": 0.8,
            "symbol_volatility": 0.8,
            "expected_move_percent": 1.2,
            "catalyst_strength": 0.7,
            "liquidity_risk": 0.2,
            "downside_risk": 0.3,
            "horizon_minutes": 15,
        }
        assisted = strategy.stock_decision(
            "STOCK:NVDA",
            Decimal("100"),
            0,
            Decimal("0"),
            research,
        )
        self.assertEqual(assisted.action, "BUY")

        prices = [
            Decimal("10"),
            Decimal("9.9"),
            Decimal("9.8"),
            Decimal("9.7"),
            Decimal("9.6"),
            Decimal("9.7"),
            Decimal("9.9"),
            Decimal("10.2"),
            Decimal("10.5"),
            Decimal("10.8"),
            Decimal("11.1"),
        ]
        decisions = [
            strategy.stock_decision(
                "STOCK:EMA",
                price,
                0,
                Decimal("0"),
                None,
            )
            for price in prices
        ]
        self.assertIn("BUY", [item.action for item in decisions])


class AnalystPriorityBonusTests(unittest.TestCase):
    """TradingStrategy.analyst_priority_bonus is a pure, two-sided nudge -
    see priority_score. Must default to neutral (0) whenever coverage is
    missing, since many of this bot's penny/micro-cap names simply aren't
    covered by analysts at all - that must read as "no signal", never as
    a de facto exclusion.
    """

    def test_no_rating_and_no_target_is_neutral(self):
        bonus = TradingStrategy.analyst_priority_bonus(
            Decimal("10"), None, None, Decimal("5")
        )
        self.assertEqual(bonus, Decimal("0"))

    def test_bullish_rating_and_price_well_below_target_is_positive(self):
        bonus = TradingStrategy.analyst_priority_bonus(
            Decimal("10"),
            Decimal("15"),
            {"strong_buy": 10, "buy": 0, "hold": 0, "sell": 0, "under_perform": 0},
            Decimal("5"),
        )
        self.assertEqual(bonus, Decimal("5"))

    def test_bearish_rating_and_price_above_target_is_negative(self):
        bonus = TradingStrategy.analyst_priority_bonus(
            Decimal("15"),
            Decimal("10"),
            {"strong_buy": 0, "buy": 0, "hold": 0, "sell": 0, "under_perform": 10},
            Decimal("5"),
        )
        self.assertEqual(bonus, Decimal("-5"))

    def test_evenly_split_rating_at_target_price_is_neutral(self):
        bonus = TradingStrategy.analyst_priority_bonus(
            Decimal("10"),
            Decimal("10"),
            {"strong_buy": 5, "buy": 0, "hold": 0, "sell": 5, "under_perform": 0},
            Decimal("5"),
        )
        self.assertEqual(bonus, Decimal("0"))

    def test_extreme_upside_is_clipped_not_unbounded(self):
        # target 10x the current price - the +-50% clip must cap this the
        # same as a merely 50%-undervalued price would, not scale further.
        extreme = TradingStrategy.analyst_priority_bonus(
            Decimal("10"), Decimal("100"), None, Decimal("5")
        )
        moderate = TradingStrategy.analyst_priority_bonus(
            Decimal("10"), Decimal("15"), None, Decimal("5")
        )
        self.assertEqual(extreme, moderate)

    def test_non_positive_price_is_neutral(self):
        bonus = TradingStrategy.analyst_priority_bonus(
            Decimal("0"),
            Decimal("15"),
            {"strong_buy": 10, "buy": 0, "hold": 0, "sell": 0, "under_perform": 0},
            Decimal("5"),
        )
        self.assertEqual(bonus, Decimal("0"))


class AnalystDataServiceTests(unittest.TestCase):
    """Constructed via __new__ throughout - never calls __init__, so no
    real background thread ever starts in these tests. request()/
    snapshot() are the only two methods the main trading thread actually
    touches; _fetch() is exercised directly, standing in for what the
    (untested-here) worker thread does with a dequeued item.
    """

    def _service(self, **config_overrides):
        from webull_bot.analyst_data import AnalystDataService

        service = AnalystDataService.__new__(AnalystDataService)
        defaults = dict(
            analyst_priority_enabled=True,
            analyst_data_cache_seconds=43200,
            analyst_priority_bonus_max=Decimal("5"),
        )
        defaults.update(config_overrides)
        service.config = SimpleNamespace(**defaults)
        service.log = logging.getLogger("test-analyst-data")
        service._lock = threading.Lock()
        service._bonus = {}
        service._fetched_at = {}
        service._queued = set()
        service._queue = queue.Queue(maxsize=50)
        return service

    def test_request_enqueues_a_never_seen_symbol(self):
        service = self._service()
        service.request("AAPL", Decimal("100"))
        self.assertIn("AAPL", service._queued)
        self.assertEqual(service._queue.qsize(), 1)

    def test_request_enqueues_a_never_seen_symbol_even_when_monotonic_is_small(self):
        """Regression test: time.monotonic() is relative to an arbitrary
        reference point (often host boot), not the epoch - on a
        freshly-booted host it can be well under
        ANALYST_DATA_CACHE_SECONDS. A never-fetched symbol must still be
        eligible in that case; treating "never fetched" as "fetched at
        monotonic time zero" (a 0.0 default) silently broke every
        first-ever fetch on a fresh CI runner.
        """
        service = self._service()
        with unittest.mock.patch("time.monotonic", return_value=5.0):
            service.request("AAPL", Decimal("100"))
        self.assertIn("AAPL", service._queued)
        self.assertEqual(service._queue.qsize(), 1)

    def test_request_skips_a_symbol_already_queued(self):
        service = self._service()
        service.request("AAPL", Decimal("100"))
        service.request("AAPL", Decimal("101"))
        self.assertEqual(service._queue.qsize(), 1)

    def test_request_skips_a_symbol_still_within_the_cache_window(self):
        service = self._service()
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            service._fetched_at["AAPL"] = 100.0
        with unittest.mock.patch("time.monotonic", return_value=200.0):
            service.request("AAPL", Decimal("100"))
        self.assertEqual(service._queue.qsize(), 0)

    def test_request_refetches_once_the_cache_window_elapses(self):
        service = self._service(analyst_data_cache_seconds=50)
        service._fetched_at["AAPL"] = 100.0
        with unittest.mock.patch("time.monotonic", return_value=200.0):
            service.request("AAPL", Decimal("100"))
        self.assertEqual(service._queue.qsize(), 1)

    def test_request_disabled_is_a_full_noop(self):
        service = self._service(analyst_priority_enabled=False)
        service.request("AAPL", Decimal("100"))
        self.assertEqual(service._queue.qsize(), 0)
        self.assertNotIn("AAPL", service._queued)

    def test_fetch_populates_the_bonus_and_snapshot_reflects_it(self):
        service = self._service()
        # 50% upside (the formula's clip boundary) + unanimous strong_buy:
        # both signals max out at 1.0, so bonus == bonus_max exactly -
        # see AnalystPriorityBonusTests for the formula itself.
        service.api = SimpleNamespace(
            analyst_target_price=lambda symbol: Decimal("150"),
            analyst_rating=lambda symbol: {
                "strong_buy": 10,
                "buy": 0,
                "hold": 0,
                "sell": 0,
                "under_perform": 0,
            },
        )
        service._fetch("AAPL", Decimal("100"))
        self.assertEqual(service.snapshot(), {"AAPL": 5.0})

    def test_fetch_failure_is_swallowed_by_the_worker_not_fetch_itself(self):
        # _fetch() itself propagates - the worker loop (not under test
        # here) is what catches it, matching MarketResearchAgent's
        # _worker/_research split.
        service = self._service()
        service.api = SimpleNamespace(
            analyst_target_price=lambda symbol: (_ for _ in ()).throw(
                RuntimeError("no coverage")
            ),
            analyst_rating=lambda symbol: None,
        )
        with self.assertRaises(RuntimeError):
            service._fetch("AAPL", Decimal("100"))
        self.assertEqual(service.snapshot(), {})


class TradeEventStreamServiceTests(unittest.TestCase):
    """Phase 0 of the polling-to-streaming migration (see the plan) -
    constructed via __new__ throughout, so no real gRPC connection or
    background thread ever starts in these tests. _on_events_message()/
    drain() are the only two touchpoints the rest of the bot actually
    uses; the SDK-facing pieces (_run, _silence_sdk_prints) aren't
    exercised here.
    """

    def _service(self):
        from webull_bot.trade_events import TradeEventStreamService

        service = TradeEventStreamService.__new__(TradeEventStreamService)
        service.config = SimpleNamespace(
            webull_app_key="key", webull_app_secret="secret",
            webull_region_id="us", account_id="acct-1",
        )
        service.log = logging.getLogger("test-trade-events")
        service._queue = queue.Queue(maxsize=3)
        return service

    def test_on_events_message_enqueues(self):
        service = self._service()
        service._on_events_message(1024, 1, {"a": 1}, raw_message=None)
        self.assertEqual(service.drain(), [(1024, 1, {"a": 1})])

    def test_drain_returns_events_in_order_and_clears_the_queue(self):
        service = self._service()
        service._on_events_message(1024, 1, {"n": 1}, raw_message=None)
        service._on_events_message(1028, 2, {"n": 2}, raw_message=None)
        self.assertEqual(
            service.drain(), [(1024, 1, {"n": 1}), (1028, 2, {"n": 2})]
        )
        self.assertEqual(service.drain(), [])

    def test_a_full_queue_drops_the_oldest_not_the_newest(self):
        service = self._service()
        for n in range(4):
            service._on_events_message(1024, 1, {"n": n}, raw_message=None)
        # maxsize=3: the oldest (n=0) must be the one dropped.
        self.assertEqual(
            service.drain(), [(1024, 1, {"n": 1}), (1024, 1, {"n": 2}), (1024, 1, {"n": 3})]
        )


class TradeEventLoggingTests(unittest.TestCase):
    """AutoTrader.log_trade_events is purely observational (Phase 0) -
    it must never touch trading state, only log.
    """

    def test_noop_when_the_service_is_disabled(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(trade_event_service=None)
        log_trade_events = AutoTrader.log_trade_events.__get__(fake_bot)
        log_trade_events()  # must not raise

    def test_drains_and_logs_every_event(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            trade_event_service=SimpleNamespace(
                drain=lambda: [(1024, 1, {"order_id": "abc"})]
            )
        )
        log_trade_events = AutoTrader.log_trade_events.__get__(fake_bot)
        with self.assertLogs("webull-bot", level="INFO") as logs:
            log_trade_events()
        self.assertIn("abc", logs.output[0])


class VolatilityScalpTests(StrategyConfigMixin, unittest.TestCase):
    """Buy small dips, sell small rips, repeatedly, on symbols whose own
    realized short-window volatility clears volatility_scalp_min_stdev_
    percent - see strategy.py's realized_volatility_percent/
    is_volatility_scalp_eligible/volatility_scalp_dip_signal/
    volatility_scalp_exit_override.
    """

    def _feed(self, strategy, symbol, prices):
        for price in prices:
            strategy.update_stock_snapshot(
                {"symbol": symbol, "volume": "1000", "price": str(price)},
                Decimal(str(price)),
            )

    def test_not_eligible_with_too_few_samples(self):
        strategy = TradingStrategy(self.config())
        self._feed(strategy, "THIN", [10, 10.1, 9.9])
        self.assertIsNone(strategy.realized_volatility_percent("THIN"))
        self.assertFalse(strategy.is_volatility_scalp_eligible("THIN"))

    def test_calm_symbol_is_not_eligible(self):
        strategy = TradingStrategy(self.config())
        # Tiny back-and-forth well under the 1.5% stdev threshold.
        self._feed(strategy, "CALM", [10.00, 10.01, 9.99, 10.00, 10.01, 9.99, 10.00])
        stdev = strategy.realized_volatility_percent("CALM")
        self.assertIsNotNone(stdev)
        self.assertLess(stdev, self.config().volatility_scalp_min_stdev_percent)
        self.assertFalse(strategy.is_volatility_scalp_eligible("CALM"))

    def test_choppy_symbol_is_eligible(self):
        strategy = TradingStrategy(self.config())
        # Swings of several percent each step - clears the 1.5% stdev bar.
        self._feed(strategy, "WILD", [10, 10.5, 9.6, 10.4, 9.7, 10.3, 9.8])
        stdev = strategy.realized_volatility_percent("WILD")
        self.assertIsNotNone(stdev)
        self.assertGreaterEqual(stdev, self.config().volatility_scalp_min_stdev_percent)
        self.assertTrue(strategy.is_volatility_scalp_eligible("WILD"))

    def test_high_stdev_low_volume_symbol_is_not_eligible(self):
        """By request: "the stocks being chosen have very low volume,
        thus they do not fluctuate much, we need high volume stocks
        for more volatility." A thin name can clear the stdev bar
        purely from a few small prints, without real tradeable volume
        behind the move.
        """
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_min_volume = 500_000
        self._feed(strategy, "THINWILD", [10, 10.5, 9.6, 10.4, 9.7, 10.3, 9.8])
        stdev = strategy.realized_volatility_percent("THINWILD")
        self.assertIsNotNone(stdev)
        self.assertGreaterEqual(stdev, self.config().volatility_scalp_min_stdev_percent)
        # _feed only supplies "volume": "1000" per tick - well under the
        # 500k floor.
        self.assertFalse(strategy.is_volatility_scalp_eligible("THINWILD"))

    def test_high_stdev_high_volume_symbol_is_eligible(self):
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_min_volume = 500_000
        for price in (10, 10.5, 9.6, 10.4, 9.7, 10.3, 9.8):
            strategy.update_stock_snapshot(
                {"symbol": "LIQUIDWILD", "volume": "600000", "price": str(price)},
                Decimal(str(price)),
            )
        self.assertTrue(strategy.is_volatility_scalp_eligible("LIQUIDWILD"))

    def test_disabled_in_config_is_never_eligible_even_when_choppy(self):
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_enabled = False
        self._feed(strategy, "WILD", [10, 10.5, 9.6, 10.4, 9.7, 10.3, 9.8])
        self.assertFalse(strategy.is_volatility_scalp_eligible("WILD"))

    def test_dip_signal_fires_once_price_pulls_back_far_enough_from_the_local_high(self):
        strategy = TradingStrategy(self.config())
        self._feed(strategy, "WILD", [10, 10.5, 9.6, 10.4, 9.7, 10.3, 9.8])
        # Local high is over the last 5 samples only (9.6, 10.4, 9.7,
        # 10.3, 9.8) = 10.4, NOT the whole window's 10.5 - a stock making
        # new highs every sample (a strong trend) should still register a
        # real short pullback as a dip. 0.5% below 10.4 is 10.348.
        self.assertFalse(strategy.volatility_scalp_dip_signal("WILD", Decimal("10.36")))
        self.assertTrue(strategy.volatility_scalp_dip_signal("WILD", Decimal("10.30")))

    def test_dip_signal_reacts_to_recent_pullback_even_in_a_strong_uptrend(self):
        """Live incident: HOWL, up ~100% intraday, kept making new window
        highs almost every sample - a dip measured against the WHOLE
        window's high almost never fired. Measuring against only the
        last few samples keeps the signal reactive to real local
        pullbacks regardless of the larger trend.
        """
        strategy = TradingStrategy(self.config())
        # A strong uptrend where every new sample is a new all-time-
        # window high, then one small pullback (6.90), THEN a small
        # bounce off that pullback (6.95) - see the bounce-confirmation
        # tests below for why the entry now waits for the bounce tick.
        self._feed(strategy, "HOWL", [1, 2, 3, 4, 5, 5.5, 6, 6.5, 7, 6.90])
        self.assertFalse(strategy.volatility_scalp_dip_signal("HOWL", Decimal("6.99")))
        self.assertTrue(strategy.volatility_scalp_dip_signal("HOWL", Decimal("6.95")))

    def test_dip_signal_false_for_an_unseen_symbol(self):
        strategy = TradingStrategy(self.config())
        self.assertFalse(strategy.volatility_scalp_dip_signal("NEVERSEEN", Decimal("10")))

    def test_dip_signal_does_not_require_a_bounce_by_design(self):
        """By explicit request: keep buying the dip continuously, even
        while price is still actively declining tick to tick, rather
        than waiting for a confirmed reversal - the user wants constant,
        high-frequency trading on this cohort and accepts the resulting
        losses as the cost of that. An earlier version of this signal
        required an uptick from the immediately-preceding sample before
        firing; that requirement has been deliberately removed.
        """
        strategy = TradingStrategy(self.config())
        # 10.5 -> 10.4 -> 10.3 -> 10.2: still declining every tick.
        self._feed(strategy, "WILD", [10.5, 10.4, 10.3, 10.2])
        # Still falling relative to the last sample (10.2), but clears
        # the drop-from-local-high threshold - fires anyway.
        self.assertTrue(strategy.volatility_scalp_dip_signal("WILD", Decimal("10.15")))

    def test_dip_signal_excludes_the_current_price_when_already_appended(self):
        """In live usage (via update_stock_snapshot), the window's last
        element IS already this same price by the time this runs. If the
        local-high lookback naively included it as an extra sample, the
        real recent high can get pushed out of the last-N window and the
        signal would compare price against itself (0% drop, never a
        dip) instead of the actual recent high.
        """
        strategy = TradingStrategy(self.config())
        # True recent high is 10.5 (the oldest sample) - if the current
        # price (9.5) were double-counted as a 6th sample, the last-5
        # lookback would push 10.5 out and see only 9.0s and the current
        # price itself, masking a real ~9.5% dip.
        strategy.volatility_price_history["WILD"].extend(
            [10.5, 9.0, 9.0, 9.0, 9.0, 9.5]
        )
        self.assertTrue(strategy.volatility_scalp_dip_signal("WILD", Decimal("9.5")))

    def test_target_price_is_cost_plus_the_configured_small_percent(self):
        strategy = TradingStrategy(self.config())
        target = strategy.volatility_scalp_target_price(Decimal("20.00"))
        self.assertEqual(target, Decimal("20.00") * Decimal("1.005"))

    def test_exit_override_promotes_hold_to_profit_once_the_quick_target_is_cleared(self):
        strategy = TradingStrategy(self.config())
        from webull_bot.strategy import Decision

        hold = Decision("HOLD", "position between target and stop", Decimal("20.50"))
        result = strategy.volatility_scalp_exit_override(
            hold, quantity=10, average_cost=Decimal("20.00"), price=Decimal("20.15")
        )
        self.assertEqual(result.action, "PROFIT")
        self.assertEqual(result.target_price, Decimal("20.00") * Decimal("1.005"))

    def test_exit_override_leaves_hold_alone_below_the_quick_target(self):
        strategy = TradingStrategy(self.config())
        from webull_bot.strategy import Decision

        hold = Decision("HOLD", "position between target and stop", Decimal("20.50"))
        result = strategy.volatility_scalp_exit_override(
            hold, quantity=10, average_cost=Decimal("20.00"), price=Decimal("20.05")
        )
        self.assertIs(result, hold)

    def test_exit_override_suppresses_a_loss_in_favor_of_averaging_down(self):
        """By explicit request ("focus less on the stop loss" /
        "average it out with another buy"): a LOSS decision on a
        volatility-scalp position is downgraded to HOLD instead of
        being allowed to stop the position out - the position is meant
        to be averaged into on a dip (see AutoTrader's averaging-buy
        entry path), not exited at a loss.
        """
        strategy = TradingStrategy(self.config())
        from webull_bot.strategy import Decision

        loss = Decision("LOSS", "percentage stop reached", Decimal("19.00"))
        result = strategy.volatility_scalp_exit_override(
            loss, quantity=10, average_cost=Decimal("20.00"), price=Decimal("20.20")
        )
        self.assertEqual(result.action, "HOLD")

    def test_exit_override_lets_the_stop_through_past_the_hard_stop_floor(self):
        """Research finding (compared against freqtrade's documented DCA
        pattern after "basically only taking losses" was reported live):
        a mature DCA implementation never fully suppresses the stop-loss
        during averaging - it keeps a wide-but-always-active hard stop
        as a catastrophic-loss backstop. VOLATILITY_SCALP_HARD_STOP_
        PERCENT (5% in the test config) restores that: a drop beyond it
        means a real breakdown, not a normal dip within the DCA ladder's
        own range, so the real stop-loss is let through.
        """
        strategy = TradingStrategy(self.config())
        from webull_bot.strategy import Decision

        loss = Decision("LOSS", "percentage stop reached", Decimal("18.90"))
        # (20.00 - 18.90) / 20.00 = 5.5% - past the 5% hard stop floor.
        result = strategy.volatility_scalp_exit_override(
            loss,
            quantity=10,
            average_cost=Decimal("20.00"),
            price=Decimal("18.90"),
            averaging_available=True,
        )
        self.assertIs(result, loss)

    def test_exit_override_still_suppresses_a_loss_within_the_hard_stop_floor(self):
        strategy = TradingStrategy(self.config())
        from webull_bot.strategy import Decision

        loss = Decision("LOSS", "percentage stop reached", Decimal("19.50"))
        # (20.00 - 19.50) / 20.00 = 2.5% - well within the 5% floor.
        result = strategy.volatility_scalp_exit_override(
            loss,
            quantity=10,
            average_cost=Decimal("20.00"),
            price=Decimal("19.50"),
            averaging_available=True,
        )
        self.assertEqual(result.action, "HOLD")

    def test_exit_override_leaves_a_loss_alone_when_flat_or_no_cost_basis(self):
        strategy = TradingStrategy(self.config())
        from webull_bot.strategy import Decision

        loss = Decision("LOSS", "percentage stop reached", Decimal("19.00"))
        self.assertIs(
            strategy.volatility_scalp_exit_override(
                loss, quantity=0, average_cost=Decimal("20.00"), price=Decimal("20.20")
            ),
            loss,
        )
        self.assertIs(
            strategy.volatility_scalp_exit_override(
                loss, quantity=10, average_cost=Decimal("0"), price=Decimal("20.20")
            ),
            loss,
        )

    def test_exit_override_leaves_a_loss_alone_when_averaging_is_not_available(self):
        """Sanity-check fix: a position that gets the fast profit-take
        purely because its symbol is in the cohort, but was never opened
        via the dip-buy path (e.g. a normal trend-strategy position),
        has no averaging-down recovery plan behind it. Suppressing its
        stop-loss with nothing else backing it up would leave it
        bleeding indefinitely with no path back to even - its normal
        stop-loss must stay fully in effect.
        """
        strategy = TradingStrategy(self.config())
        from webull_bot.strategy import Decision

        loss = Decision("LOSS", "percentage stop reached", Decimal("19.00"))
        result = strategy.volatility_scalp_exit_override(
            loss,
            quantity=10,
            average_cost=Decimal("20.00"),
            price=Decimal("20.20"),
            averaging_available=False,
        )
        self.assertIs(result, loss)

    def test_average_down_signal_fires_once_price_clears_the_dip_threshold(self):
        strategy = TradingStrategy(self.config())
        # dip_entry_percent default 0.2% - 0.15% below cost doesn't
        # clear it, 0.5% below does.
        self.assertFalse(
            strategy.volatility_scalp_average_down_signal(
                price=Decimal("19.97"), average_cost=Decimal("20.00")
            )
        )
        self.assertTrue(
            strategy.volatility_scalp_average_down_signal(
                price=Decimal("19.90"), average_cost=Decimal("20.00")
            )
        )

    def test_average_down_signal_false_above_cost_or_with_no_cost_basis(self):
        strategy = TradingStrategy(self.config())
        self.assertFalse(
            strategy.volatility_scalp_average_down_signal(
                price=Decimal("20.10"), average_cost=Decimal("20.00")
            )
        )
        self.assertFalse(
            strategy.volatility_scalp_average_down_signal(
                price=Decimal("19.00"), average_cost=Decimal("0")
            )
        )

    def test_average_down_signal_widens_the_required_drop_per_level(self):
        """Structural fix (not a same-day band-aid): live incident, BTCT
        averaged down at 1.79 then 1.78 - essentially the same price,
        gaining no real risk reduction per add. Each successive
        averaging level now requires a proportionally bigger drop -
        level 0 uses the base 0.5% (this test's config), level 1 needs
        1.5x that (0.75%), level 2 needs 2x (1.0%), etc., via
        VOLATILITY_SCALP_AVERAGING_STEP_MULTIPLIER (0.5 here).
        """
        strategy = TradingStrategy(self.config())
        cost = Decimal("20.00")
        # Level 0: base 0.5% - 19.91 (0.45%) doesn't clear, 19.90 (0.5%)
        # does, matching the un-widened test above.
        self.assertFalse(
            strategy.volatility_scalp_average_down_signal(
                price=Decimal("19.91"), average_cost=cost, level=0
            )
        )
        self.assertTrue(
            strategy.volatility_scalp_average_down_signal(
                price=Decimal("19.90"), average_cost=cost, level=0
            )
        )
        # Level 1: requires 1.5x the base (0.75%) - the same 0.5% drop
        # that cleared level 0 is no longer enough.
        self.assertFalse(
            strategy.volatility_scalp_average_down_signal(
                price=Decimal("19.90"), average_cost=cost, level=1
            )
        )
        self.assertTrue(
            strategy.volatility_scalp_average_down_signal(
                price=Decimal("19.85"), average_cost=cost, level=1
            )
        )
        # Level 2: requires 2x the base (1.0%).
        self.assertFalse(
            strategy.volatility_scalp_average_down_signal(
                price=Decimal("19.85"), average_cost=cost, level=2
            )
        )
        self.assertTrue(
            strategy.volatility_scalp_average_down_signal(
                price=Decimal("19.80"), average_cost=cost, level=2
            )
        )

    def test_clear_market_state_resets_the_volatility_window(self):
        strategy = TradingStrategy(self.config())
        self._feed(strategy, "WILD", [10, 10.5, 9.6, 10.4, 9.7, 10.3, 9.8])
        strategy.clear_market_state()
        self.assertIsNone(strategy.realized_volatility_percent("WILD"))

    def test_seed_volatility_window_populates_an_empty_window(self):
        strategy = TradingStrategy(self.config())
        strategy.seed_volatility_window("NEW", [10.0, 10.2, 9.9, 10.1, 9.8, 10.3])
        self.assertIsNotNone(strategy.realized_volatility_percent("NEW"))

    def test_seed_volatility_window_never_overwrites_existing_live_history(self):
        strategy = TradingStrategy(self.config())
        self._feed(strategy, "WILD", [10, 10.5, 9.6, 10.4, 9.7])
        before = list(strategy.volatility_price_history["WILD"])
        strategy.seed_volatility_window("WILD", [1.0, 1.1, 1.2, 1.3, 1.4, 1.5])
        self.assertEqual(list(strategy.volatility_price_history["WILD"]), before)

    def test_seed_volatility_window_skips_non_positive_closes(self):
        strategy = TradingStrategy(self.config())
        strategy.seed_volatility_window("NEW", [10.0, 0, -1, 10.2, 9.9])
        self.assertEqual(list(strategy.volatility_price_history["NEW"]), [10.0, 10.2, 9.9])

    def test_share_count_targets_the_configured_notional_under_a_dollar(self):
        """Rounds to the nearest 100 shares (Webull's own lot-restricted-
        band minimum under $1) - by request, not flatly capped at 100
        anymore, this genuinely scales with the target notional.
        """
        strategy = TradingStrategy(self.config())
        # 400 / 0.89 = 449.4... -> floor to 449 -> round down to the
        # nearest 100 -> 400 shares, well over the old flat 100.
        self.assertEqual(
            strategy.volatility_scalp_share_count(Decimal("0.89")), 400
        )

    def test_share_count_targets_the_configured_notional_at_a_dollar_and_up(self):
        """Rounds to the nearest 10 shares at $1+ - "in the tens, if not
        the hundreds," scaling with both price and the target notional.
        """
        strategy = TradingStrategy(self.config())
        # 400 / 1.99 = 201.0... -> floor to 201 -> round down to 200.
        self.assertEqual(
            strategy.volatility_scalp_share_count(Decimal("1.99")), 200
        )
        # 400 / 5.00 = 80.
        self.assertEqual(strategy.volatility_scalp_share_count(Decimal("5.00")), 80)

    def test_share_count_under_a_dollar_always_reaches_the_exchange_minimum(self):
        """Sub-$1 has no smaller valid order than 100 shares (Webull's
        own lot-restricted-band rule) - even a tiny target still rounds
        UP to it, since there's no smaller legal alternative. The
        caller's own affordability/exposure checks are the real
        backstop on this, not this function.
        """
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_target_notional = Decimal("5")
        self.assertEqual(strategy.volatility_scalp_share_count(Decimal("0.50")), 100)

    def test_share_count_at_a_dollar_and_up_degrades_gracefully_below_one_lot(self):
        """$1+ has no exchange-mandated minimum - a target too small for
        a full 10-share lot still buys whatever whole-share quantity it
        can actually afford, rather than forcing a 10-share lot or
        skipping the trade entirely.
        """
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_target_notional = Decimal("5")
        self.assertEqual(strategy.volatility_scalp_share_count(Decimal("4.00")), 1)

    def test_share_count_is_zero_above_the_max_price_cap(self):
        strategy = TradingStrategy(self.config())
        self.assertEqual(strategy.volatility_scalp_share_count(Decimal("5.01")), 0)

    def test_share_count_is_zero_for_a_non_positive_price(self):
        strategy = TradingStrategy(self.config())
        self.assertEqual(strategy.volatility_scalp_share_count(Decimal("0")), 0)

    def test_buying_power_fraction_shrinks_the_target_on_a_small_account(self):
        """Live sanity check caught this: on a small account, a flat
        dollar target alone gets silently zeroed by the caller's
        affordability check on nearly every attempt. Passing buying_
        power scales the actual target down automatically instead.
        """
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_target_notional = Decimal("400")
        strategy.config.volatility_scalp_target_notional_buying_power_fraction = (
            Decimal("0.15")
        )
        # buying_power=$107.80 -> target = min(400, 107.80*0.15=16.17).
        # 16.17 / 3.00 = 5.39 -> floor 5, under 10 -> degrades to 5.
        self.assertEqual(
            strategy.volatility_scalp_share_count(
                Decimal("3.00"), buying_power=Decimal("107.80")
            ),
            5,
        )

    def test_buying_power_none_falls_back_to_the_flat_target(self):
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_target_notional = Decimal("400")
        self.assertEqual(
            strategy.volatility_scalp_share_count(Decimal("5.00"), buying_power=None),
            80,
        )

    def test_non_positive_buying_power_falls_back_to_the_flat_target(self):
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_target_notional = Decimal("400")
        self.assertEqual(
            strategy.volatility_scalp_share_count(
                Decimal("5.00"), buying_power=Decimal("0")
            ),
            80,
        )

    def test_intensity_scales_down_a_dollar_and_up_trade_size(self):
        """By request: "lessen the intensity" outside core hours without
        stopping trading - intensity dampens the target notional for
        $1+ trades, where it's the only lever that actually changes
        anything (see the sub-$1 test just below for why).
        """
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_target_notional = Decimal("400")
        # Full intensity: 400 / 5.00 = 80.
        self.assertEqual(
            strategy.volatility_scalp_share_count(
                Decimal("5.00"), intensity=Decimal("1")
            ),
            80,
        )
        # 40% intensity: 160 / 5.00 = 32.
        self.assertEqual(
            strategy.volatility_scalp_share_count(
                Decimal("5.00"), intensity=Decimal("0.4")
            ),
            30,
        )

    def test_intensity_has_no_effect_on_the_sub_dollar_exchange_floor(self):
        """Sub-$1 orders always round UP to at least 100 shares - Webull's
        own lot-restricted-band minimum leaves no smaller valid order,
        so dampening the soft target can't shrink this specific trade
        size regardless of intensity.
        """
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_target_notional = Decimal("400")
        self.assertEqual(
            strategy.volatility_scalp_share_count(
                Decimal("0.50"), intensity=Decimal("0.1")
            ),
            100,
        )

    def test_intensity_clamped_to_the_zero_to_one_range(self):
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_target_notional = Decimal("400")
        # Negative or >1 intensity should behave like the nearest valid
        # bound (0 or 1), not silently invert or amplify sizing.
        self.assertEqual(
            strategy.volatility_scalp_share_count(
                Decimal("5.00"), intensity=Decimal("-1")
            ),
            0,
        )
        self.assertEqual(
            strategy.volatility_scalp_share_count(
                Decimal("5.00"), intensity=Decimal("2")
            ),
            80,
        )


class VolatilityScalpVwapGateTests(StrategyConfigMixin, unittest.TestCase):
    """volatility_scalp_vwap_supports_entry - by request, after an end-
    of-day retrospective ("we just kept buying at the wrong time"): the
    SMA trend filter only catches a multi-day downtrend, nothing for a
    stock simply having a bad DAY today specifically. Uses its own much
    wider band than the general vwap_supports_entry.
    """

    def test_blocks_when_price_sits_well_below_session_vwap(self):
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_vwap_band_percent = Decimal("0.05")
        # Feed volume so VWAP actually accumulates (cum_pv/cum_vol).
        strategy.update_stock_snapshot(
            {"symbol": "WEAK", "volume": "1000", "price": "10.00"},
            Decimal("10.00"),
        )
        strategy.update_stock_snapshot(
            {"symbol": "WEAK", "volume": "2000", "price": "10.00"},
            Decimal("10.00"),
        )
        # VWAP ~10.00, band 5% -> floor 9.50. Price 9.00 is well below.
        self.assertFalse(
            strategy.volatility_scalp_vwap_supports_entry("WEAK", Decimal("9.00"))
        )

    def test_allows_a_normal_dip_within_the_wider_band(self):
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_vwap_band_percent = Decimal("0.05")
        strategy.update_stock_snapshot(
            {"symbol": "OK", "volume": "1000", "price": "10.00"},
            Decimal("10.00"),
        )
        strategy.update_stock_snapshot(
            {"symbol": "OK", "volume": "2000", "price": "10.00"},
            Decimal("10.00"),
        )
        # 9.70 is only 3% below VWAP - within the 5% band, a normal
        # choppy-stock dip, not a real warning sign.
        self.assertTrue(
            strategy.volatility_scalp_vwap_supports_entry("OK", Decimal("9.70"))
        )

    def test_fails_open_with_no_vwap_data_yet(self):
        strategy = TradingStrategy(self.config())
        self.assertTrue(
            strategy.volatility_scalp_vwap_supports_entry(
                "NEVERSEEN", Decimal("10")
            )
        )


class VolatilityScalpEntrySpreadGateTests(StrategyConfigMixin, unittest.TestCase):
    """volatility_scalp_entry_spread_ok - by request, "make sure the
    algo plays around in the spread while ensuring a profit, or a
    profitable entry." Entries had no spread-quality check at all until
    now; reuses the exit side's own VOLATILITY_SCALP_MAX_EXIT_SPREAD_
    PERCENT bound for symmetry.
    """

    def test_blocks_an_absurdly_wide_spread(self):
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_max_exit_spread_percent = Decimal("8")
        strategy.metrics["WIDE"] = {"spread_percent": "15"}
        self.assertFalse(strategy.volatility_scalp_entry_spread_ok("WIDE"))

    def test_allows_a_spread_within_the_bound(self):
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_max_exit_spread_percent = Decimal("8")
        strategy.metrics["OK"] = {"spread_percent": "5"}
        self.assertTrue(strategy.volatility_scalp_entry_spread_ok("OK"))

    def test_no_spread_data_yet_does_not_block(self):
        strategy = TradingStrategy(self.config())
        self.assertTrue(strategy.volatility_scalp_entry_spread_ok("NEVERSEEN"))


class DualThrustBreakoutSignalTests(StrategyConfigMixin, unittest.TestCase):
    """dual_thrust_breakout_signal - an opening-range-breakout style
    entry trigger adapted from the classic Dual Thrust strategy, OR'd
    alongside the existing dip signal as an additional way to trigger a
    fresh entry (the mirror case - buy a fresh push to a new high,
    instead of buying a pullback).
    """

    def _feed(self, strategy, symbol, prices):
        for price in prices:
            strategy.update_stock_snapshot(
                {"symbol": symbol, "volume": "1000", "price": str(price)},
                Decimal(str(price)),
            )

    def test_fires_once_price_breaks_above_the_recent_range_by_k_times_its_size(self):
        strategy = TradingStrategy(self.config())
        # Last 5 samples: 9.6, 10.4, 9.7, 10.3, 9.8 -> range_high=10.4,
        # range_low=9.6, range=0.8. K=0.5 -> upper_band = 10.4 + 0.4 = 10.8.
        self._feed(strategy, "WILD", [10, 10.5, 9.6, 10.4, 9.7, 10.3, 9.8])
        self.assertFalse(
            strategy.dual_thrust_breakout_signal("WILD", Decimal("10.7"))
        )
        self.assertTrue(
            strategy.dual_thrust_breakout_signal("WILD", Decimal("10.9"))
        )

    def test_no_history_never_fires(self):
        strategy = TradingStrategy(self.config())
        self.assertFalse(
            strategy.dual_thrust_breakout_signal("NEVERSEEN", Decimal("10"))
        )

    def test_a_flat_range_never_fires(self):
        strategy = TradingStrategy(self.config())
        self._feed(strategy, "FLAT", [10, 10, 10, 10, 10, 10])
        self.assertFalse(
            strategy.dual_thrust_breakout_signal("FLAT", Decimal("10.5"))
        )


class HeikinAshiReversalSignalTests(StrategyConfigMixin, unittest.TestCase):
    """heikin_ashi_bullish_reversal_signal - a confirmed HA bullish
    reversal (red bar immediately followed by a green one with little/no
    lower wick) built from synthetic OHLC bars bucketed off the same
    rolling tick-price window, OR'd alongside the dip and breakout
    signals as a third independent entry trigger.
    """

    def _feed(self, strategy, symbol, prices):
        for price in prices:
            strategy.update_stock_snapshot(
                {"symbol": symbol, "volume": "1000", "price": str(price)},
                Decimal(str(price)),
            )

    def test_not_enough_history_never_fires(self):
        strategy = TradingStrategy(self.config())
        self._feed(strategy, "THIN", [10, 10.1, 9.9])
        self.assertFalse(strategy.heikin_ashi_bullish_reversal_signal("THIN"))

    def test_fires_on_a_confirmed_bullish_reversal(self):
        strategy = TradingStrategy(self.config())
        # 6 bars of 5 samples each: a steady decline (bars 1-5, each bar
        # closing lower than it opened) followed by one sharp, clean
        # rally (bar 6, closing well above its open with almost no
        # pullback) - a textbook red-then-green HA reversal.
        prices = []
        base = 10.0
        for _ in range(5):
            prices.extend([base, base - 0.02, base - 0.05, base - 0.08, base - 0.10])
            base -= 0.10
        prices.extend([base, base + 0.05, base + 0.15, base + 0.30, base + 0.50])
        self._feed(strategy, "REV", prices)
        self.assertTrue(strategy.heikin_ashi_bullish_reversal_signal("REV"))

    def test_a_continuing_decline_does_not_fire(self):
        strategy = TradingStrategy(self.config())
        prices = []
        base = 10.0
        for _ in range(6):
            prices.extend([base, base - 0.02, base - 0.05, base - 0.08, base - 0.10])
            base -= 0.10
        self._feed(strategy, "DOWN", prices)
        self.assertFalse(strategy.heikin_ashi_bullish_reversal_signal("DOWN"))


class ParabolicSarExitSignalTests(StrategyConfigMixin, unittest.TestCase):
    """parabolic_sar_exit_signal - an additional exit trigger for a held
    volatility-scalp position (alongside, not instead of, the existing
    quick profit target): fires once the trailing SAR level flips
    bearish over the synthetic bar series.
    """

    def _feed(self, strategy, symbol, prices):
        for price in prices:
            strategy.update_stock_snapshot(
                {"symbol": symbol, "volume": "1000", "price": str(price)},
                Decimal(str(price)),
            )

    def test_not_enough_history_never_fires(self):
        strategy = TradingStrategy(self.config())
        self._feed(strategy, "THIN", [10, 10.1, 9.9])
        self.assertFalse(
            strategy.parabolic_sar_exit_signal("THIN", Decimal("10"))
        )

    def test_fires_once_an_uptrend_reverses(self):
        strategy = TradingStrategy(self.config())
        # A steady 6-bar rally followed by a sharp reversal bar - SAR
        # should flip bearish once the reversal bar's low breaks the
        # trailing stop built up during the rally.
        prices = []
        base = 10.0
        for _ in range(6):
            prices.extend([base, base + 0.05, base + 0.02, base + 0.08, base + 0.10])
            base += 0.10
        prices.extend([base, base - 0.20, base - 0.40, base - 0.60, base - 0.80])
        self._feed(strategy, "TREND", prices)
        self.assertTrue(
            strategy.parabolic_sar_exit_signal("TREND", Decimal(str(base - 0.80)))
        )

    def test_a_continuing_uptrend_does_not_fire(self):
        strategy = TradingStrategy(self.config())
        prices = []
        base = 10.0
        for _ in range(6):
            prices.extend([base, base + 0.05, base + 0.02, base + 0.08, base + 0.10])
            base += 0.10
        self._feed(strategy, "TREND", prices)
        self.assertFalse(
            strategy.parabolic_sar_exit_signal("TREND", Decimal(str(base + 0.10)))
        )


class VolatilityScalpMomentumGateTests(StrategyConfigMixin, unittest.TestCase):
    """volatility_scalp_momentum_stalled_or_rising (entry side) and
    volatility_scalp_momentum_stalling (exit side) - by request: "we
    don't want to buy when there is downward momentum... buy when the
    dip is stalled or at the bottom, or even when the momentum starts
    to go up" and "if there is a profit and it doesn't seem to be going
    much higher, then sell it off... before the next dip." The entry
    gate requires a genuine two-step decline immediately beforehand,
    then fires on the very first tick that stops declining ("almost as
    the rise starts"); the exit gate requires two non-declining ticks.
    """

    def _feed(self, strategy, symbol, prices):
        for price in prices:
            strategy.update_stock_snapshot(
                {"symbol": symbol, "volume": "1000", "price": str(price)},
                Decimal(str(price)),
            )

    def test_entry_gate_blocks_while_still_falling(self):
        strategy = TradingStrategy(self.config())
        self._feed(strategy, "FALL", [10, 9.9, 9.8, 9.7])
        self.assertFalse(
            strategy.volatility_scalp_momentum_stalled_or_rising(
                "FALL", Decimal("9.6")
            )
        )

    def test_entry_gate_fires_the_instant_the_turn_happens(self):
        """By request: "the momentum stop being negative after
        consecutive downticks, then we buy, almost as the rise
        starts." A genuine two-step decline (10 -> 9.9 -> 9.75) just
        happened, and the very next tick merely stops falling (9.8,
        just above the last low) - fires immediately, not several
        ticks later.
        """
        strategy = TradingStrategy(self.config())
        self._feed(strategy, "TURN", [10, 9.9, 9.75])
        self.assertTrue(
            strategy.volatility_scalp_momentum_stalled_or_rising(
                "TURN", Decimal("9.8")
            )
        )

    def test_entry_gate_allows_once_momentum_turns_up(self):
        strategy = TradingStrategy(self.config())
        # Same genuine two-step decline, this time querying a clearer
        # rise (9.75) rather than just a flat stall.
        self._feed(strategy, "RISE", [10, 9.9, 9.7])
        self.assertTrue(
            strategy.volatility_scalp_momentum_stalled_or_rising(
                "RISE", Decimal("9.75")
            )
        )

    def test_entry_gate_blocks_without_a_genuine_prior_decline(self):
        """By request: momentum must have actually been negative
        (consecutive downticks) before the turn counts - a flat lead-
        in followed by an uptick is not "the rise starting after a
        dip," it's just noise, even though the current tick alone
        isn't declining.
        """
        strategy = TradingStrategy(self.config())
        self._feed(strategy, "FLATLEAD", [10, 10, 9.9])
        self.assertFalse(
            strategy.volatility_scalp_momentum_stalled_or_rising(
                "FLATLEAD", Decimal("9.95")
            )
        )

    def test_entry_gate_fails_open_with_no_history(self):
        strategy = TradingStrategy(self.config())
        self.assertTrue(
            strategy.volatility_scalp_momentum_stalled_or_rising(
                "NEVERSEEN", Decimal("10")
            )
        )

    def test_exit_gate_fires_once_upward_momentum_stalls(self):
        strategy = TradingStrategy(self.config())
        # THREE consecutive equal ticks (10.3, 10.3, 10.3) - upward
        # momentum has stopped making fresh highs for two ticks running
        # now (the stronger, recalibrated confirmation - a single flat
        # tick alone is no longer enough).
        self._feed(strategy, "RISE", [10, 10.1, 10.2, 10.3, 10.3, 10.3])
        self.assertTrue(
            strategy.volatility_scalp_momentum_stalling("RISE", Decimal("10.3"))
        )

    def test_exit_gate_does_not_fire_on_a_single_flat_tick(self):
        """Recalibrated by request - "too trigger happy to sell... not
        capturing the profits when it can" - a single flat tick is
        normal noise, not a real stall anymore.
        """
        strategy = TradingStrategy(self.config())
        self._feed(strategy, "RISE", [10, 10.1, 10.2, 10.3, 10.3])
        self.assertFalse(
            strategy.volatility_scalp_momentum_stalling("RISE", Decimal("10.3"))
        )

    def test_exit_gate_does_not_fire_while_still_climbing(self):
        strategy = TradingStrategy(self.config())
        self._feed(strategy, "RISE", [10, 10.1, 10.2, 10.3])
        self.assertFalse(
            strategy.volatility_scalp_momentum_stalling("RISE", Decimal("10.4"))
        )

    def test_exit_gate_fails_closed_with_no_history(self):
        strategy = TradingStrategy(self.config())
        self.assertFalse(
            strategy.volatility_scalp_momentum_stalling(
                "NEVERSEEN", Decimal("10")
            )
        )


class VolatilityScalpExitOverrideMomentumStallTests(
    StrategyConfigMixin, unittest.TestCase
):
    """volatility_scalp_exit_override's fourth exit path: any real
    profit combined with stalling upward momentum sells immediately,
    ahead of the fixed quick target or a full SAR reversal - "sell it
    off before the next dip."
    """

    def _feed(self, strategy, symbol, prices):
        for price in prices:
            strategy.update_stock_snapshot(
                {"symbol": symbol, "volume": "1000", "price": str(price)},
                Decimal(str(price)),
            )

    def test_fires_once_most_of_the_target_is_covered_and_momentum_stalls(self):
        strategy = TradingStrategy(self.config())
        # cost=10.29, target = 10.29 * 1.005 = 10.34145 (under the mixin's
        # 0.5% target_percent). min_stall_price (60% of the way from
        # cost to target) = 10.29 + (10.34145-10.29)*0.6 = 10.3209 -
        # price=10.33 clears that but stays under the full target, and
        # ends with TWO consecutive equal ticks (the recalibrated,
        # stronger stall confirmation).
        self._feed(strategy, "RISE", [10.1, 10.2, 10.29, 10.32, 10.33, 10.33, 10.33])
        from webull_bot.strategy import Decision

        result = strategy.volatility_scalp_exit_override(
            Decision("HOLD", "between target and stop", Decimal("10.33")),
            quantity=100,
            average_cost=Decimal("10.29"),
            price=Decimal("10.33"),
            averaging_available=True,
            symbol="RISE",
        )
        self.assertEqual(result.action, "PROFIT")
        self.assertIn("momentum stalling", result.reason)

    def test_does_not_fire_on_a_tiny_profit_even_if_momentum_stalls(self):
        """Recalibrated by request - "too trigger happy to sell... not
        capturing the profits when it can" - a tiny profit (well under
        VOLATILITY_SCALP_MOMENTUM_STALL_MIN_PROFIT_FRACTION of the way
        to the real target) no longer triggers an early exit just
        because momentum stalled for a couple ticks.
        """
        strategy = TradingStrategy(self.config())
        # cost=10.29, target=10.34145, min_stall_price=10.3209 - price
        #=10.30 is a real profit but well under that fraction.
        self._feed(strategy, "RISE", [10.1, 10.2, 10.29, 10.30, 10.30, 10.30])
        from webull_bot.strategy import Decision

        held = Decision("HOLD", "between target and stop", Decimal("10.30"))
        result = strategy.volatility_scalp_exit_override(
            held,
            quantity=100,
            average_cost=Decimal("10.29"),
            price=Decimal("10.30"),
            averaging_available=True,
            symbol="RISE",
        )
        self.assertIs(result, held)

    def test_never_fires_at_exactly_cost_no_real_profit(self):
        strategy = TradingStrategy(self.config())
        self._feed(strategy, "RISE", [10.1, 10.2, 10.3, 10.3])
        from webull_bot.strategy import Decision

        held = Decision("HOLD", "between target and stop", Decimal("10.3"))
        result = strategy.volatility_scalp_exit_override(
            held,
            quantity=100,
            average_cost=Decimal("10.30"),
            price=Decimal("10.3"),
            averaging_available=True,
            symbol="RISE",
        )
        self.assertIs(result, held)

    def test_does_not_fire_while_still_climbing(self):
        strategy = TradingStrategy(self.config())
        # cost=10.38 keeps this under the 0.5% quick target
        # (10.38 * 1.005 = 10.4319) and price=10.40 is still a fresh
        # high above the prior tick (10.35) - genuinely still climbing,
        # not stalled.
        self._feed(strategy, "RISE", [10.1, 10.2, 10.3, 10.35])
        from webull_bot.strategy import Decision

        held = Decision("HOLD", "between target and stop", Decimal("10.40"))
        result = strategy.volatility_scalp_exit_override(
            held,
            quantity=100,
            average_cost=Decimal("10.38"),
            price=Decimal("10.40"),
            averaging_available=True,
            symbol="RISE",
        )
        self.assertIs(result, held)


class VolatilityScalpExitOverrideSarTests(StrategyConfigMixin, unittest.TestCase):
    """volatility_scalp_exit_override's third exit path: a Parabolic SAR
    trend reversal, but only once price has at least cleared cost - it
    locks in a reversal early, without ever becoming a second, backdoor
    stop-loss (the LOSS suppression above deliberately disables the real
    one for this cohort).
    """

    def _feed(self, strategy, symbol, prices):
        for price in prices:
            strategy.update_stock_snapshot(
                {"symbol": symbol, "volume": "1000", "price": str(price)},
                Decimal(str(price)),
            )

    def _reversed_series(self, strategy, symbol):
        prices = []
        base = 10.0
        for _ in range(6):
            prices.extend([base, base + 0.05, base + 0.02, base + 0.08, base + 0.10])
            base += 0.10
        prices.extend([base, base - 0.20, base - 0.40, base - 0.60, base - 0.80])
        self._feed(strategy, symbol, prices)
        return Decimal(str(base - 0.80))

    def test_sar_reversal_triggers_profit_once_price_clears_cost(self):
        strategy = TradingStrategy(self.config())
        price = self._reversed_series(strategy, "TREND")
        from webull_bot.strategy import Decision

        result = strategy.volatility_scalp_exit_override(
            Decision("HOLD", "between target and stop", price),
            quantity=100,
            average_cost=price - Decimal("0.01"),
            price=price,
            averaging_available=True,
            symbol="TREND",
        )
        self.assertEqual(result.action, "PROFIT")
        self.assertIn("parabolic SAR", result.reason)

    def test_sar_reversal_never_fires_below_cost(self):
        """A SAR flip must never act as a backdoor stop-loss - this
        cohort's real stop-loss is deliberately suppressed while
        averaging is available, and SAR isn't meant to reintroduce it
        by another name.
        """
        strategy = TradingStrategy(self.config())
        price = self._reversed_series(strategy, "TREND")
        from webull_bot.strategy import Decision

        held = Decision("HOLD", "between target and stop", price)
        result = strategy.volatility_scalp_exit_override(
            held,
            quantity=100,
            average_cost=price + Decimal("1"),
            price=price,
            averaging_available=True,
            symbol="TREND",
        )
        self.assertIs(result, held)

    def test_no_symbol_passed_skips_the_sar_check_entirely(self):
        strategy = TradingStrategy(self.config())
        from webull_bot.strategy import Decision

        # cost just barely below price - well under the quick target
        # (0.5% default), so PROFIT can only come from a SAR check that
        # (with no symbol/history available) must never fire.
        held = Decision("HOLD", "between target and stop", Decimal("10"))
        result = strategy.volatility_scalp_exit_override(
            held,
            quantity=100,
            average_cost=Decimal("9.999"),
            price=Decimal("10"),
            averaging_available=True,
        )
        self.assertIs(result, held)


class VolatilityScalpReentryCooldownTests(unittest.TestCase):
    def test_ready_when_never_exited(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            last_exit_at={},
            config=SimpleNamespace(volatility_scalp_reentry_cooldown_seconds=5),
        )
        ready = AutoTrader.volatility_scalp_reentry_ready.__get__(fake_bot)
        self.assertTrue(ready("STOCK:WILD"))

    def test_blocked_immediately_after_an_exit(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            last_exit_at={"STOCK:WILD": time.monotonic()},
            config=SimpleNamespace(volatility_scalp_reentry_cooldown_seconds=5),
        )
        ready = AutoTrader.volatility_scalp_reentry_ready.__get__(fake_bot)
        self.assertFalse(ready("STOCK:WILD"))

    def test_ready_again_once_the_short_cooldown_elapses(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            last_exit_at={"STOCK:WILD": time.monotonic() - 10},
            config=SimpleNamespace(volatility_scalp_reentry_cooldown_seconds=5),
        )
        ready = AutoTrader.volatility_scalp_reentry_ready.__get__(fake_bot)
        self.assertTrue(ready("STOCK:WILD"))


class VolatilityScalpEntryPriceTests(unittest.TestCase):
    """volatility_scalp_entry_price - by request, "a lot of the orders
    are being cancelled... ensure the initial order itself is likely to
    be filled." Crosses at the (tick-quantized) ask instead of the
    passive bid/ask midpoint every other entry uses, guaranteeing a
    real chance to fill immediately instead of sitting for the full
    ORDER_TIMEOUT_SECONDS waiting for the market to fall back to a
    passive mid-price.
    """

    @staticmethod
    def _fake_bot():
        from webull_bot.bot import AutoTrader
        from webull_bot.webull_api import WebullAPI

        fake_bot = SimpleNamespace(
            api=SimpleNamespace(
                quote_ask=lambda q: (
                    Decimal(str(q["ask"])) if q.get("ask") else None
                ),
                price_tick_size=WebullAPI.price_tick_size,
            )
        )
        return AutoTrader.volatility_scalp_entry_price.__get__(fake_bot)

    def test_crosses_at_the_ask_for_a_dollar_plus_stock(self):
        entry_price = self._fake_bot()
        result = entry_price({"bid": "9.90", "ask": "10.05"})
        self.assertEqual(result, Decimal("10.05"))

    def test_crosses_at_the_ask_with_sub_penny_precision_under_a_dollar(self):
        entry_price = self._fake_bot()
        result = entry_price({"bid": "0.4590", "ask": "0.4600"})
        self.assertEqual(result, Decimal("0.4600"))

    def test_returns_none_with_no_valid_ask(self):
        entry_price = self._fake_bot()
        self.assertIsNone(entry_price({"bid": "9.90", "ask": None}))


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


class HasPendingBuyOrderTests(unittest.TestCase):
    """Live incident: with volatility_scalp_reentry_cooldown_seconds
    zeroed by request, self.volatility_scalp_positions was the ONLY
    thing preventing a duplicate BUY - and it gets wiped every cycle
    the account's position snapshot still reads flat, which is true the
    entire time a resting BUY order hasn't filled yet. MTNB got 5
    duplicate 100-share BUY orders stacked within ~70s in production
    because of this. has_pending_buy_order checks self.working_orders
    directly instead, independent of that stale snapshot.
    """

    @staticmethod
    def _fake_bot(working_orders):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(working_orders=working_orders)
        return AutoTrader.has_pending_buy_order.__get__(fake_bot)

    def test_true_while_an_uncancelled_buy_order_is_resting(self):
        has_pending = self._fake_bot(
            {
                "order-1": {
                    "key": "STOCK:MTNB",
                    "action": "BUY",
                    "cancel_requested_at": None,
                }
            }
        )
        self.assertTrue(has_pending("STOCK:MTNB"))

    def test_false_once_a_cancel_has_been_requested(self):
        has_pending = self._fake_bot(
            {
                "order-1": {
                    "key": "STOCK:MTNB",
                    "action": "BUY",
                    "cancel_requested_at": time.monotonic(),
                }
            }
        )
        self.assertFalse(has_pending("STOCK:MTNB"))

    def test_false_for_a_different_symbol(self):
        has_pending = self._fake_bot(
            {
                "order-1": {
                    "key": "STOCK:OTHER",
                    "action": "BUY",
                    "cancel_requested_at": None,
                }
            }
        )
        self.assertFalse(has_pending("STOCK:MTNB"))

    def test_false_for_a_non_buy_order_on_the_same_symbol(self):
        has_pending = self._fake_bot(
            {
                "order-1": {
                    "key": "STOCK:MTNB",
                    "action": "PROFIT",
                    "cancel_requested_at": None,
                }
            }
        )
        self.assertFalse(has_pending("STOCK:MTNB"))

    def test_false_with_no_working_orders_at_all(self):
        has_pending = self._fake_bot({})
        self.assertFalse(has_pending("STOCK:MTNB"))


class VolatilityScalpPositionValueCapTests(unittest.TestCase):
    """Live incident: GAUZ alone grew to ~66% of a small account's total
    value. volatility_scalp_position_value_ok caps any single cohort
    symbol's total position value (existing + a prospective new buy) to
    VOLATILITY_SCALP_MAX_POSITION_FRACTION of total account value.
    """

    @staticmethod
    def _fake_bot(account_value, max_fraction=Decimal("0.35")):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            cached_account_value=account_value,
            config=SimpleNamespace(volatility_scalp_max_position_fraction=max_fraction),
        )
        return AutoTrader.volatility_scalp_position_value_ok.__get__(fake_bot)

    def test_blocks_a_buy_that_would_exceed_the_fraction_of_account_value(self):
        check = self._fake_bot(account_value=Decimal("200"))
        # 300 shares * 0.44 = 132, well over 35% of 200 (=70).
        self.assertFalse(check(200, 100, Decimal("0.44")))

    def test_allows_a_buy_that_stays_within_the_fraction(self):
        check = self._fake_bot(account_value=Decimal("200"))
        # 100 shares * 0.44 = 44, under 35% of 200 (=70).
        self.assertTrue(check(0, 100, Decimal("0.44")))

    def test_fails_open_when_account_value_is_unknown(self):
        check = self._fake_bot(account_value=None)
        self.assertTrue(check(1000, 1000, Decimal("100")))

    def test_fails_open_when_account_value_is_non_positive(self):
        check = self._fake_bot(account_value=Decimal("0"))
        self.assertTrue(check(1000, 1000, Decimal("100")))

    def test_considers_the_existing_position_value_too(self):
        """An averaging-down buy must account for what's already held,
        not just the new clip - the cap is on the TOTAL resulting
        position, not each individual buy in isolation.
        """
        check = self._fake_bot(account_value=Decimal("200"))
        # Already holding 100 shares (44 worth) - adding another 100
        # (another 44) totals 88, over 35% of 200 (=70).
        self.assertFalse(check(100, 100, Decimal("0.44")))


class VolatilityScalpTotalExposureCapTests(unittest.TestCase):
    """Per-symbol caps alone don't bound worst case: several cohort
    symbols could each individually satisfy
    volatility_scalp_max_position_fraction while the account as a whole
    is almost entirely concentrated in the cohort during a correlated
    selloff. volatility_scalp_total_exposure_ok caps the WHOLE cohort's
    combined value instead.
    """

    @staticmethod
    def _fake_bot(account_value, symbols, max_fraction=Decimal("0.60")):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            cached_account_value=account_value,
            volatility_scalp_positions=set(symbols),
            config=SimpleNamespace(
                volatility_scalp_max_total_exposure_fraction=max_fraction
            ),
        )
        return AutoTrader.volatility_scalp_total_exposure_ok.__get__(fake_bot)

    def test_blocks_when_existing_cohort_positions_plus_the_new_buy_exceed_the_cap(
        self,
    ):
        check = self._fake_bot(
            account_value=Decimal("200"), symbols={"AAA", "BBB", "CCC"}
        )
        positions = [
            {"symbol": "AAA", "quantity": "100", "cost_price": "0.50"},
            {"symbol": "BBB", "quantity": "100", "cost_price": "0.50"},
        ]
        # 50 + 50 already held; a 20 more pushes total to 120, over 60% of 200 (=120 is exactly the boundary)
        self.assertFalse(check(positions, Decimal("20.01")))

    def test_allows_when_combined_cohort_value_stays_within_the_cap(self):
        check = self._fake_bot(
            account_value=Decimal("200"), symbols={"AAA", "BBB", "CCC"}
        )
        positions = [
            {"symbol": "AAA", "quantity": "100", "cost_price": "0.50"},
        ]
        # 50 already held; adding 20 totals 70, under 60% of 200 (=120).
        self.assertTrue(check(positions, Decimal("20")))

    def test_ignores_positions_outside_the_cohort(self):
        check = self._fake_bot(account_value=Decimal("200"), symbols={"AAA"})
        positions = [
            {"symbol": "NOTSCALP", "quantity": "1000", "cost_price": "50"},
        ]
        self.assertTrue(check(positions, Decimal("20")))

    def test_fails_open_when_account_value_is_unknown(self):
        check = self._fake_bot(account_value=None, symbols={"AAA"})
        self.assertTrue(check([], Decimal("1000")))

    def test_fails_open_when_account_value_is_non_positive(self):
        check = self._fake_bot(account_value=Decimal("0"), symbols={"AAA"})
        self.assertTrue(check([], Decimal("1000")))


class StrategyTuningTests(StrategyConfigMixin, unittest.TestCase):
    def test_vwap_gate_blocks_entry_below_session_vwap(self):
        strategy = TradingStrategy(self.config())
        strategy.update_stock_snapshot(
            {"symbol": "VWAPTEST", "volume": "1000", "price": "10"},
            Decimal("10"),
        )
        strategy.update_stock_snapshot(
            {"symbol": "VWAPTEST", "volume": "2000", "price": "12"},
            Decimal("12"),
        )
        self.assertEqual(strategy.vwap("VWAPTEST"), Decimal("12"))
        self.assertFalse(strategy.vwap_supports_entry("VWAPTEST", Decimal("9")))
        self.assertTrue(strategy.vwap_supports_entry("VWAPTEST", Decimal("12")))

    def test_vwap_gate_does_not_block_entry_without_data(self):
        strategy = TradingStrategy(self.config())
        self.assertTrue(strategy.vwap_supports_entry("UNSEEN", Decimal("5")))

    def test_sma_trend_gate_off_by_default_passes_regardless_of_data(self):
        strategy = TradingStrategy(self.config())
        strategy.sma_trend["BELOWTREND"] = Decimal("100")
        self.assertTrue(
            strategy.sma_trend_supports_entry("BELOWTREND", Decimal("50"))
        )

    def test_sma_trend_gate_blocks_price_below_the_daily_sma(self):
        config = self.config()
        config.sma_trend_filter_enabled = True
        strategy = TradingStrategy(config)
        strategy.sma_trend["TREND"] = Decimal("100")
        self.assertFalse(strategy.sma_trend_supports_entry("TREND", Decimal("99")))
        self.assertTrue(strategy.sma_trend_supports_entry("TREND", Decimal("100")))
        self.assertTrue(strategy.sma_trend_supports_entry("TREND", Decimal("101")))

    def test_sma_trend_gate_does_not_block_entry_without_data(self):
        config = self.config()
        config.sma_trend_filter_enabled = True
        strategy = TradingStrategy(config)
        self.assertTrue(strategy.sma_trend_supports_entry("UNSEEN", Decimal("5")))

    def test_trend_signal_fires_short_on_a_fresh_bearish_cross(self):
        strategy = TradingStrategy(self.config())
        key = "STOCK:TEST"
        uptrend = [10, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8]
        for price in uptrend:
            strategy.trend_signal(key, Decimal(str(price)))
        # 10.4 still confirms the ongoing uptrend (reenter_on_trend fires
        # here per the shared fixture's reenter_confirmation_polls=2,
        # unrelated to shorting) - the fresh bearish cross only fires once
        # the EMA spread actually flips negative, at 10.3.
        self.assertEqual(strategy.trend_signal(key, Decimal("10.4")), "BUY")
        self.assertEqual(strategy.trend_signal(key, Decimal("10.3")), "SHORT")

    def test_vwap_gate_short_direction_blocks_price_above_vwap(self):
        strategy = TradingStrategy(self.config())
        strategy.update_stock_snapshot(
            {"symbol": "SHORTVWAP", "volume": "1000", "price": "10"},
            Decimal("10"),
        )
        strategy.update_stock_snapshot(
            {"symbol": "SHORTVWAP", "volume": "2000", "price": "12"},
            Decimal("12"),
        )
        self.assertFalse(
            strategy.vwap_supports_entry("SHORTVWAP", Decimal("13"), "SHORT")
        )
        self.assertTrue(
            strategy.vwap_supports_entry("SHORTVWAP", Decimal("12"), "SHORT")
        )

    def test_extension_gate_short_direction_blocks_chasing_todays_low(self):
        strategy = TradingStrategy(self.config())
        strategy.metrics["DIPPED"] = {"low": 50.0}
        self.assertFalse(
            strategy.entry_extension_ok("DIPPED", Decimal("50.2"), direction="SHORT")
        )
        self.assertTrue(
            strategy.entry_extension_ok("DIPPED", Decimal("52.0"), direction="SHORT")
        )

    def test_tick_direction_short_requires_downticks(self):
        strategy = TradingStrategy(self.config())
        for price in ["10", "9.9", "9.8", "9.7"]:
            strategy.trend_signal("STOCK:DOWN", Decimal(price))
        self.assertTrue(strategy.tick_direction_ok("STOCK:DOWN", "SHORT"))
        for price in ["10", "10.1", "10.2", "10.3"]:
            strategy.trend_signal("STOCK:UP", Decimal(price))
        self.assertFalse(strategy.tick_direction_ok("STOCK:UP", "SHORT"))

    def test_stock_decision_opens_a_short_on_a_fresh_bearish_cross(self):
        config = self.config()
        config.short_selling_enabled = True
        # Tick-direction confirmation is exercised separately in
        # test_tick_direction_short_requires_downticks - disabled here so
        # this test isolates just the SHORT entry gate/sizing mechanics.
        config.tick_direction_enabled = False
        strategy = TradingStrategy(config)
        key = "STOCK:TEST"
        uptrend = [10, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8]
        for price in uptrend:
            strategy.stock_decision(key, Decimal(str(price)), 0, Decimal("0"))
        strategy.stock_decision(key, Decimal("10.4"), 0, Decimal("0"))
        decision = strategy.stock_decision(key, Decimal("10.3"), 0, Decimal("0"))
        self.assertEqual(decision.action, "SHORT")

    def test_stock_decision_short_signal_is_a_noop_when_disabled(self):
        config = self.config()
        config.short_selling_enabled = False
        strategy = TradingStrategy(config)
        key = "STOCK:TEST"
        uptrend = [10, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8]
        for price in uptrend:
            strategy.stock_decision(key, Decimal(str(price)), 0, Decimal("0"))
        strategy.stock_decision(key, Decimal("10.4"), 0, Decimal("0"))
        decision = strategy.stock_decision(key, Decimal("10.3"), 0, Decimal("0"))
        self.assertEqual(decision.action, "HOLD")

    def test_stock_decision_holds_when_cost_diverges_implausibly_from_price(self):
        """Regression test for a live incident: a held position (FPE) kept
        computing a PROFIT target 13-60% above the live market price and
        repeatedly submitting a limit order that could never fill, because
        a quote field the target derived from had drifted far from
        reality. The guard only needs to suppress that unreachable-target
        path - a price implausibly *above* cost, not below (see the
        sibling test confirming a stop still fires on a real, large
        decline - the guard must never block that).
        """
        strategy = TradingStrategy(self.config())
        key = "STOCK:FPE"
        # price (12) is 20% above cost (10) - without the guard this fires
        # PROFIT off a suspect gain (target maxes ~2.7% above cost here).
        decision = strategy.stock_decision(key, Decimal("12"), 1, Decimal("10"))
        self.assertEqual(decision.action, "HOLD")
        self.assertEqual(
            decision.reason, "cost basis diverges implausibly from live price"
        )

    def test_stock_decision_short_position_also_guards_against_implausible_cost(self):
        strategy = TradingStrategy(self.config())
        key = "STOCK:SHORTBAD"
        # price (8) is 20% below cost (10) - the mirror of the long case:
        # a suspiciously large paper gain for a short, not a loss.
        decision = strategy.stock_decision(key, Decimal("8"), -1, Decimal("10"))
        self.assertEqual(decision.action, "HOLD")
        self.assertEqual(
            decision.reason, "cost basis diverges implausibly from live price"
        )

    def test_stock_decision_stop_loss_still_fires_despite_implausible_cost_divergence(self):
        """Regression test for a live incident: a real, large decline
        (AZI, ~31% since entry - a genuinely volatile stock, not bad
        data: bid/ask were tight and consistent with the last-trade price)
        sat with no working stop at all, because the sanity guard used to
        run before the stop check too, not just before the profit target.
        A stop-loss must always be able to fire regardless of how
        implausible the divergence looks - failing to protect capital is
        worse than a hypothetical bad cost reading.
        """
        strategy = TradingStrategy(self.config())
        key = "STOCK:AZI"
        decision = strategy.stock_decision(key, Decimal("1.21"), 1, Decimal("1.75"))
        self.assertEqual(decision.action, "LOSS")

    def test_stock_decision_tolerates_a_normal_divergence_within_the_sanity_band(self):
        """A real, if unusually large, intraday move shouldn't trip the
        guard - only a divergence beyond what this strategy's own stop
        discipline would ever let survive should.
        """
        strategy = TradingStrategy(self.config())
        key = "STOCK:MOVED"
        # ~10% divergence - well under the 15% sanity band but still much
        # larger than a normal scalp - should reach real stop/target math,
        # not the sanity guard (the stop fires here since 10% down blows
        # through even a widened adaptive stop).
        decision = strategy.stock_decision(key, Decimal("90"), 1, Decimal("100"))
        self.assertEqual(decision.action, "LOSS")

    def test_stock_decision_short_position_stop_and_profit_are_mirrored(self):
        strategy = TradingStrategy(self.config())
        key = "STOCK:SHORTPOS"
        # Shorted at 100: a short profits as price falls, stops out as
        # price rises - the exact mirror of the long-side math.
        loss = strategy.stock_decision(key, Decimal("102"), -10, Decimal("100"))
        self.assertEqual(loss.action, "LOSS")

        profit = strategy.stock_decision(key, Decimal("90"), -10, Decimal("100"))
        self.assertEqual(profit.action, "PROFIT")

        hold = strategy.stock_decision(key, Decimal("99.9"), -10, Decimal("100"))
        self.assertEqual(hold.action, "HOLD")

    def test_stock_decision_buy_blocked_when_price_is_below_sma_trend(self):
        config = self.config()
        config.sma_trend_filter_enabled = True
        strategy = TradingStrategy(config)
        strategy.sma_trend["TREND"] = Decimal("100")
        key = "STOCK:TREND"
        downtrend = [10, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.2]
        for price in downtrend:
            strategy.stock_decision(key, Decimal(str(price)), 0, Decimal("0"))
        strategy.stock_decision(key, Decimal("9.6"), 0, Decimal("0"))
        decision = strategy.stock_decision(key, Decimal("9.7"), 0, Decimal("0"))
        self.assertEqual(decision.action, "HOLD")
        self.assertEqual(decision.reason, "price below the higher-timeframe SMA trend")

    def test_extension_gate_blocks_entry_right_at_todays_high(self):
        strategy = TradingStrategy(self.config())
        strategy.metrics["SPIKED"] = {"high": 100.0}
        self.assertFalse(strategy.entry_extension_ok("SPIKED", Decimal("99.5")))
        self.assertTrue(strategy.entry_extension_ok("SPIKED", Decimal("98.0")))

    def test_extension_gate_does_not_block_entry_without_data(self):
        strategy = TradingStrategy(self.config())
        self.assertTrue(strategy.entry_extension_ok("UNSEEN", Decimal("50")))

    def test_opening_grace_widens_extension_gate(self):
        strategy = TradingStrategy(self.config())
        strategy.metrics["SPIKED"] = {"high": 100.0}
        # Same price/high pair the plain gate rejects above - the grace
        # multiplier (2x -> 2% room instead of 1%) should let it through.
        self.assertFalse(strategy.entry_extension_ok("SPIKED", Decimal("99.5")))
        self.assertTrue(
            strategy.entry_extension_ok("SPIKED", Decimal("99.5"), True)
        )

    def test_opening_grace_widens_spread_gate(self):
        strategy = TradingStrategy(self.config())
        strategy.metrics["WIDE"] = {"spread_percent": 0.25}
        key = "STOCK:WIDE"
        self.assertFalse(strategy.entry_spread_ok(key))
        self.assertTrue(strategy.entry_spread_ok(key, True))

    def test_idle_cash_relaxation_widens_extension_gate(self):
        strategy = TradingStrategy(self.config())
        strategy.metrics["SPIKED"] = {"high": 100.0}
        self.assertFalse(strategy.entry_extension_ok("SPIKED", Decimal("99.5")))
        self.assertTrue(
            strategy.entry_extension_ok(
                "SPIKED", Decimal("99.5"), False, "BUY", Decimal("2")
            )
        )

    def test_idle_cash_relaxation_widens_spread_gate(self):
        strategy = TradingStrategy(self.config())
        strategy.metrics["WIDE"] = {"spread_percent": 0.25}
        key = "STOCK:WIDE"
        self.assertFalse(strategy.entry_spread_ok(key))
        self.assertTrue(
            strategy.entry_spread_ok(key, False, Decimal("2"))
        )

    def test_idle_cash_relaxation_and_opening_grace_take_the_larger_multiplier(self):
        """Both mechanisms widen the same gates for different reasons -
        whichever justifies more room this cycle should win, not average
        or stack multiplicatively.
        """
        strategy = TradingStrategy(self.config())
        strategy.metrics["WIDE"] = {"spread_percent": 0.25}
        key = "STOCK:WIDE"
        # opening_grace_multiplier=2 alone already covers a 0.25 spread
        # (0.15*2=0.30) - a smaller idle multiplier shouldn't override it.
        self.assertTrue(
            strategy.entry_spread_ok(key, True, Decimal("1.1"))
        )

    def test_idle_cash_relaxation_widens_vwap_band(self):
        strategy = TradingStrategy(self.config())
        strategy._update_vwap("TEST", Decimal("100"), 100.0)
        strategy._update_vwap("TEST", Decimal("100"), 200.0)
        # 0.5% below VWAP - outside the default 0.1% band, inside a 10x one.
        price = Decimal("99.5")
        self.assertFalse(strategy.vwap_supports_entry("TEST", price))
        self.assertTrue(
            strategy.vwap_supports_entry("TEST", price, "BUY", Decimal("10"))
        )

    def test_idle_cash_relaxation_lowers_tick_direction_veto(self):
        strategy = TradingStrategy(self.config())
        key = "STOCK:TICKY"
        for price in (10.0, 9.9, 9.8):
            strategy.tick_history[key].append(price)
        # All downticks - score is -1, fails the default (>=0) BUY veto.
        self.assertFalse(strategy.tick_direction_ok(key))
        self.assertTrue(
            strategy.tick_direction_ok(key, "BUY", Decimal("1.5"))
        )

    def test_adaptive_stop_percent_is_clamped_between_configured_bounds(self):
        strategy = TradingStrategy(self.config())
        strategy.metrics["CALM"] = {"range_ratio": 0.001}
        strategy.metrics["WILD"] = {"range_ratio": 0.10}
        self.assertEqual(strategy.adaptive_stop_percent("CALM"), Decimal("0.0015"))
        self.assertEqual(strategy.adaptive_stop_percent("WILD"), Decimal("0.02"))

    def test_reentry_requires_confirmation_polls_after_initial_crossover(self):
        strategy = TradingStrategy(self.config())
        key = "STOCK:REENTRY"
        downtrend = [10, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.2]
        for price in downtrend:
            strategy.trend_signal(key, Decimal(str(price)))

        # By this point the downtrend has already held for
        # reenter_confirmation_polls cycles, so this re-fires "SHORT" -
        # trend_streak's re-entry counter is shared symmetrically between
        # directions (see test_reentry_requires_confirmation_polls_for_a_
        # continuing_downtrend_too for a dedicated check of that).
        self.assertEqual(strategy.trend_signal(key, Decimal("9.6")), "SHORT")
        self.assertEqual(strategy.trend_signal(key, Decimal("9.7")), "BUY")
        # A fresh crossover fires instantly, but the very next poll of a
        # still-forming uptrend should not immediately re-fire.
        self.assertEqual(strategy.trend_signal(key, Decimal("9.9")), "HOLD")
        # Once the uptrend has held for the configured confirmation polls,
        # re-entry is allowed again.
        self.assertEqual(strategy.trend_signal(key, Decimal("10.2")), "BUY")

    def test_reentry_requires_confirmation_polls_for_a_continuing_downtrend_too(self):
        """SHORT's re-entry mechanism mirrors BUY's exactly - without it, a
        short entry needs VWAP/SMA-trend/extension/tick-direction to all
        align on the single exact tick of the fresh bearish cross, which
        in production essentially never happened. A persisting downtrend
        must get repeated chances, the same way a persisting uptrend
        already does for BUY.
        """
        strategy = TradingStrategy(self.config())
        key = "STOCK:SHORTREENTRY"
        uptrend = [10, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8]
        for price in uptrend:
            strategy.trend_signal(key, Decimal(str(price)))

        self.assertEqual(strategy.trend_signal(key, Decimal("10.4")), "BUY")
        self.assertEqual(strategy.trend_signal(key, Decimal("10.3")), "SHORT")
        # A fresh crossover fires instantly, but the very next poll of a
        # still-forming downtrend should not immediately re-fire.
        self.assertEqual(strategy.trend_signal(key, Decimal("10.1")), "HOLD")
        # Once the downtrend has held for the configured confirmation
        # polls, re-entry is allowed again.
        self.assertEqual(strategy.trend_signal(key, Decimal("9.8")), "SHORT")

    def test_tick_direction_score_ranges_from_all_downticks_to_all_upticks(self):
        strategy = TradingStrategy(self.config())
        self.assertEqual(strategy.tick_direction_score("STOCK:NODATA"), Decimal("0"))

        for price in ["10", "10.1", "10.2", "10.3"]:
            strategy.trend_signal("STOCK:UP", Decimal(price))
        self.assertEqual(strategy.tick_direction_score("STOCK:UP"), Decimal("1"))

        for price in ["10", "9.9", "9.8", "9.7"]:
            strategy.trend_signal("STOCK:DOWN", Decimal(price))
        self.assertEqual(strategy.tick_direction_score("STOCK:DOWN"), Decimal("-1"))

    def test_tick_direction_veto_blocks_an_otherwise_qualifying_ema_entry(self):
        """Real bid/ask depth isn't available from the quote feed, so tick
        direction (net upticks vs downticks in the recent tape) is the
        closest proxy for order-flow imbalance. An EMA crossover can fire
        right as a long downtrend just barely turns - the smoothed EMA
        already reads "up" while the last several individual prints are
        still dominated by the downtrend that preceded it. This must hold
        instead of chasing an entry the raw tape doesn't yet support.
        """
        strategy = TradingStrategy(self.config())
        key = "STOCK:REENTRY2"
        downtrend = [10, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.2]
        for price in downtrend:
            strategy.trend_signal(key, Decimal(str(price)))
        strategy.trend_signal(key, Decimal("9.6"))

        decision = strategy.stock_decision(key, Decimal("9.7"), 0, Decimal("0"), None)
        self.assertEqual(decision.action, "HOLD")
        self.assertIn("recent ticks", decision.reason)

    def test_tick_direction_disabled_lets_the_ema_entry_through_regardless(self):
        config = self.config()
        config.tick_direction_enabled = False
        strategy = TradingStrategy(config)
        key = "STOCK:REENTRY3"
        downtrend = [10, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.2]
        for price in downtrend:
            strategy.trend_signal(key, Decimal(str(price)))
        strategy.trend_signal(key, Decimal("9.6"))

        decision = strategy.stock_decision(key, Decimal("9.7"), 0, Decimal("0"), None)
        self.assertEqual(decision.action, "BUY")

    def test_recurring_crossovers_boost_priority_score(self):
        strategy = TradingStrategy(self.config())
        choppy = [10, 9.8, 9.6, 9.8, 10, 9.8, 9.6, 9.8, 10, 9.8, 9.6, 9.8, 10, 9.8, 9.6]
        for price in choppy:
            strategy.trend_signal("STOCK:CHOP", Decimal(str(price)))
        smooth = [Decimal("10") + Decimal(str(i)) * Decimal("0.1") for i in range(15)]
        for price in smooth:
            strategy.trend_signal("STOCK:SMOOTH", price)

        self.assertGreater(strategy.crossover_counts["CHOP"], strategy.crossover_counts["SMOOTH"])

        strategy.activity["CHOP"] = 5.0
        strategy.activity["SMOOTH"] = 5.0
        self.assertGreater(
            strategy.priority_score("CHOP", None),
            strategy.priority_score("SMOOTH", None),
        )

    def test_clear_market_state_resets_crossover_counts(self):
        strategy = TradingStrategy(self.config())
        for price in [10, 9.8, 9.6, 9.8, 10, 9.8, 9.6, 9.8, 10, 9.8]:
            strategy.trend_signal("STOCK:CHOP", Decimal(str(price)))
        self.assertGreater(strategy.crossover_counts["CHOP"], 0)
        strategy.clear_market_state()
        self.assertEqual(strategy.crossover_counts["CHOP"], 0)

    def test_clear_market_state_resets_tick_history(self):
        strategy = TradingStrategy(self.config())
        for price in [10, 9.9, 9.8, 9.7]:
            strategy.trend_signal("STOCK:CHOP", Decimal(str(price)))
        self.assertNotEqual(strategy.tick_direction_score("STOCK:CHOP"), Decimal("0"))
        strategy.clear_market_state()
        self.assertEqual(strategy.tick_direction_score("STOCK:CHOP"), Decimal("0"))

    def test_stop_and_target_scale_with_adaptive_stop_percent(self):
        strategy = TradingStrategy(self.config())
        strategy.metrics["WILD"] = {"range_ratio": 0.10}
        decision = strategy.stock_decision(
            "STOCK:WILD",
            Decimal("97.9"),
            10,
            Decimal("100"),
            None,
        )
        self.assertEqual(decision.action, "LOSS")

    def test_fractional_position_targets_a_smaller_move_than_whole_share(self):
        """A fractional position can only be exited during core hours at
        all, so it should cycle capital quickly (many trades/hour) rather
        than sit waiting for the same larger move a whole-share position
        can hold toward - a fractional position's target is just the flat
        cost-recovery floor (stock_min_net_profit_percent +
        stock_estimated_round_trip_cost_percent), not also scaled by the
        adaptive stop like whole-share's is, so at the same elevated
        volatility the fractional target is reached first.
        """
        strategy = TradingStrategy(self.config())
        strategy.metrics["WILD"] = {"range_ratio": 0.10}
        price = Decimal("101.80")

        whole_share = strategy.stock_decision(
            "STOCK:WILD", price, 10, Decimal("100"), None
        )
        self.assertEqual(whole_share.action, "HOLD")

        fractional = strategy.stock_decision(
            "STOCK:WILD", price, Decimal("0.5"), Decimal("100"), None
        )
        self.assertEqual(fractional.action, "PROFIT")

    def test_fractional_target_ignores_the_adaptive_stop_scaling(self):
        """Regression test: a fractional target that also scales with
        stop_percent * FRACTIONAL_TARGET_STOP_MULTIPLE (like whole-share
        does) combines badly with a tiny fractional quantity's inflated
        fee-per-share (SELL_FEE_DOLLARS spread over well under 1 share) -
        together they demanded far more absolute price appreciation than
        "capture the profit quickly" intends. Confirmed against a real
        live position (MSFT, 0.0108 shares, cost $493.23): the old target
        math required ~$498.63 before firing; the position was already
        genuinely profitable at ~$496.93 and should have sold. Using just
        the flat cost-recovery floor for a fractional position's target
        (skipping the stop-scaled term entirely) fixes it.
        """
        strategy = TradingStrategy(self.config())
        strategy.metrics["MSFT"] = {"range_ratio": 0.10}  # elevated stop_percent
        decision = strategy.stock_decision(
            "STOCK:MSFT",
            Decimal("496.9337037037037037037037037"),
            Decimal("0.0108"),
            Decimal("493.23"),
            None,
        )
        self.assertEqual(decision.action, "PROFIT")

    def test_option_decision_cuts_loss_before_it_reaches_zero(self):
        strategy = TradingStrategy(self.config())
        decision = strategy.option_decision(
            Decimal("0.40"),
            5,
            Decimal("1.00"),
            10,
        )
        self.assertEqual(decision.action, "LOSS")

    def test_option_decision_holds_above_stop_and_below_target(self):
        strategy = TradingStrategy(self.config())
        decision = strategy.option_decision(
            Decimal("0.90"),
            5,
            Decimal("1.00"),
            10,
        )
        self.assertEqual(decision.action, "HOLD")

    def test_option_decision_forces_exit_inside_the_dte_window(self):
        strategy = TradingStrategy(self.config())
        profit_case = strategy.option_decision(
            Decimal("1.10"),
            5,
            Decimal("1.00"),
            2,
        )
        self.assertEqual(profit_case.action, "PROFIT")
        self.assertIn("time decay exit", profit_case.reason)

        loss_case = strategy.option_decision(
            Decimal("0.90"),
            5,
            Decimal("1.00"),
            2,
        )
        self.assertEqual(loss_case.action, "LOSS")
        self.assertIn("time decay exit", loss_case.reason)

    def test_option_decision_no_position_holds(self):
        strategy = TradingStrategy(self.config())
        decision = strategy.option_decision(Decimal("1.00"), 0, Decimal("0"), 10)
        self.assertEqual(decision.action, "HOLD")

    def test_option_direction_signal_fires_call_on_a_fresh_bullish_cross(self):
        strategy = TradingStrategy(self.config())
        key = "OPTU:TEST"
        downtrend = [10, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.2]
        for price in downtrend:
            strategy.option_direction_signal(key, Decimal(str(price)))
        self.assertEqual(strategy.option_direction_signal(key, Decimal("9.6")), "HOLD")
        self.assertEqual(strategy.option_direction_signal(key, Decimal("9.7")), "CALL")

    def test_option_direction_signal_fires_put_on_a_fresh_bearish_cross(self):
        strategy = TradingStrategy(self.config())
        key = "OPTU:TEST"
        uptrend = [10, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8]
        for price in uptrend:
            strategy.option_direction_signal(key, Decimal(str(price)))
        self.assertEqual(strategy.option_direction_signal(key, Decimal("10.4")), "HOLD")
        self.assertEqual(strategy.option_direction_signal(key, Decimal("10.3")), "PUT")

    def test_option_entry_confirmed_requires_direction(self):
        strategy = TradingStrategy(self.config())
        self.assertFalse(strategy.option_entry_confirmed("HOLD", None, None))
        self.assertTrue(strategy.option_entry_confirmed("CALL", None, None))
        self.assertTrue(strategy.option_entry_confirmed("PUT", None, None))

    def test_option_entry_confirmed_checks_tick_alignment(self):
        strategy = TradingStrategy(self.config())
        self.assertTrue(strategy.option_entry_confirmed("CALL", Decimal("0.5"), None))
        self.assertFalse(strategy.option_entry_confirmed("CALL", Decimal("-0.5"), None))
        self.assertTrue(strategy.option_entry_confirmed("PUT", Decimal("-0.5"), None))
        self.assertFalse(strategy.option_entry_confirmed("PUT", Decimal("0.5"), None))

    def test_option_entry_confirmed_checks_obi_alignment(self):
        strategy = TradingStrategy(self.config())
        self.assertTrue(strategy.option_entry_confirmed("CALL", None, Decimal("0.70")))
        self.assertFalse(strategy.option_entry_confirmed("CALL", None, Decimal("0.30")))
        self.assertTrue(strategy.option_entry_confirmed("PUT", None, Decimal("0.30")))
        self.assertFalse(strategy.option_entry_confirmed("PUT", None, Decimal("0.70")))

    def test_option_delta_ok_rejects_outside_the_directional_band(self):
        self.assertTrue(TradingStrategy.option_delta_ok(None))
        self.assertTrue(TradingStrategy.option_delta_ok(Decimal("0.45")))
        self.assertTrue(TradingStrategy.option_delta_ok(Decimal("-0.45")))
        self.assertFalse(TradingStrategy.option_delta_ok(Decimal("0.05")))
        self.assertFalse(TradingStrategy.option_delta_ok(Decimal("0.95")))

    def test_option_iv_percentile_ok_passes_with_sparse_history(self):
        history = deque([Decimal("0.3")] * 3, maxlen=30)
        self.assertTrue(
            TradingStrategy.option_iv_percentile_ok(history, Decimal("0.9"))
        )

    def test_option_iv_percentile_ok_rejects_the_priciest_tail(self):
        history = deque(
            [Decimal(str(0.20 + 0.01 * i)) for i in range(20)], maxlen=30
        )
        self.assertFalse(
            TradingStrategy.option_iv_percentile_ok(history, Decimal("0.50"))
        )
        self.assertTrue(
            TradingStrategy.option_iv_percentile_ok(history, Decimal("0.20"))
        )

    def test_option_market_regime_ok_rejects_a_vixy_spike(self):
        history = deque([Decimal(str(15 + i)) for i in range(20)], maxlen=30)
        self.assertFalse(
            TradingStrategy.option_market_regime_ok(history, Decimal("40"))
        )
        self.assertTrue(
            TradingStrategy.option_market_regime_ok(history, Decimal("15"))
        )
        self.assertTrue(TradingStrategy.option_market_regime_ok(history, None))

    def test_option_order_quantity_applies_the_capital_fraction_cap(self):
        strategy = TradingStrategy(self.config())
        # $1 premium -> $100/contract. 5% of $500 buying power caps at 0
        # contracts even though option_quantity/max_order_notional would
        # otherwise allow one.
        quantity, contract_cost = strategy.option_order_quantity(
            Decimal("1.00"), Decimal("500")
        )
        self.assertEqual(contract_cost, Decimal("100"))
        self.assertEqual(quantity, 0)

        quantity, _ = strategy.option_order_quantity(
            Decimal("1.00"), Decimal("5000")
        )
        self.assertEqual(quantity, 1)

class StopLossEscalationTests(unittest.TestCase):
    def test_escalated_stop_bypasses_cooldown_after_cancel(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(trade_cooldown_seconds=Decimal("30")),
            last_trade={"STOCK:X": 0.0},
            pending_stock_exits=set(),
            stop_loss_escalated=set(),
        )
        fake_bot.cooldown_ready = AutoTrader.cooldown_ready.__get__(fake_bot)
        ready = AutoTrader.stop_ready_to_submit.__get__(fake_bot)
        # Not yet escalated and cooldown hasn't elapsed: must wait.
        with unittest.mock.patch("time.monotonic", return_value=10.0):
            self.assertFalse(ready("STOCK:X", "X"))
        # Escalated: resubmit immediately even though the cooldown clock
        # (timed from the original, now-cancelled order) hasn't elapsed.
        fake_bot.stop_loss_escalated.add("X")
        with unittest.mock.patch("time.monotonic", return_value=10.0):
            self.assertTrue(ready("STOCK:X", "X"))

    def test_record_trade_marks_last_exit_only_for_exit_actions(self):
        """STOCK_REENTRY_COOLDOWN_SECONDS gates the next BUY off
        last_exit_at - that timestamp must only be set on an actual exit
        (PROFIT/STOP/MANUAL_SELL), never on a BUY, or an entry would look
        like a fresh exit and the cooldown would never clear correctly.
        """
        from collections import defaultdict, deque

        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            last_trade={},
            last_exit_at={},
            trade_times=defaultdict(deque),
            working_orders={},
            status=SimpleNamespace(record_trade=lambda *a, **k: None),
            position_opened_at={},
            symbol_pnl_history=defaultdict(deque),
            consecutive_exit_failures=defaultdict(int),
            submitted_order_ids_today=set(),
        )
        record_trade = AutoTrader.record_trade.__get__(fake_bot)

        record_trade("STOCK:X", "order-1", "BUY")
        self.assertNotIn("STOCK:X", fake_bot.last_exit_at)

        record_trade(
            "STOCK:X", "order-2", "PROFIT", Decimal("10.00"), pnl=Decimal("1")
        )
        self.assertIn("STOCK:X", fake_bot.last_exit_at)

    def test_reentry_cooldown_blocks_immediate_rebuy_after_an_exit(self):
        """A stock that just closed shouldn't immediately pull the bot back
        in on the next favorable-looking poll - it must wait out
        STOCK_REENTRY_COOLDOWN_SECONDS from the last exit first. A symbol
        that has never had a position closed has nothing to wait out.
        """
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(stock_reentry_cooldown_seconds=Decimal("600")),
            last_exit_at={"STOCK:X": 1000.0},
        )
        ready = AutoTrader.reentry_cooldown_ready.__get__(fake_bot)
        with unittest.mock.patch("time.monotonic", return_value=1100.0):
            self.assertFalse(ready("STOCK:X"))  # only 100s since the exit
        with unittest.mock.patch("time.monotonic", return_value=1601.0):
            self.assertTrue(ready("STOCK:X"))  # past the 600s cooldown
        self.assertTrue(ready("STOCK:Y"))  # never exited - nothing to wait out

    def test_pending_exit_always_blocks_regardless_of_escalation(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(trade_cooldown_seconds=Decimal("30")),
            last_trade={},
            pending_stock_exits={"X"},
            stop_loss_escalated={"X"},
        )
        ready = AutoTrader.stop_ready_to_submit.__get__(fake_bot)
        self.assertFalse(ready("STOCK:X", "X"))

    def test_escalation_also_catches_a_stalled_profit_order(self):
        """Regression test: a PROFIT limit order that never fills (target
        price the market doesn't actually reach) must escalate the same
        way a stalled STOP does - otherwise it cancels on the generic order
        timeout and resubmits at the identical unreachable price forever,
        never realizing the gain.
        """
        from webull_bot.bot import AutoTrader

        cancelled = []
        fake_bot = SimpleNamespace(
            config=SimpleNamespace(stop_loss_escalate_seconds=15),
            api=SimpleNamespace(cancel=lambda order_id: cancelled.append(order_id)),
            status=SimpleNamespace(discard_trade=lambda order_id: None),
            stop_exit_submitted={"ASHR": 0.0},
            pending_stock_exits={"ASHR"},
            stop_loss_escalated=set(),
            consecutive_exit_failures=defaultdict(int),
            working_orders={
                "order-1": {
                    "submitted_at": 0.0,
                    "key": "STOCK:ASHR",
                    "action": "PROFIT",
                    "cancel_requested_at": None,
                    "pnl": None,
                }
            },
        )
        fake_bot.reverse_phantom_exit = AutoTrader.reverse_phantom_exit.__get__(fake_bot)
        fake_bot._note_exit_failure = AutoTrader._note_exit_failure.__get__(fake_bot)
        escalate = AutoTrader.escalate_stalled_stop_losses.__get__(fake_bot)

        with unittest.mock.patch("time.monotonic", return_value=20.0):
            escalate()

        self.assertEqual(cancelled, ["order-1"])
        self.assertIn("ASHR", fake_bot.stop_loss_escalated)
        self.assertNotIn("ASHR", fake_bot.pending_stock_exits)
        self.assertNotIn("ASHR", fake_bot.stop_exit_submitted)
        self.assertEqual(fake_bot.consecutive_exit_failures["ASHR"], 1)


class StopLossConfirmationTests(unittest.TestCase):
    """Live complaint: a position dips through its stop on a single noisy
    tick (the bot polls as fast as every 0.25s) and gets sold at the exact
    worst moment, then recovers. stop_loss_confirmed requires the breach to
    hold continuously for STOP_LOSS_CONFIRMATION_SECONDS before AutoTrader
    will actually submit the exit - see trade_stocks' LOSS branch.
    """

    def _fake_bot(self, **overrides):
        from webull_bot.bot import AutoTrader

        defaults = dict(
            config=SimpleNamespace(
                stop_loss_confirmation_enabled=True,
                stop_loss_confirmation_seconds=Decimal("2"),
            ),
            stop_condition_since={},
            stop_loss_escalated=set(),
            volatility_scalp_symbols=set(),
            volatility_scalp_positions=set(),
            strategy=SimpleNamespace(is_volatility_scalp_eligible=lambda symbol: False),
        )
        defaults.update(overrides)
        fake_bot = SimpleNamespace(**defaults)
        fake_bot.stop_loss_confirmed = AutoTrader.stop_loss_confirmed.__get__(fake_bot)
        return fake_bot

    def test_volatility_scalp_cohort_skips_the_confirmation_wait(self):
        """Live incident: MYND (a volatility-scalp-eligible symbol) sat
        11%+ past its stop for many minutes because price kept ticking
        back above the stop line often enough that the 2s confirmation
        window never completed - the same choppiness that made it
        eligible in the first place also defeated the wick-filtering
        confirmation. Any eligible symbol's positions must stop out on
        the first breach, no wait - condensed onto eligibility alone,
        not the narrower curated cohort list.
        """
        fake_bot = self._fake_bot(
            strategy=SimpleNamespace(
                is_volatility_scalp_eligible=lambda symbol: symbol == "MYND"
            )
        )
        # No stop_condition_since entry at all - would normally be
        # unconfirmed (see test_symbol_never_seen_in_breach_is_not_
        # confirmed), but the cohort bypass short-circuits before that
        # check ever runs.
        self.assertTrue(fake_bot.stop_loss_confirmed("MYND"))

    def test_unconfirmed_breach_is_not_yet_actionable(self):
        fake_bot = self._fake_bot(stop_condition_since={"ASHR": 10.0})
        with unittest.mock.patch("time.monotonic", return_value=11.0):
            self.assertFalse(fake_bot.stop_loss_confirmed("ASHR"))

    def test_breach_confirmed_once_it_holds_the_full_window(self):
        fake_bot = self._fake_bot(stop_condition_since={"ASHR": 10.0})
        with unittest.mock.patch("time.monotonic", return_value=12.0):
            self.assertTrue(fake_bot.stop_loss_confirmed("ASHR"))

    def test_symbol_never_seen_in_breach_is_not_confirmed(self):
        fake_bot = self._fake_bot()
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            self.assertFalse(fake_bot.stop_loss_confirmed("ASHR"))

    def test_disabled_skips_the_wait_entirely(self):
        fake_bot = self._fake_bot(
            config=SimpleNamespace(
                stop_loss_confirmation_enabled=False,
                stop_loss_confirmation_seconds=Decimal("2"),
            ),
        )
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            self.assertTrue(fake_bot.stop_loss_confirmed("ASHR"))

    def test_already_escalated_skips_the_wait(self):
        # An escalated stop was already confirmed once, before its first
        # (now-cancelled) submission - re-requiring a fresh dwell here
        # would just leave it unprotected for longer with no benefit.
        fake_bot = self._fake_bot(stop_loss_escalated={"ASHR"})
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            self.assertTrue(fake_bot.stop_loss_confirmed("ASHR"))


class RepriceRestingExitsTests(unittest.TestCase):
    def test_reprice_cancels_and_replaces_at_new_ask_without_recording_pnl_again(self):
        """The continuous re-quote loop must cancel + resubmit the resting
        exit directly at the fresh ask, while leaving stop_exit_submitted's
        original timestamp, pending_stock_exits membership, and realized PnL
        completely untouched - record_trade/record_realized_exit must only
        ever fire once, at the original PROFIT/LOSS submission, never again
        here (see the module-level constraint this guards against: double-
        counting realized P&L for a single logical exit).
        """
        from webull_bot.bot import AutoTrader

        cancelled = []
        placed = []
        quote = {"symbol": "ASHR", "bid": "34.18", "ask": "34.20", "price": "34.19"}

        class FakeApi:
            @staticmethod
            def stock_quote(symbol):
                return quote

            @staticmethod
            def stock_quotes_resilient(symbols, category):
                return ([quote for _ in symbols], set())

            @staticmethod
            def quote_ask(q):
                return Decimal(str(q["ask"]))

            @staticmethod
            def stock_position(symbol, positions):
                for item in positions:
                    if item.get("symbol") == symbol:
                        return (
                            Decimal(str(item.get("quantity", "0"))),
                            Decimal(str(item.get("cost_price", "0"))),
                        )
                return Decimal("0"), Decimal("0")

            @staticmethod
            def cancel(order_id):
                cancelled.append(order_id)

            @staticmethod
            def place_stock(symbol, side, quantity, limit_price=None, fractional=False):
                placed.append((symbol, side, quantity, limit_price))
                return "order-2"

        rekeyed = []
        fake_bot = SimpleNamespace(
            config=SimpleNamespace(order_monitor_seconds=Decimal("5")),
            api=FakeApi(),
            status=SimpleNamespace(
                rekey_trade=lambda old, new: rekeyed.append((old, new))
            ),
            last_reprice=0.0,
            strategy=SimpleNamespace(is_volatility_scalp_eligible=lambda symbol: False),
            volatility_scalp_symbols=set(),
            volatility_scalp_positions=set(),
            stop_loss_escalated=set(),
            pending_stock_exits={"ASHR"},
            stop_exit_submitted={"ASHR": 12345.0},
            daily_realized_pnl=Decimal("0"),
            daily_realized_loss=Decimal("0"),
            is_fractional_quantity=AutoTrader.is_fractional_quantity,
            stock_categories={},
            working_orders={
                "order-1": {
                    "submitted_at": 0.0,
                    "key": "STOCK:ASHR",
                    "action": "PROFIT",
                    "cancel_requested_at": None,
                    "limit_price": Decimal("34.00"),
                }
            },
        )
        fake_bot._batched_quotes = AutoTrader._batched_quotes.__get__(fake_bot)
        reprice = AutoTrader.reprice_resting_exits.__get__(fake_bot)

        positions = [
            {
                "instrument_type": "EQUITY",
                "symbol": "ASHR",
                "quantity": "1",
                "cost_price": "30.00",
            }
        ]
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice(positions)

        self.assertEqual(cancelled, ["order-1"])
        self.assertEqual(len(placed), 1)
        self.assertEqual(placed[0], ("ASHR", "SELL", Decimal("1"), Decimal("34.20")))
        self.assertNotIn("order-1", fake_bot.working_orders)
        self.assertIn("order-2", fake_bot.working_orders)
        new_order = fake_bot.working_orders["order-2"]
        self.assertEqual(new_order["key"], "STOCK:ASHR")
        self.assertEqual(new_order["action"], "PROFIT")
        self.assertEqual(new_order["limit_price"], Decimal("34.20"))
        # Original stop-loss-escalation timestamp, pending-exit membership,
        # and realized PnL must all survive untouched - only the very first
        # PROFIT/LOSS submission is allowed to move any of these.
        self.assertEqual(fake_bot.stop_exit_submitted["ASHR"], 12345.0)
        self.assertIn("ASHR", fake_bot.pending_stock_exits)
        self.assertEqual(fake_bot.daily_realized_pnl, Decimal("0"))
        # Regression coverage for a live incident: the dashboard's trade-
        # log entry must follow the order to its new id, or a later
        # cancellation can't find it to discard - see StatusWriter.
        # rekey_trade.
        self.assertEqual(rekeyed, [("order-1", "order-2")])

    def test_reprice_skips_when_ask_unchanged(self):
        from webull_bot.bot import AutoTrader

        calls = []
        quote = {"symbol": "ASHR", "bid": "33.98", "ask": "34.00", "price": "33.99"}

        class FakeApi:
            @staticmethod
            def stock_quote(symbol):
                return quote

            @staticmethod
            def stock_quotes_resilient(symbols, category):
                return ([quote for _ in symbols], set())

            @staticmethod
            def quote_ask(q):
                return Decimal(str(q["ask"]))

            @staticmethod
            def stock_position(symbol, positions):
                return Decimal("1"), Decimal("30.00")

            @staticmethod
            def cancel(order_id):
                calls.append(order_id)

            @staticmethod
            def place_stock(*args, **kwargs):
                calls.append("placed")
                return "order-2"

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(order_monitor_seconds=Decimal("5")),
            api=FakeApi(),
            last_reprice=0.0,
            strategy=SimpleNamespace(is_volatility_scalp_eligible=lambda symbol: False),
            volatility_scalp_symbols=set(),
            volatility_scalp_positions=set(),
            stop_loss_escalated=set(),
            pending_stock_exits={"ASHR"},
            stop_exit_submitted={"ASHR": 5.0},
            daily_realized_pnl=Decimal("0"),
            daily_realized_loss=Decimal("0"),
            stock_categories={},
            working_orders={
                "order-1": {
                    "submitted_at": 0.0,
                    "key": "STOCK:ASHR",
                    "action": "PROFIT",
                    "cancel_requested_at": None,
                    "limit_price": Decimal("34.00"),
                }
            },
        )
        fake_bot._batched_quotes = AutoTrader._batched_quotes.__get__(fake_bot)
        reprice = AutoTrader.reprice_resting_exits.__get__(fake_bot)

        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice([{"instrument_type": "EQUITY", "symbol": "ASHR", "quantity": "1"}])

        self.assertEqual(calls, [])
        self.assertIn("order-1", fake_bot.working_orders)

    def test_reprice_leaves_escalated_symbols_to_the_normal_path(self):
        from webull_bot.bot import AutoTrader

        calls = []

        class FakeApi:
            @staticmethod
            def stock_quote(symbol):
                raise AssertionError("must not fetch a quote for an escalated symbol")

            @staticmethod
            def cancel(order_id):
                calls.append(order_id)

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(order_monitor_seconds=Decimal("5")),
            api=FakeApi(),
            last_reprice=0.0,
            strategy=SimpleNamespace(is_volatility_scalp_eligible=lambda symbol: False),
            volatility_scalp_symbols=set(),
            volatility_scalp_positions=set(),
            stop_loss_escalated={"ASHR"},
            pending_stock_exits=set(),
            stop_exit_submitted={},
            daily_realized_pnl=Decimal("0"),
            daily_realized_loss=Decimal("0"),
            working_orders={
                "order-1": {
                    "submitted_at": 0.0,
                    "key": "STOCK:ASHR",
                    "action": "PROFIT",
                    "cancel_requested_at": None,
                    "limit_price": Decimal("34.00"),
                }
            },
        )
        reprice = AutoTrader.reprice_resting_exits.__get__(fake_bot)

        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice([])

        self.assertEqual(calls, [])
        self.assertIn("order-1", fake_bot.working_orders)

    def test_reprice_never_touches_stop_loss_orders(self):
        """A stop-loss must never be repriced to chase the ask - it needs
        to fill fast to cap a loss, not rest above a possibly-falling
        market hoping for a better price. Only escalate_stalled_stop_losses
        should ever move a stop, and only towards a guaranteed-fill price.
        """
        from webull_bot.bot import AutoTrader

        calls = []

        class FakeApi:
            @staticmethod
            def stock_quote(symbol):
                raise AssertionError("must not fetch a quote for a STOP order")

            @staticmethod
            def cancel(order_id):
                calls.append(order_id)

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(order_monitor_seconds=Decimal("5")),
            api=FakeApi(),
            last_reprice=0.0,
            strategy=SimpleNamespace(is_volatility_scalp_eligible=lambda symbol: False),
            volatility_scalp_symbols=set(),
            volatility_scalp_positions=set(),
            stop_loss_escalated=set(),
            pending_stock_exits={"ASHR"},
            stop_exit_submitted={"ASHR": 0.0},
            daily_realized_pnl=Decimal("0"),
            daily_realized_loss=Decimal("0"),
            working_orders={
                "order-1": {
                    "submitted_at": 0.0,
                    "key": "STOCK:ASHR",
                    "action": "STOP",
                    "cancel_requested_at": None,
                    "limit_price": Decimal("34.00"),
                }
            },
        )
        reprice = AutoTrader.reprice_resting_exits.__get__(fake_bot)

        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice([{"instrument_type": "EQUITY", "symbol": "ASHR", "quantity": "1"}])

        self.assertEqual(calls, [])
        self.assertIn("order-1", fake_bot.working_orders)

    def test_reprice_never_chases_the_ask_below_entry_cost(self):
        """If the ask has fallen below the position's own entry cost since
        the resting PROFIT order was placed, repricing to that ask would
        turn a profit-take into a guaranteed loss. Leave the existing
        (already validly-priced) order resting instead.
        """
        from webull_bot.bot import AutoTrader

        cancelled = []
        placed = []
        quote = {"symbol": "ASHR", "bid": "29.90", "ask": "29.95", "price": "29.95"}

        class FakeApi:
            @staticmethod
            def stock_quote(symbol):
                return quote

            @staticmethod
            def stock_quotes_resilient(symbols, category):
                return ([quote for _ in symbols], set())

            @staticmethod
            def quote_ask(q):
                return Decimal(str(q["ask"]))

            @staticmethod
            def stock_position(symbol, positions):
                for item in positions:
                    if item.get("symbol") == symbol:
                        return (
                            Decimal(str(item.get("quantity", "0"))),
                            Decimal(str(item.get("cost_price", "0"))),
                        )
                return Decimal("0"), Decimal("0")

            @staticmethod
            def cancel(order_id):
                cancelled.append(order_id)

            @staticmethod
            def place_stock(*args, **kwargs):
                placed.append((args, kwargs))
                return "order-2"

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(order_monitor_seconds=Decimal("5")),
            api=FakeApi(),
            last_reprice=0.0,
            strategy=SimpleNamespace(is_volatility_scalp_eligible=lambda symbol: False),
            volatility_scalp_symbols=set(),
            volatility_scalp_positions=set(),
            stop_loss_escalated=set(),
            pending_stock_exits={"ASHR"},
            stop_exit_submitted={"ASHR": 12345.0},
            daily_realized_pnl=Decimal("0"),
            daily_realized_loss=Decimal("0"),
            is_fractional_quantity=AutoTrader.is_fractional_quantity,
            stock_categories={},
            working_orders={
                "order-1": {
                    "submitted_at": 0.0,
                    "key": "STOCK:ASHR",
                    "action": "PROFIT",
                    "cancel_requested_at": None,
                    "limit_price": Decimal("30.20"),
                }
            },
        )
        fake_bot._batched_quotes = AutoTrader._batched_quotes.__get__(fake_bot)
        reprice = AutoTrader.reprice_resting_exits.__get__(fake_bot)

        # Entry cost (30.00) is above the current ask (29.95) - the stock
        # dropped after entry.
        positions = [
            {
                "instrument_type": "EQUITY",
                "symbol": "ASHR",
                "quantity": "1",
                "cost_price": "30.00",
            }
        ]
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice(positions)

        self.assertEqual(cancelled, [])
        self.assertEqual(placed, [])
        self.assertIn("order-1", fake_bot.working_orders)
        self.assertEqual(
            fake_bot.working_orders["order-1"]["limit_price"], Decimal("30.20")
        )


class VolatilityScalpRepriceTests(unittest.TestCase):
    """reprice_volatility_scalp_exits - the "cent by cent" active
    repricer, on its own faster VOLATILITY_SCALP_REPRICE_SECONDS cadence,
    scoped only to symbols currently volatility-scalp eligible.
    """

    @staticmethod
    def _fake_bot(
        eligible_symbols,
        working_orders,
        positions_cost_by_symbol,
        quote_by_symbol=None,
    ):
        """quote_by_symbol maps symbol -> (bid, ask) as strings; defaults
        to a tight bid=10.00/ask=10.05 spread on cost=9.50 (comfortably
        clears any floor) unless a test overrides it.
        """
        from webull_bot.bot import AutoTrader
        from webull_bot.webull_api import WebullAPI

        cancelled = []
        placed = []
        quote_by_symbol = quote_by_symbol or {}

        class FakeApi:
            @staticmethod
            def stock_quote(symbol):
                bid, ask = quote_by_symbol.get(symbol, ("10.00", "10.05"))
                return {"symbol": symbol, "bid": bid, "ask": ask}

            @staticmethod
            def stock_quotes_resilient(symbols, category):
                return ([FakeApi.stock_quote(s) for s in symbols], set())

            @staticmethod
            def quote_bid(q):
                return Decimal(str(q["bid"])) if q.get("bid") is not None else None

            @staticmethod
            def quote_ask(q):
                return Decimal(str(q["ask"])) if q.get("ask") is not None else None

            price_tick_size = staticmethod(WebullAPI.price_tick_size)

            @staticmethod
            def stock_position(symbol, positions):
                cost = positions_cost_by_symbol.get(symbol)
                if cost is None:
                    return Decimal("0"), Decimal("0")
                return Decimal("1"), cost

            @staticmethod
            def cancel(order_id):
                cancelled.append(order_id)

            @staticmethod
            def place_stock(symbol, side, quantity, limit_price=None):
                placed.append((symbol, side, quantity, limit_price))
                return "order-new"

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                volatility_scalp_reprice_seconds=Decimal("1"),
                sell_fee_dollars=Decimal("0"),
                volatility_scalp_target_percent=Decimal("0.005"),
                volatility_scalp_max_exit_spread_percent=Decimal("8"),
            ),
            api=FakeApi(),
            status=SimpleNamespace(rekey_trade=lambda old, new: None),
            last_volatility_reprice=0.0,
            strategy=SimpleNamespace(
                is_volatility_scalp_eligible=lambda symbol: symbol in eligible_symbols
            ),
            volatility_scalp_symbols=set(eligible_symbols),
            volatility_scalp_positions=set(),
            stop_loss_escalated=set(),
            is_fractional_quantity=AutoTrader.is_fractional_quantity,
            stock_categories={},
            working_orders=working_orders,
        )
        fake_bot._stall_exit_price = AutoTrader._stall_exit_price.__get__(fake_bot)
        fake_bot._batched_quotes = AutoTrader._batched_quotes.__get__(fake_bot)
        return fake_bot, cancelled, placed

    def test_reprices_toward_a_new_fillable_price_for_an_eligible_symbol(self):
        from webull_bot.bot import AutoTrader

        working_orders = {
            "order-1": {
                "submitted_at": 0.0,
                "key": "STOCK:HOWL",
                "action": "PROFIT",
                "cancel_requested_at": None,
                "limit_price": Decimal("9.90"),
            }
        }
        # cost=9.50, bid=10.00 clears cost + 0.5% target (9.5475) - fills
        # immediately at the (rounded-down-to-tick) bid.
        fake_bot, cancelled, placed = self._fake_bot(
            {"HOWL"}, working_orders, {"HOWL": Decimal("9.50")}
        )
        reprice = AutoTrader.reprice_volatility_scalp_exits.__get__(fake_bot)
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice([])
        self.assertEqual(cancelled, ["order-1"])
        self.assertEqual(placed, [("HOWL", "SELL", Decimal("1"), Decimal("10.00"))])
        self.assertNotIn("order-1", fake_bot.working_orders)
        self.assertEqual(
            fake_bot.working_orders["order-new"]["limit_price"], Decimal("10.00")
        )

    def test_ignores_a_symbol_that_is_not_currently_eligible(self):
        """Left to the separate reprice_resting_exits instead - see its
        own skip of eligible symbols.
        """
        from webull_bot.bot import AutoTrader

        working_orders = {
            "order-1": {
                "submitted_at": 0.0,
                "key": "STOCK:CALM",
                "action": "PROFIT",
                "cancel_requested_at": None,
                "limit_price": Decimal("9.90"),
            }
        }
        fake_bot, cancelled, placed = self._fake_bot(
            set(), working_orders, {"CALM": Decimal("9.50")}
        )
        reprice = AutoTrader.reprice_volatility_scalp_exits.__get__(fake_bot)
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice([])
        self.assertEqual(cancelled, [])
        self.assertEqual(placed, [])

    def test_keeps_managing_an_adopted_position_even_once_no_longer_live_eligible(
        self,
    ):
        """Live incident (this bug, caught from a real trade log): BTCT
        averaged down 5 times (blended cost ~$1.8494), then stopped out
        at $1.81 - a 2.1% drop, well inside the 5% hard-stop floor that
        should have protected it. is_volatility_scalp_eligible is a
        LIVE, continuously-recalculated stdev check - once several
        fills naturally calmed the rolling window down below the
        eligibility bar, the position instantly lost ALL cohort
        management (including this repricer) and fell back to the
        plain, much tighter general path. Once a symbol has actually
        been adopted (self.volatility_scalp_positions), it now keeps
        this treatment for as long as it's held, regardless of whether
        it's still live-eligible this exact cycle.
        """
        from webull_bot.bot import AutoTrader

        working_orders = {
            "order-1": {
                "submitted_at": 0.0,
                "key": "STOCK:BTCT",
                "action": "PROFIT",
                "cancel_requested_at": None,
                "limit_price": Decimal("1.90"),
            }
        }
        # BTCT is NOT in eligible_symbols (simulating it dropping out of
        # live eligibility), but IS in volatility_scalp_positions
        # (already adopted) - must still be actively managed.
        fake_bot, cancelled, placed = self._fake_bot(
            set(),
            working_orders,
            {"BTCT": Decimal("1.8494")},
            quote_by_symbol={"BTCT": ("1.86", "1.87")},
        )
        fake_bot.volatility_scalp_positions = {"BTCT"}
        reprice = AutoTrader.reprice_volatility_scalp_exits.__get__(fake_bot)
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice([])
        self.assertEqual(cancelled, ["order-1"])
        self.assertTrue(placed)

    def test_never_reprices_below_the_profit_floor(self):
        """Live incident: the old check here was a blunt "ask < cost ->
        skip entirely," which left a resting order frozen at a stale
        price whenever the ask dipped below cost, even if the bid still
        cleared a profitable fill. Now uses _stall_exit_price - still
        NEVER returns a price below cost + min_profit + fee, but tries
        the bid too, not just a raw ask comparison.
        """
        from webull_bot.bot import AutoTrader

        working_orders = {
            "order-1": {
                "submitted_at": 0.0,
                "key": "STOCK:HOWL",
                "action": "PROFIT",
                "cancel_requested_at": None,
                "limit_price": Decimal("11.00"),
            }
        }
        # cost=10.50, bid=9.95/ask=10.05 - NEITHER clears cost + 0.5%
        # target (10.5525), so no fillable profitable price exists at
        # all right now. Must not reprice (or fill) at a loss.
        fake_bot, cancelled, placed = self._fake_bot(
            {"HOWL"},
            working_orders,
            {"HOWL": Decimal("10.50")},
            quote_by_symbol={"HOWL": ("9.95", "10.05")},
        )
        reprice = AutoTrader.reprice_volatility_scalp_exits.__get__(fake_bot)
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice([])
        self.assertEqual(cancelled, [])
        self.assertEqual(placed, [])

    def test_reprices_down_toward_a_still_profitable_bid_below_the_stale_ask(self):
        """The specific bug this fix targets: the ask alone sitting
        below cost used to freeze repricing entirely. Now, if the BID
        still clears the floor, it reprices down to (and fills at) that
        lower-but-still-profitable price instead of staying stuck.
        """
        from webull_bot.bot import AutoTrader

        working_orders = {
            "order-1": {
                "submitted_at": 0.0,
                "key": "STOCK:HOWL",
                "action": "PROFIT",
                "cancel_requested_at": None,
                "limit_price": Decimal("11.00"),
            }
        }
        # cost=10.00, bid=10.10 clears cost + 0.5% target (10.05) even
        # though this is a lower price than the stale 11.00 resting
        # limit - reprices down to it rather than freezing.
        fake_bot, cancelled, placed = self._fake_bot(
            {"HOWL"},
            working_orders,
            {"HOWL": Decimal("10.00")},
            quote_by_symbol={"HOWL": ("10.10", "10.12")},
        )
        reprice = AutoTrader.reprice_volatility_scalp_exits.__get__(fake_bot)
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice([])
        self.assertEqual(cancelled, ["order-1"])
        self.assertEqual(placed, [("HOWL", "SELL", Decimal("1"), Decimal("10.10"))])

    def test_respects_its_own_faster_throttle(self):
        from webull_bot.bot import AutoTrader

        working_orders = {
            "order-1": {
                "submitted_at": 0.0,
                "key": "STOCK:HOWL",
                "action": "PROFIT",
                "cancel_requested_at": None,
                "limit_price": Decimal("9.90"),
            }
        }
        fake_bot, cancelled, placed = self._fake_bot(
            {"HOWL"}, working_orders, {"HOWL": Decimal("9.50")}
        )
        fake_bot.last_volatility_reprice = 99.5
        reprice = AutoTrader.reprice_volatility_scalp_exits.__get__(fake_bot)
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice([])
        self.assertEqual(cancelled, [])
        self.assertEqual(placed, [])


class VolatilityScalpEntryRepriceTests(unittest.TestCase):
    """reprice_volatility_scalp_entries - by request, don't wait
    passively for a resting dip-buy to fill; actively lower the limit
    if price keeps falling, since it may never come back up to the
    original price.
    """

    @staticmethod
    def _fake_bot(eligible_symbols, working_orders, buy_limit_by_symbol):
        from webull_bot.bot import AutoTrader

        cancelled = []
        placed = []

        class FakeApi:
            @staticmethod
            def stock_quote(symbol):
                return {"symbol": symbol}

            @staticmethod
            def stock_quotes_resilient(symbols, category):
                return ([FakeApi.stock_quote(s) for s in symbols], set())

            @staticmethod
            def stock_limit_price(q, side):
                return buy_limit_by_symbol.get(q["symbol"])

            @staticmethod
            def cancel(order_id):
                cancelled.append(order_id)

            @staticmethod
            def place_stock(symbol, side, quantity, limit_price=None):
                placed.append((symbol, side, quantity, limit_price))
                return "order-new"

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(volatility_scalp_reprice_seconds=Decimal("1")),
            api=FakeApi(),
            status=SimpleNamespace(rekey_trade=lambda old, new: None),
            last_volatility_entry_reprice=0.0,
            strategy=SimpleNamespace(
                is_volatility_scalp_eligible=lambda symbol: symbol in eligible_symbols
            ),
            volatility_scalp_positions=set(),
            stock_categories={},
            working_orders=working_orders,
        )
        fake_bot._batched_quotes = AutoTrader._batched_quotes.__get__(fake_bot)
        return fake_bot, cancelled, placed

    def test_lowers_the_limit_when_price_has_fallen(self):
        from webull_bot.bot import AutoTrader

        working_orders = {
            "order-1": {
                "submitted_at": 0.0,
                "key": "STOCK:GAUZ",
                "action": "BUY",
                "cancel_requested_at": None,
                "limit_price": Decimal("0.45"),
                "quantity": 100,
            }
        }
        fake_bot, cancelled, placed = self._fake_bot(
            {"GAUZ"}, working_orders, {"GAUZ": Decimal("0.40")}
        )
        reprice = AutoTrader.reprice_volatility_scalp_entries.__get__(fake_bot)
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice()
        self.assertEqual(cancelled, ["order-1"])
        self.assertEqual(placed, [("GAUZ", "BUY", 100, Decimal("0.40"))])
        self.assertNotIn("order-1", fake_bot.working_orders)
        self.assertEqual(
            fake_bot.working_orders["order-new"]["limit_price"], Decimal("0.40")
        )
        self.assertEqual(fake_bot.working_orders["order-new"]["quantity"], 100)

    def test_never_chases_the_price_upward(self):
        """Repricing up would mean paying more for the same dip-buy -
        only ever lower the resting limit, never raise it.
        """
        from webull_bot.bot import AutoTrader

        working_orders = {
            "order-1": {
                "submitted_at": 0.0,
                "key": "STOCK:GAUZ",
                "action": "BUY",
                "cancel_requested_at": None,
                "limit_price": Decimal("0.40"),
                "quantity": 100,
            }
        }
        fake_bot, cancelled, placed = self._fake_bot(
            {"GAUZ"}, working_orders, {"GAUZ": Decimal("0.45")}
        )
        reprice = AutoTrader.reprice_volatility_scalp_entries.__get__(fake_bot)
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice()
        self.assertEqual(cancelled, [])
        self.assertEqual(placed, [])

    def test_ignores_a_symbol_not_in_the_cohort(self):
        from webull_bot.bot import AutoTrader

        working_orders = {
            "order-1": {
                "submitted_at": 0.0,
                "key": "STOCK:CALM",
                "action": "BUY",
                "cancel_requested_at": None,
                "limit_price": Decimal("10.00"),
                "quantity": 5,
            }
        }
        fake_bot, cancelled, placed = self._fake_bot(
            set(), working_orders, {"CALM": Decimal("9.00")}
        )
        reprice = AutoTrader.reprice_volatility_scalp_entries.__get__(fake_bot)
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice()
        self.assertEqual(cancelled, [])
        self.assertEqual(placed, [])

    def test_ignores_a_non_buy_order(self):
        from webull_bot.bot import AutoTrader

        working_orders = {
            "order-1": {
                "submitted_at": 0.0,
                "key": "STOCK:GAUZ",
                "action": "PROFIT",
                "cancel_requested_at": None,
                "limit_price": Decimal("0.45"),
                "quantity": 100,
            }
        }
        fake_bot, cancelled, placed = self._fake_bot(
            {"GAUZ"}, working_orders, {"GAUZ": Decimal("0.40")}
        )
        reprice = AutoTrader.reprice_volatility_scalp_entries.__get__(fake_bot)
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice()
        self.assertEqual(cancelled, [])
        self.assertEqual(placed, [])

    def test_respects_its_own_throttle(self):
        from webull_bot.bot import AutoTrader

        working_orders = {
            "order-1": {
                "submitted_at": 0.0,
                "key": "STOCK:GAUZ",
                "action": "BUY",
                "cancel_requested_at": None,
                "limit_price": Decimal("0.45"),
                "quantity": 100,
            }
        }
        fake_bot, cancelled, placed = self._fake_bot(
            {"GAUZ"}, working_orders, {"GAUZ": Decimal("0.40")}
        )
        fake_bot.last_volatility_entry_reprice = 99.5
        reprice = AutoTrader.reprice_volatility_scalp_entries.__get__(fake_bot)
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice()
        self.assertEqual(cancelled, [])
        self.assertEqual(placed, [])


class RepriceRestingEntriesTests(unittest.TestCase):
    """reprice_resting_entries - by request: general (non-volatility-
    scalp) BUY/SHORT entries were resting passively at their original
    price forever and getting cancelled outright after order_timeout_
    seconds, instead of crossing further into the spread first. Live
    incident: IBRX got cancelled for never filling 4 separate times in
    ~15 minutes.
    """

    @staticmethod
    def _fake_bot(working_orders, bid, ask, eligible=False, fractional_ok=True):
        from webull_bot.bot import AutoTrader

        cancelled = []
        placed = []

        class FakeApi:
            @staticmethod
            def stock_quote(symbol):
                return {"symbol": symbol}

            @staticmethod
            def stock_quotes_resilient(symbols, category):
                return ([FakeApi.stock_quote(s) for s in symbols], set())

            @staticmethod
            def quote_ask(q):
                return ask

            @staticmethod
            def quote_bid(q):
                return bid

            @staticmethod
            def cancel(order_id):
                cancelled.append(order_id)

            @staticmethod
            def place_stock(symbol, side, quantity, limit_price=None):
                placed.append((symbol, side, quantity, limit_price))
                return "order-new"

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(order_monitor_seconds=Decimal("5")),
            api=FakeApi(),
            status=SimpleNamespace(rekey_trade=lambda old, new: None),
            last_entry_reprice=0.0,
            strategy=SimpleNamespace(is_volatility_scalp_eligible=lambda s: eligible),
            volatility_scalp_positions=set(),
            working_orders=working_orders,
            is_fractional_quantity=AutoTrader.is_fractional_quantity,
            stock_categories={},
        )
        fake_bot._batched_quotes = AutoTrader._batched_quotes.__get__(fake_bot)
        return fake_bot, cancelled, placed

    def test_buy_chases_up_toward_a_higher_ask(self):
        from webull_bot.bot import AutoTrader

        working_orders = {
            "order-1": {
                "submitted_at": 0.0,
                "key": "STOCK:IBRX",
                "action": "BUY",
                "cancel_requested_at": None,
                "limit_price": Decimal("8.00"),
                "quantity": Decimal("10"),
            }
        }
        fake_bot, cancelled, placed = self._fake_bot(
            working_orders, bid=Decimal("8.05"), ask=Decimal("8.10")
        )
        reprice = AutoTrader.reprice_resting_entries.__get__(fake_bot)
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice(True)
        self.assertEqual(cancelled, ["order-1"])
        self.assertEqual(placed, [("IBRX", "BUY", Decimal("10"), Decimal("8.10"))])
        self.assertNotIn("order-1", fake_bot.working_orders)
        self.assertEqual(
            fake_bot.working_orders["order-new"]["limit_price"], Decimal("8.10")
        )

    def test_short_chases_down_toward_a_lower_bid(self):
        from webull_bot.bot import AutoTrader

        working_orders = {
            "order-1": {
                "submitted_at": 0.0,
                "key": "STOCK:XYZ",
                "action": "SHORT",
                "cancel_requested_at": None,
                "limit_price": Decimal("20.00"),
                "quantity": 5,
            }
        }
        fake_bot, cancelled, placed = self._fake_bot(
            working_orders, bid=Decimal("19.90"), ask=Decimal("19.95")
        )
        reprice = AutoTrader.reprice_resting_entries.__get__(fake_bot)
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice(True)
        self.assertEqual(cancelled, ["order-1"])
        self.assertEqual(placed, [("XYZ", "SHORT", 5, Decimal("19.90"))])

    def test_does_not_reprice_when_the_price_has_not_improved(self):
        from webull_bot.bot import AutoTrader

        working_orders = {
            "order-1": {
                "submitted_at": 0.0,
                "key": "STOCK:IBRX",
                "action": "BUY",
                "cancel_requested_at": None,
                "limit_price": Decimal("8.10"),
                "quantity": Decimal("10"),
            }
        }
        fake_bot, cancelled, placed = self._fake_bot(
            working_orders, bid=Decimal("8.05"), ask=Decimal("8.10")
        )
        reprice = AutoTrader.reprice_resting_entries.__get__(fake_bot)
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice(True)
        self.assertEqual(cancelled, [])
        self.assertEqual(placed, [])

    def test_defers_to_the_volatility_scalp_repricer_for_eligible_symbols(self):
        from webull_bot.bot import AutoTrader

        working_orders = {
            "order-1": {
                "submitted_at": 0.0,
                "key": "STOCK:GAUZ",
                "action": "BUY",
                "cancel_requested_at": None,
                "limit_price": Decimal("0.40"),
                "quantity": 100,
            }
        }
        fake_bot, cancelled, placed = self._fake_bot(
            working_orders, bid=Decimal("0.44"), ask=Decimal("0.45"), eligible=True
        )
        reprice = AutoTrader.reprice_resting_entries.__get__(fake_bot)
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice(True)
        self.assertEqual(cancelled, [])
        self.assertEqual(placed, [])

    def test_ignores_a_non_entry_order(self):
        from webull_bot.bot import AutoTrader

        working_orders = {
            "order-1": {
                "submitted_at": 0.0,
                "key": "STOCK:IBRX",
                "action": "PROFIT",
                "cancel_requested_at": None,
                "limit_price": Decimal("8.00"),
                "quantity": 10,
            }
        }
        fake_bot, cancelled, placed = self._fake_bot(
            working_orders, bid=Decimal("8.05"), ask=Decimal("8.10")
        )
        reprice = AutoTrader.reprice_resting_entries.__get__(fake_bot)
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice(True)
        self.assertEqual(cancelled, [])
        self.assertEqual(placed, [])

    def test_skips_a_fractional_buy_outside_core_hours(self):
        from webull_bot.bot import AutoTrader

        working_orders = {
            "order-1": {
                "submitted_at": 0.0,
                "key": "STOCK:IBRX",
                "action": "BUY",
                "cancel_requested_at": None,
                "limit_price": Decimal("8.00"),
                "quantity": Decimal("2.5"),
            }
        }
        fake_bot, cancelled, placed = self._fake_bot(
            working_orders, bid=Decimal("8.05"), ask=Decimal("8.10")
        )
        reprice = AutoTrader.reprice_resting_entries.__get__(fake_bot)
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice(False)
        self.assertEqual(cancelled, [])
        self.assertEqual(placed, [])

    def test_respects_its_own_throttle(self):
        from webull_bot.bot import AutoTrader

        working_orders = {
            "order-1": {
                "submitted_at": 0.0,
                "key": "STOCK:IBRX",
                "action": "BUY",
                "cancel_requested_at": None,
                "limit_price": Decimal("8.00"),
                "quantity": 10,
            }
        }
        fake_bot, cancelled, placed = self._fake_bot(
            working_orders, bid=Decimal("8.05"), ask=Decimal("8.10")
        )
        fake_bot.last_entry_reprice = 99.0
        reprice = AutoTrader.reprice_resting_entries.__get__(fake_bot)
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice(True)
        self.assertEqual(cancelled, [])
        self.assertEqual(placed, [])


class BotOvertradingCapTests(unittest.TestCase):
    def test_rate_capped_blocks_after_configured_trades_per_hour(self):
        from collections import defaultdict, deque

        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(stock_max_trades_per_hour=2),
            trade_times=defaultdict(deque),
        )
        rate_capped = AutoTrader.rate_capped.__get__(fake_bot)
        key = "STOCK:CAPPED"
        # rate_capped prunes anything more than an hour old against the real
        # time.monotonic() clock, so timestamps must be pinned relative to a
        # frozen "now" - a literal 0.0 only stayed "recent" by coincidence of
        # how long this process/container had been up.
        with unittest.mock.patch("time.monotonic", return_value=0.0):
            self.assertFalse(rate_capped(key))
            fake_bot.trade_times[key].append(0.0)
            self.assertFalse(rate_capped(key))
            fake_bot.trade_times[key].append(0.0)
            self.assertTrue(rate_capped(key))

    def test_rate_cap_disabled_when_limit_is_zero(self):
        from collections import defaultdict, deque

        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(stock_max_trades_per_hour=0),
            trade_times=defaultdict(deque),
        )
        rate_capped = AutoTrader.rate_capped.__get__(fake_bot)
        key = "STOCK:UNCAPPED"
        for _ in range(50):
            fake_bot.trade_times[key].append(0.0)
        self.assertFalse(rate_capped(key))

    def test_record_realized_exit_tracks_pnl_and_loss_separately(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(sell_fee_dollars=Decimal("0.02")),
            daily_realized_pnl=Decimal("0"),
            daily_realized_loss=Decimal("0"),
            daily_pnl=SimpleNamespace(record=lambda *a, **k: None),
        )
        record = AutoTrader.record_realized_exit.__get__(fake_bot)
        record(Decimal("100"), Decimal("101"), 10)
        self.assertEqual(fake_bot.daily_realized_pnl, Decimal("9.98"))
        self.assertEqual(fake_bot.daily_realized_loss, Decimal("0"))
        record(Decimal("50"), Decimal("49"), 5)
        self.assertEqual(fake_bot.daily_realized_pnl, Decimal("4.96"))
        self.assertEqual(fake_bot.daily_realized_loss, Decimal("5.02"))

    def test_daily_loss_breaker_triggers_once_threshold_is_reached(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                daily_loss_circuit_breaker_enabled=True,
                daily_max_loss_dollars=Decimal("50"),
            ),
            daily_loss_breaker_triggered=False,
            daily_realized_loss=Decimal("10"),
            api=SimpleNamespace(close_all_positions=lambda loss_callback=None: []),
            wash_sales=SimpleNamespace(block=lambda *a, **k: None),
            last_account_refresh=0.0,
        )
        handle = AutoTrader.handle_daily_loss_breaker.__get__(fake_bot)
        self.assertFalse(handle())
        fake_bot.daily_realized_loss = Decimal("60")
        self.assertTrue(handle())
        self.assertTrue(fake_bot.daily_loss_breaker_triggered)
        # Stays tripped even if realized loss is later read as lower.
        fake_bot.daily_realized_loss = Decimal("0")
        self.assertTrue(handle())

    def test_daily_loss_breaker_disabled_by_default_behavior(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                daily_loss_circuit_breaker_enabled=False,
                daily_max_loss_dollars=Decimal("50"),
            ),
            daily_loss_breaker_triggered=False,
            daily_realized_loss=Decimal("999"),
        )
        handle = AutoTrader.handle_daily_loss_breaker.__get__(fake_bot)
        self.assertFalse(handle())


class StrategyReviewAgentTests(unittest.TestCase):
    def test_empty_or_truncated_completion_does_not_raise(self):
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        self.assertEqual(agent._parse_response(""), {})
        self.assertEqual(agent._parse_response(None), {})

    def test_salvage_json_objects_extracts_complete_entries_before_a_cutoff(self):
        # Two complete suggested_changes entries, then a third cut off
        # mid-string - exactly the "Unterminated string" shape a real
        # truncation produces.
        truncated = (
            '{"assessment":"x","severity":"minor","suggested_changes":['
            '{"lever":"position size","direction":"decrease"},'
            '{"lever":"entry selectivity","direction":"increase"},'
            '{"lever":"stop-loss tightness","reasoning":"unterminat'
        )
        salvaged = MarketResearchAgent._salvage_json_objects(truncated, "lever")
        self.assertEqual(
            [item["lever"] for item in salvaged],
            ["position size", "entry selectivity"],
        )

    def test_salvage_json_objects_ignores_objects_without_the_required_key(self):
        text = '{"assessment":"x","nested":{"foo":"bar"}}'
        self.assertEqual(
            MarketResearchAgent._salvage_json_objects(text, "lever"), []
        )

    def test_parse_response_recovers_partial_suggested_changes_from_truncation(self):
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.log = logging.getLogger("test-agent")
        truncated = (
            '{"assessment":"x","severity":"minor","suggested_changes":['
            '{"lever":"position size","direction":"decrease"},'
            '{"lever":"entry selectivity","direction":"increase"},'
            '{"lever":"stop-loss tightness","reasoning":"unterminat'
        )
        with self.assertLogs("test-agent", level="WARNING") as logs:
            parsed = agent._parse_response(truncated)
        self.assertEqual(
            [item["lever"] for item in parsed["suggested_changes"]],
            ["position size", "entry selectivity"],
        )
        self.assertIn("salvaged", logs.output[0])

    def test_parse_response_still_raises_when_nothing_is_salvageable(self):
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.log = logging.getLogger("test-agent")
        with self.assertRaises(json.JSONDecodeError):
            agent._parse_response('{"assessment": "unterminat')

    def test_parse_response_salvages_a_balanced_but_internally_broken_object(self):
        """Regression test: a top-level object whose braces are perfectly
        balanced but has a syntax error INSIDE it (e.g. a missing comma
        between two suggested_changes entries - "Expecting ',' delimiter"
        from a real production response) used to propagate straight out
        of _parse_response uncaught. _extract_json_object found a
        candidate (braces balance fine), but json.loads(candidate) itself
        raised and that call sat outside any try/except - the salvage
        path never even ran for this failure shape, only for a genuinely
        truncated one.
        """
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.log = logging.getLogger("test-agent")
        # Balanced overall, but missing the comma between the two
        # suggested_changes entries in the array.
        broken = (
            '{"assessment":"x","severity":"minor","suggested_changes":['
            '{"lever":"position size","direction":"decrease"}'
            '{"lever":"entry selectivity","direction":"increase"}'
            ']}'
        )
        with self.assertLogs("test-agent", level="WARNING") as logs:
            parsed = agent._parse_response(broken)
        self.assertEqual(
            [item["lever"] for item in parsed["suggested_changes"]],
            ["position size", "entry selectivity"],
        )
        self.assertIn("salvaged", logs.output[0])

    def test_normalize_review_drops_an_unrecognized_lever(self):
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        payload = agent._normalize_review(
            {
                "assessment": "x",
                "severity": "moderate",
                "confidence": 0.5,
                "suggested_changes": [
                    {
                        "lever": "made up lever",
                        "direction": "increase",
                        "reasoning": "not real",
                    },
                    {
                        "lever": "position size",
                        "direction": "decrease",
                        "reasoning": "ok",
                    },
                ],
            }
        )
        self.assertEqual(len(payload["suggested_changes"]), 1)
        self.assertEqual(payload["suggested_changes"][0]["lever"], "position size")

    def test_normalize_review_drops_an_unrecognized_direction(self):
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        payload = agent._normalize_review(
            {
                "suggested_changes": [
                    {
                        "lever": "position size",
                        "direction": "explode",
                        "reasoning": "not real",
                    }
                ],
            }
        )
        self.assertEqual(payload["suggested_changes"], [])

    def test_normalize_review_defaults_safely_for_garbage_input(self):
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        payload = agent._normalize_review("not a dict")
        self.assertEqual(payload["severity"], "none")
        self.assertEqual(payload["confidence"], 0)
        self.assertEqual(payload["suggested_changes"], [])

    def test_normalize_review_rejects_an_unrecognized_severity(self):
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        payload = agent._normalize_review({"severity": "catastrophic"})
        self.assertEqual(payload["severity"], "none")

    def test_session_date_resets_at_market_open_not_midnight(self):
        """AGENT_DAILY_REQUEST_LIMIT budgets the extended trading day
        (MARKET_OPEN_TIME to end of session), not a calendar day - a
        moment before market open still belongs to the previous session's
        tail end, not a fresh budget.
        """
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(market_open_time="04:00")
        agent.config.session_time = (
            lambda value: datetime_time(*(int(p) for p in value.split(":")))
        )

        before_open = datetime(2026, 8, 6, 2, 30, tzinfo=timezone.utc)
        self.assertEqual(
            agent._session_date(before_open), datetime(2026, 8, 5).date()
        )

        at_open = datetime(2026, 8, 6, 4, 0, tzinfo=timezone.utc)
        self.assertEqual(
            agent._session_date(at_open), datetime(2026, 8, 6).date()
        )

        mid_session = datetime(2026, 8, 6, 21, 0, tzinfo=timezone.utc)
        self.assertEqual(
            agent._session_date(mid_session), datetime(2026, 8, 6).date()
        )

    def test_submit_strategy_review_resets_budget_at_the_new_sessions_market_open(self):
        import queue as queue_module

        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(
            agent_daily_request_limit=250,
            agent_daily_token_budget=90000,
            market_open_time="04:00",
            strategy_review_interval_seconds=0,
        )
        agent.config.session_time = (
            lambda value: datetime_time(*(int(p) for p in value.split(":")))
        )
        agent._timezone = timezone.utc
        agent._requests_today = 200
        agent._limit_logged_date = None
        # Still "yesterday's" session per _session_date, even though the
        # calendar date has already ticked over past midnight.
        agent._request_date = datetime(2026, 8, 5).date()
        agent._last_submitted = 0.0
        agent._rate_limit_blocked = False
        agent._token_usage_log = []
        agent._token_limit_logged_at = 0.0
        agent._work = queue_module.Queue(maxsize=1)
        agent.log = logging.getLogger("test-agent")

        with unittest.mock.patch(
            "webull_bot.market_agent.datetime"
        ) as mock_datetime:
            mock_datetime.now.return_value = datetime(
                2026, 8, 6, 2, 30, tzinfo=timezone.utc
            )
            agent.submit_strategy_review({"a": 1})
        # Before market open - still yesterday's session, budget untouched.
        self.assertEqual(agent._requests_today, 200)

        with unittest.mock.patch(
            "webull_bot.market_agent.datetime"
        ) as mock_datetime:
            mock_datetime.now.return_value = datetime(
                2026, 8, 6, 5, 0, tzinfo=timezone.utc
            )
            agent.submit_strategy_review({"a": 1})
        # Past market open - new session, budget resets to 0
        # (submit_strategy_review() only resets/enqueues; _review_strategy()
        # is what later advances the count, on the worker thread this test
        # doesn't run).
        self.assertEqual(agent._requests_today, 0)
        self.assertEqual(agent._request_date, datetime(2026, 8, 6).date())

    def test_submit_strategy_review_force_bypasses_the_interval_throttle(self):
        """force=True must actually skip the interval wait - it was
        previously accepted as a parameter but never read anywhere in
        submit_strategy_review(), so a forced post-circuit-breaker
        reevaluation (submit_strategy_review(..., force=True)) silently
        behaved identically to a routine submit and could sit rate-
        limited for minutes instead of firing immediately.
        """
        import queue as queue_module
        import time as time_module

        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(
            agent_daily_request_limit=250,
            agent_daily_token_budget=90000,
            market_open_time="00:00",
            session_time=lambda value: datetime_time(0, 0),
            strategy_review_interval_seconds=120,
        )
        agent._request_date = datetime.now(timezone.utc).date()
        agent._requests_today = 0
        agent._limit_logged_date = None
        agent._last_submitted = time_module.monotonic()  # "just submitted"
        agent._timezone = timezone.utc
        agent._work = queue_module.Queue(maxsize=1)
        agent._rate_limit_blocked = False
        agent._token_usage_log = []
        agent._token_limit_logged_at = 0.0
        agent.log = logging.getLogger("test-agent")

        agent.submit_strategy_review({"a": 1}, force=False)
        self.assertTrue(agent._work.empty())  # still within the interval

        agent.submit_strategy_review({"a": 1}, force=True)
        self.assertFalse(agent._work.empty())  # force bypassed the wait

    def test_submit_strategy_review_respects_rolling_token_budget_and_rate_limit_block(self):
        """The interval throttle alone doesn't protect against Groq's real
        tokens-per-day cap - a request can be perfectly on-schedule and
        still 429 if the account's rolling 24h usage is near its limit.
        submit_strategy_review() must refuse to queue work in either case:
        usage already near budget, or a prior 429 having blocked the rest
        of the session.
        """
        import queue as queue_module

        # A fixed monotonic clock, not the real one -
        # submit_strategy_review()'s interval check compares elapsed-
        # since-_last_submitted against this, and the real clock's
        # absolute value depends on how long the host has been up, which
        # is not something a test should depend on.
        now = 100_000.0

        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(
            agent_daily_request_limit=250,
            agent_daily_token_budget=1000,
            market_open_time="00:00",
            session_time=lambda value: datetime_time(0, 0),
            strategy_review_interval_seconds=120,
        )
        agent._request_date = datetime.now(timezone.utc).date()
        agent._requests_today = 0
        agent._limit_logged_date = None
        agent._last_submitted = 0.0
        agent._timezone = timezone.utc
        agent._rate_limit_blocked = False
        agent._token_limit_logged_at = 0.0
        agent.log = logging.getLogger("test-agent")

        # Over the token budget, even though the interval has long elapsed.
        agent._work = queue_module.Queue(maxsize=1)
        agent._token_usage_log = [(now, 1500)]
        with unittest.mock.patch("time.monotonic", return_value=now):
            agent.submit_strategy_review({"a": 1})
        self.assertTrue(agent._work.empty())

        # Under budget and past the interval - goes through normally.
        agent._token_usage_log = [(now, 100)]
        with unittest.mock.patch("time.monotonic", return_value=now):
            agent.submit_strategy_review({"a": 1})
        self.assertFalse(agent._work.empty())

        # A prior 429 blocks submission for the rest of the session, even
        # with budget free and the interval elapsed - no backoff timer to
        # wait out, it just stays blocked until the next _session_date.
        agent._work = queue_module.Queue(maxsize=1)
        agent._last_submitted = 0.0
        agent._token_usage_log = []
        agent._rate_limit_blocked = True
        with unittest.mock.patch("time.monotonic", return_value=now):
            agent.submit_strategy_review({"a": 1})
        self.assertTrue(agent._work.empty())

    def test_rolling_tokens_used_prunes_entries_older_than_24h(self):
        import time as time_module

        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        now = time_module.monotonic()
        agent._token_usage_log = [
            (now - 86500, 5000),  # just over 24h old - dropped
            (now - 3600, 200),    # 1h old - kept
            (now, 100),           # fresh - kept
        ]
        self.assertEqual(agent._rolling_tokens_used(), 300)
        self.assertEqual(len(agent._token_usage_log), 2)

    def test_parse_retry_after_reads_groqs_minutes_seconds_hint(self):
        message = (
            "Error code: 429 - {'error': {'message': 'Rate limit reached "
            "... Please try again in 33m57.312s. Need more tokens?', "
            "'type': 'compound', 'code': 'rate_limit_exceeded'}}"
        )
        seconds = MarketResearchAgent._parse_retry_after(message)
        # 33*60 + 57.312 + 30s safety margin
        self.assertAlmostEqual(seconds, 2067.312, places=2)

    def test_parse_retry_after_falls_back_to_a_safe_default_when_unparseable(self):
        seconds = MarketResearchAgent._parse_retry_after("rate_limit_exceeded")
        self.assertEqual(seconds, 1800.0)

    def test_rate_limit_error_blocks_review_for_the_rest_of_the_session(self):
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.log = logging.getLogger("test-agent")
        agent._rate_limit_blocked = False

        error = RuntimeError(
            "Error code: 429 - {'error': {'message': 'Rate limit reached "
            "... Please try again in 16m38.784s.', 'type': 'compound', "
            "'code': 'rate_limit_exceeded'}}"
        )
        with self.assertLogs("test-agent", level="WARNING") as logs:
            agent._handle_review_error(error)

        self.assertTrue(agent._rate_limit_blocked)
        self.assertIn("until the next session", logs.output[0])

    def test_rate_limit_block_clears_only_at_the_next_session(self):
        """A prior day's 429 must not silently linger and block review
        forever - it clears specifically when submit_strategy_review()
        rolls over to a new _session_date (start of the next extended
        trading day), same as the request-count budget.
        """
        import queue as queue_module

        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(
            agent_daily_request_limit=250,
            agent_daily_token_budget=90000,
            market_open_time="04:00",
            strategy_review_interval_seconds=0,
        )
        agent.config.session_time = (
            lambda value: datetime_time(*(int(p) for p in value.split(":")))
        )
        agent._timezone = timezone.utc
        agent._requests_today = 10
        agent._limit_logged_date = None
        agent._request_date = datetime(2026, 8, 5).date()
        agent._rate_limit_blocked = True
        agent._last_submitted = 0.0
        agent._token_usage_log = []
        agent._token_limit_logged_at = 0.0
        agent._work = queue_module.Queue(maxsize=1)
        agent.log = logging.getLogger("test-agent")

        with unittest.mock.patch(
            "webull_bot.market_agent.datetime"
        ) as mock_datetime:
            mock_datetime.now.return_value = datetime(
                2026, 8, 5, 22, 0, tzinfo=timezone.utc
            )
            agent.submit_strategy_review({"a": 1})
        # Still the same session - the block must still be in effect.
        self.assertTrue(agent._rate_limit_blocked)
        self.assertTrue(agent._work.empty())

        with unittest.mock.patch(
            "webull_bot.market_agent.datetime"
        ) as mock_datetime:
            mock_datetime.now.return_value = datetime(
                2026, 8, 6, 5, 0, tzinfo=timezone.utc
            )
            agent.submit_strategy_review({"a": 1})
        # Past market open on a new day - the block clears and this submit
        # goes through.
        self.assertFalse(agent._rate_limit_blocked)
        self.assertFalse(agent._work.empty())

    def test_review_strategy_makes_exactly_one_call_per_cycle(self):
        """Regression test: a retry-with-different-params here used to
        count a second time against AGENT_DAILY_REQUEST_LIMIT and the
        rolling token budget, spending the day's budget faster than
        STRATEGY_REVIEW_INTERVAL_SECONDS' pacing intends -
        _review_strategy must place exactly one Groq call per invocation,
        no matter what comes back.
        """
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(
            agent_daily_request_limit=250,
            groq_model="groq/compound-mini",
        )
        agent.log = logging.getLogger("test-agent")
        agent._requests_today = 0
        agent._latest_strategy_review = None
        agent._lock = threading.Lock()

        calls = []

        class FakeMessage:
            def __init__(self, content):
                self.content = content

        class FakeChoice:
            def __init__(self, content):
                self.message = FakeMessage(content)

        class FakeResponse:
            def __init__(self, content):
                self.choices = [FakeChoice(content)]

        def fake_create(**kwargs):
            calls.append(kwargs["messages"][1]["content"])
            return FakeResponse("{}")

        agent.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=fake_create)
            )
        )

        agent._review_strategy(
            {"holdings": [], "pnl_today": {}, "recent_trades": []}
        )

        # Exactly one call - an empty response falls back to conservative
        # defaults for this cycle rather than retrying with different
        # params, and the request budget only ever advances by one.
        self.assertEqual(len(calls), 1)
        self.assertEqual(agent._requests_today, 1)
        self.assertEqual(agent._latest_strategy_review["severity"], "none")
        self.assertEqual(agent._latest_strategy_review["confidence"], 0)
        self.assertEqual(agent._latest_strategy_review["suggested_changes"], [])

    def test_review_strategy_disables_every_built_in_tool(self):
        """Regression test: raising max_completion_tokens and then telling
        the model to keep JSON compact both failed to reliably stop
        truncated/malformed responses in production - Groq's own tool-
        orchestration overhead before writing the JSON isn't something a
        prompt instruction can bound. This assessment never needs search
        (it's computed purely from STATE's numeric performance data), so
        every built-in tool must be disabled outright via compound_custom,
        not just discouraged in the prompt.
        """
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(
            agent_daily_request_limit=250,
            groq_model="groq/compound-mini",
        )
        agent.log = logging.getLogger("test-agent")
        agent._requests_today = 0
        agent._latest_strategy_review = None
        agent._lock = threading.Lock()

        captured = {}

        class FakeMessage:
            def __init__(self, content):
                self.content = content

        class FakeChoice:
            def __init__(self, content):
                self.message = FakeMessage(content)

        class FakeResponse:
            def __init__(self, content):
                self.choices = [FakeChoice(content)]

        def fake_create(**kwargs):
            captured.update(kwargs)
            return FakeResponse("{}")

        agent.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=fake_create)
            )
        )

        agent._review_strategy(
            {"holdings": [], "pnl_today": {}, "recent_trades": []}
        )

        self.assertEqual(
            captured["compound_custom"], {"tools": {"enabled_tools": []}}
        )
        self.assertNotIn("search_settings", captured)

    def test_review_strategy_omits_compound_custom_for_a_plain_model(self):
        """A plain (non-Compound) model doesn't understand compound_custom -
        it must only be sent when groq_model is actually a Compound system,
        not unconditionally.
        """
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(
            agent_daily_request_limit=250,
            groq_model="llama-3.3-70b-versatile",
        )
        agent.log = logging.getLogger("test-agent")
        agent._requests_today = 0
        agent._latest_strategy_review = None
        agent._lock = threading.Lock()

        captured = {}

        class FakeMessage:
            def __init__(self, content):
                self.content = content

        class FakeChoice:
            def __init__(self, content):
                self.message = FakeMessage(content)

        class FakeResponse:
            def __init__(self, content):
                self.choices = [FakeChoice(content)]

        def fake_create(**kwargs):
            captured.update(kwargs)
            return FakeResponse("{}")

        agent.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=fake_create)
            )
        )

        agent._review_strategy(
            {"holdings": [], "pnl_today": {}, "recent_trades": []}
        )

        self.assertNotIn("compound_custom", captured)

    def test_review_strategy_includes_reasoning_effort_for_a_gpt_oss_model(self):
        """gpt-oss is a reasoning model - its hidden "thinking" tokens are
        counted against max_completion_tokens, so reasoning_effort must be
        sent to keep that overhead small and bounded instead of unbounded.
        """
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(
            agent_daily_request_limit=250,
            groq_model="openai/gpt-oss-120b",
            groq_reasoning_effort="low",
        )
        agent.log = logging.getLogger("test-agent")
        agent._requests_today = 0
        agent._latest_strategy_review = None
        agent._lock = threading.Lock()

        captured = {}

        class FakeMessage:
            def __init__(self, content):
                self.content = content

        class FakeChoice:
            def __init__(self, content):
                self.message = FakeMessage(content)

        class FakeResponse:
            def __init__(self, content):
                self.choices = [FakeChoice(content)]

        def fake_create(**kwargs):
            captured.update(kwargs)
            return FakeResponse("{}")

        agent.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=fake_create)
            )
        )

        agent._review_strategy(
            {"holdings": [], "pnl_today": {}, "recent_trades": []}
        )

        self.assertEqual(captured["reasoning_effort"], "low")
        self.assertNotIn("compound_custom", captured)

    def test_review_strategy_omits_reasoning_effort_for_a_non_gpt_oss_model(self):
        """reasoning_effort is a gpt-oss-specific parameter - Groq rejects
        it outright for Compound, and other model families use a different
        enum, so it must only be sent when groq_model is actually gpt-oss.
        """
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(
            agent_daily_request_limit=250,
            groq_model="groq/compound-mini",
        )
        agent.log = logging.getLogger("test-agent")
        agent._requests_today = 0
        agent._latest_strategy_review = None
        agent._lock = threading.Lock()

        captured = {}

        class FakeMessage:
            def __init__(self, content):
                self.content = content

        class FakeChoice:
            def __init__(self, content):
                self.message = FakeMessage(content)

        class FakeResponse:
            def __init__(self, content):
                self.choices = [FakeChoice(content)]

        def fake_create(**kwargs):
            captured.update(kwargs)
            return FakeResponse("{}")

        agent.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=fake_create)
            )
        )

        agent._review_strategy(
            {"holdings": [], "pnl_today": {}, "recent_trades": []}
        )

        self.assertNotIn("reasoning_effort", captured)

    def test_review_strategy_requests_a_tpm_safe_completion_budget(self):
        """This account's on-demand tier caps prompt_tokens +
        max_completion_tokens at 8000 per request, enforced before the
        model runs - requesting the old 8000 ceiling here would always be
        rejected outright once any real STATE prompt is added on top of it.
        """
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(
            agent_daily_request_limit=250,
            groq_model="openai/gpt-oss-120b",
            groq_reasoning_effort="low",
        )
        agent.log = logging.getLogger("test-agent")
        agent._requests_today = 0
        agent._latest_strategy_review = None
        agent._lock = threading.Lock()

        captured = {}

        class FakeMessage:
            def __init__(self, content):
                self.content = content

        class FakeChoice:
            def __init__(self, content):
                self.message = FakeMessage(content)

        class FakeResponse:
            def __init__(self, content):
                self.choices = [FakeChoice(content)]

        def fake_create(**kwargs):
            captured.update(kwargs)
            return FakeResponse("{}")

        agent.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=fake_create)
            )
        )

        agent._review_strategy(
            {"holdings": [], "pnl_today": {}, "recent_trades": []}
        )

        self.assertLessEqual(captured["max_completion_tokens"], 4000)


class AllocationAndLoggingTests(unittest.TestCase):
    def test_default_capital_and_position_allocations(self):
        config = Settings()
        self.assertEqual(
            sum(config.stock_capital_fractions().values()),
            Decimal("1.00"),
        )
        self.assertEqual(
            config.stock_bucket_slot_limits(),
            {"POPULAR": 35, "PENNY": 5, "DISCOVERY": 10},
        )
        self.assertEqual(config.stock_universe_page_size, 200)
        self.assertEqual(config.stocks(), ["ALL"])
        self.assertEqual(config.max_symbols, 800)
        self.assertEqual(config.stock_universe_limit(), 800)
        self.assertEqual(Settings(max_symbols=0).stock_universe_limit(), 500)
        self.assertTrue(
            {"NVDA", "TSLA", "AAPL", "GME", "AMC"}
            <= set(config.popular_stocks())
        )
        self.assertNotIn("SPY", config.popular_stocks())
        self.assertNotIn("QQQ", config.popular_stocks())

    def test_default_risk_tuning_keeps_stop_floor_above_spread_gate(self):
        """STOCK_ENTRY_MAX_SPREAD_PERCENT must stay comfortably below
        STOCK_STOP_LOSS_MIN_PERCENT - the *floor*, not just the ceiling.
        A calm stock's adaptive stop clamps to the floor regardless of the
        range multiplier, so if the floor sat below the max tolerated
        spread (it briefly did: a 0.12% floor against a 0.50% spread gate),
        an entry near that spread ceiling could get stopped out by an
        ordinary bid/ask bounce alone, before any real adverse move -
        "trigger happy" stops firing on noise, not on a real loss.
        """
        config = Settings()
        self.assertEqual(config.stock_entry_max_spread_percent, Decimal("0.50"))
        self.assertEqual(config.stock_stop_loss_min_percent, Decimal("0.009"))
        self.assertEqual(config.stock_stop_loss_max_percent, Decimal("0.015"))
        self.assertEqual(config.stock_stop_loss_range_multiplier, Decimal("0.35"))
        spread_as_fraction = config.stock_entry_max_spread_percent / 100
        self.assertLess(spread_as_fraction, config.stock_stop_loss_min_percent)
        self.assertLess(spread_as_fraction, config.stock_stop_loss_max_percent)

    def test_default_reward_risk_ratio_gives_a_comfortable_breakeven_margin(self):
        """At the old STOCK_TARGET_STOP_MULTIPLE=1.2, breakeven needs a
        ~45.5% win rate (1 / (1 + ratio)) - too thin a margin for normal
        noise/whipsaw, and a real cause of net-losing days even with
        plenty of individual winning trades. 1.8 only needs ~35.7%.
        Trading itself is never automatically halted - no circuit breaker
        is enabled by default; this fix only improves the ratio each trade
        is judged against.
        """
        config = Settings()
        self.assertEqual(config.stock_target_stop_multiple, Decimal("1.8"))
        breakeven_win_rate = 1 / (1 + config.stock_target_stop_multiple)
        self.assertLess(breakeven_win_rate, Decimal("0.36"))
        self.assertFalse(config.daily_loss_circuit_breaker_enabled)

    def test_default_watchlist_is_parsed_and_deduplicated_by_membership(self):
        config = Settings()
        watchlist = config.default_watchlist()
        self.assertTrue(
            {"AAPL", "NVDA", "TSLA", "MSFT", "AMZN"} <= set(watchlist)
        )
        self.assertEqual(watchlist, [item.upper() for item in watchlist])

    def test_log_handler_writes_year_month_and_date_path(self):
        directory = Path("tests/.generated_logs")
        shutil.rmtree(directory, ignore_errors=True)
        try:
            handler = DatedDailyFileHandler(directory, "UTC")
            handler.setFormatter(logging.Formatter("%(message)s"))
            record = logging.LogRecord(
                "test",
                logging.INFO,
                __file__,
                1,
                "important context",
                (),
                None,
            )
            handler.emit(record)
            handler.close()

            today = datetime.now(timezone.utc).date()
            path = (
                Path(directory)
                / f"{today:%Y}"
                / f"{today:%m}"
                / f"{today:%Y-%m-%d}.log"
            )
            self.assertEqual(path.read_text(encoding="utf-8"), "important context\n")
        finally:
            shutil.rmtree(directory, ignore_errors=True)


class FractionalSharesTests(StrategyConfigMixin, unittest.TestCase):
    def test_fractional_stock_quantity_sizes_to_available_budget(self):
        config = self.config()
        config.fractional_shares_min_notional = Decimal("5")
        strategy = TradingStrategy(config)

        quantity = strategy.fractional_stock_quantity(Decimal("400.00"), Decimal("50.00"))

        self.assertEqual(quantity, Decimal("0.1250"))

    def test_fractional_stock_quantity_caps_at_one_share(self):
        config = self.config()
        config.fractional_shares_min_notional = Decimal("5")
        strategy = TradingStrategy(config)

        quantity = strategy.fractional_stock_quantity(Decimal("10.00"), Decimal("5000.00"))

        self.assertEqual(quantity, Decimal("1"))

    def test_fractional_stock_quantity_returns_zero_below_minimum_notional(self):
        config = self.config()
        config.fractional_shares_min_notional = Decimal("5")
        strategy = TradingStrategy(config)

        quantity = strategy.fractional_stock_quantity(Decimal("400.00"), Decimal("3.00"))

        self.assertEqual(quantity, Decimal("0"))

    def test_minimum_lot_size_requires_100_shares_between_10_cents_and_a_dollar(self):
        self.assertEqual(TradingStrategy.minimum_lot_size(Decimal("0.05")), 1)
        self.assertEqual(TradingStrategy.minimum_lot_size(Decimal("0.10")), 100)
        self.assertEqual(TradingStrategy.minimum_lot_size(Decimal("0.50")), 100)
        self.assertEqual(TradingStrategy.minimum_lot_size(Decimal("0.999")), 100)
        self.assertEqual(TradingStrategy.minimum_lot_size(Decimal("1.00")), 1)
        self.assertEqual(TradingStrategy.minimum_lot_size(Decimal("50.00")), 1)

    def test_exit_blocked_by_lot_restriction_allows_a_fractional_exit_at_a_normal_price(self):
        """Regression test for a live incident: every fractional position
        (quantity < 1 share) at an ordinarily-priced stock - the common
        case, not a rare one, since fractional entries are dollar-sized
        slices of normal stocks - had every PROFIT/LOSS/stall-breaker exit
        silently rejected, indefinitely, by the old bare `quantity <
        minimum_lot_size(price)` comparison: minimum_lot_size returns 1
        outside the $0.10-$0.999 band, and a fractional quantity is by
        definition under 1, so that comparison was true for practically
        every fractional position regardless of price. Confirmed against a
        real live position (CVX, 0.1268 shares at ~$205) that couldn't
        stop out despite a real, ordinary ~1% adverse move.
        """
        self.assertFalse(
            TradingStrategy.exit_blocked_by_lot_restriction(
                Decimal("0.1268"), Decimal("204.86")
            )
        )
        self.assertFalse(
            TradingStrategy.exit_blocked_by_lot_restriction(
                Decimal("0.0356"), Decimal("768.24")
            )
        )

    def test_exit_blocked_by_lot_restriction_still_blocks_the_real_sub_dollar_band(self):
        self.assertTrue(
            TradingStrategy.exit_blocked_by_lot_restriction(
                Decimal("1"), Decimal("0.50")
            )
        )
        self.assertTrue(
            TradingStrategy.exit_blocked_by_lot_restriction(
                Decimal("0.5"), Decimal("0.50")
            )
        )
        self.assertFalse(
            TradingStrategy.exit_blocked_by_lot_restriction(
                Decimal("100"), Decimal("0.50")
            )
        )

    def test_stock_order_quantity_rounds_up_to_100_when_affordable(self):
        config = SimpleNamespace(stock_quantity=1, max_order_notional=Decimal("1000"))
        strategy = TradingStrategy.__new__(TradingStrategy)
        strategy.config = config

        quantity, _ = strategy.stock_order_quantity(Decimal("0.50"), Decimal("100"))

        self.assertEqual(quantity, 100)

    def test_stock_order_quantity_skips_penny_stock_when_100_shares_unaffordable(self):
        config = SimpleNamespace(stock_quantity=1, max_order_notional=Decimal("1000"))
        strategy = TradingStrategy.__new__(TradingStrategy)
        strategy.config = config

        quantity, _ = strategy.stock_order_quantity(Decimal("0.50"), Decimal("10"))

        self.assertEqual(quantity, 0)

    def test_fractional_stock_quantity_skips_the_100_share_minimum_price_band(self):
        config = self.config()
        config.fractional_shares_min_notional = Decimal("5")
        strategy = TradingStrategy(config)

        quantity = strategy.fractional_stock_quantity(Decimal("0.50"), Decimal("1000"))

        self.assertEqual(quantity, Decimal("0"))

    def test_dollar_stock_quantity_sizes_by_notional_uncapped_at_one_share(self):
        config = self.config()
        config.fractional_shares_min_notional = Decimal("5")
        strategy = TradingStrategy(config)

        quantity, buffered_price = strategy.dollar_stock_quantity(
            Decimal("2"), Decimal("500")
        )

        self.assertEqual(buffered_price, Decimal("2") * Decimal("1.03"))
        expected = (Decimal("500") / buffered_price).quantize(
            Decimal("0.0001"), rounding=ROUND_DOWN
        )
        self.assertEqual(quantity, expected)
        self.assertGreater(quantity, Decimal("1"))

    def test_dollar_stock_quantity_skips_lot_restricted_band(self):
        config = self.config()
        config.fractional_shares_min_notional = Decimal("5")
        strategy = TradingStrategy(config)

        quantity, _ = strategy.dollar_stock_quantity(
            Decimal("0.50"), Decimal("1000")
        )

        self.assertEqual(quantity, Decimal("0"))

    def test_dollar_stock_quantity_respects_min_notional(self):
        config = self.config()
        config.fractional_shares_min_notional = Decimal("5")
        strategy = TradingStrategy(config)

        quantity, _ = strategy.dollar_stock_quantity(
            Decimal("400.00"), Decimal("3.00")
        )

        self.assertEqual(quantity, Decimal("0"))

    def test_place_stock_fractional_forces_market_core_and_omits_limit(self):
        api = WebullAPI.__new__(WebullAPI)
        captured = []

        def fake_call(callback, group, retry=True):
            return callback()

        def fake_place_order(account_id, orders):
            captured.extend(orders)
            return None

        api._call = fake_call
        api.trade = SimpleNamespace(
            order_v3=SimpleNamespace(place_order=fake_place_order)
        )
        api.config = SimpleNamespace(account_id="acct-1")

        api.place_stock(
            "TSLA",
            "BUY",
            Decimal("0.5"),
            limit_price=Decimal("250.00"),
            fractional=True,
        )

        self.assertEqual(len(captured), 1)
        order = captured[0]
        self.assertEqual(order["order_type"], "MARKET")
        self.assertEqual(order["support_trading_session"], "CORE")
        self.assertEqual(order["quantity"], "0.5")
        self.assertNotIn("limit_price", order)

    def test_place_stock_whole_share_unaffected_by_fractional_param(self):
        api = WebullAPI.__new__(WebullAPI)
        captured = []

        def fake_call(callback, group, retry=True):
            return callback()

        def fake_place_order(account_id, orders):
            captured.extend(orders)
            return None

        api._call = fake_call
        api.trade = SimpleNamespace(
            order_v3=SimpleNamespace(place_order=fake_place_order)
        )
        api.config = SimpleNamespace(account_id="acct-1")

        api.place_stock("TSLA", "BUY", 3, limit_price=Decimal("250.00"))

        order = captured[0]
        self.assertEqual(order["order_type"], "LIMIT")
        self.assertEqual(order["support_trading_session"], "ALL")
        self.assertEqual(order["limit_price"], "250.00")

    def test_stock_position_reports_fractional_quantity_without_truncation(self):
        positions = [
            {
                "instrument_type": "EQUITY",
                "symbol": "TSLA",
                "quantity": "0.5",
                "cost_price": "250.00",
            }
        ]

        quantity, cost = WebullAPI.stock_position("TSLA", positions)

        self.assertEqual(quantity, Decimal("0.5"))
        self.assertEqual(cost, Decimal("250.00"))


class AccountDayPnlExtractionTests(unittest.TestCase):
    def test_extracts_total_day_profit_loss(self):
        balance = {"total_day_profit_loss": "-0.22"}
        self.assertEqual(
            WebullAPI.account_day_pnl_from_balance(balance), Decimal("-0.22")
        )

    def test_none_when_unreported(self):
        self.assertIsNone(WebullAPI.account_day_pnl_from_balance({}))

    def test_none_on_a_malformed_value(self):
        balance = {"total_day_profit_loss": "not-a-number"}
        self.assertIsNone(WebullAPI.account_day_pnl_from_balance(balance))

    def test_buying_power_from_balance_matches_the_prior_buying_power_behavior(self):
        balance = {
            "account_currency_assets": [
                {"currency": "USD", "day_buying_power": "120.00", "buying_power": "100.00"}
            ]
        }
        self.assertEqual(
            WebullAPI.buying_power_from_balance(balance), Decimal("120.00")
        )


class StopExitPricingTests(unittest.TestCase):
    def test_stop_exit_uses_bid_ask_midpoint_not_aggressive_crossing(self):
        api = WebullAPI.__new__(WebullAPI)
        quote = {"bid": "99.00", "ask": "99.20"}
        self.assertEqual(api.stock_stop_exit_price(quote), Decimal("99.10"))

    def test_stop_exit_requires_valid_spread(self):
        api = WebullAPI.__new__(WebullAPI)
        with self.assertRaises(QuoteUnavailableError):
            api.stock_stop_exit_price({"bid": "0", "ask": "99.20"})

    def test_stop_exit_blends_in_the_last_trade_when_inside_the_spread(self):
        api = WebullAPI.__new__(WebullAPI)
        api.config = SimpleNamespace(quote_price_sanity_percent=Decimal("0.08"))
        # bid=99.00, ask=99.20, last=99.18 -> (99.00+99.20+99.18)/3 = 99.1267
        quote = {"bid": "99.00", "ask": "99.20", "price": "99.18"}
        self.assertEqual(api.stock_stop_exit_price(quote), Decimal("99.12"))

    def test_stop_exit_ignores_a_last_trade_outside_the_current_spread(self):
        """A last-trade print from before the spread moved shouldn't pull
        the price outside the current, real market - falls back to the
        plain midpoint exactly like no price were reported at all.
        """
        api = WebullAPI.__new__(WebullAPI)
        api.config = SimpleNamespace(quote_price_sanity_percent=Decimal("0.08"))
        quote = {"bid": "99.00", "ask": "99.20", "price": "95.00"}
        self.assertEqual(api.stock_stop_exit_price(quote), Decimal("99.10"))


class BidAskLastMidpointTests(unittest.TestCase):
    """stock_limit_price's passive BUY/SHORT branch shares
    _bid_ask_last_midpoint with stock_stop_exit_price - see
    StopExitPricingTests for the shared formula's own coverage.
    """

    def test_buy_blends_in_the_last_trade_when_inside_the_spread(self):
        api = WebullAPI.__new__(WebullAPI)
        api.config = SimpleNamespace(
            quote_price_sanity_percent=Decimal("0.08"),
            stock_limit_offset=Decimal("0.001"),
        )
        quote = {"bid": "99.00", "ask": "99.20", "price": "99.18"}
        self.assertEqual(api.stock_limit_price(quote, "BUY"), Decimal("99.12"))

    def test_short_uses_the_same_passive_midpoint_as_buy(self):
        api = WebullAPI.__new__(WebullAPI)
        api.config = SimpleNamespace(
            quote_price_sanity_percent=Decimal("0.08"),
            stock_limit_offset=Decimal("0.001"),
        )
        quote = {"bid": "99.00", "ask": "99.20", "price": "99.18"}
        self.assertEqual(api.stock_limit_price(quote, "SHORT"), Decimal("99.12"))

    def test_buy_falls_back_to_the_plain_midpoint_with_no_last_trade(self):
        api = WebullAPI.__new__(WebullAPI)
        api.config = SimpleNamespace(
            quote_price_sanity_percent=Decimal("0.08"),
            stock_limit_offset=Decimal("0.001"),
        )
        quote = {"bid": "99.00", "ask": "99.20"}
        self.assertEqual(api.stock_limit_price(quote, "BUY"), Decimal("99.10"))

    def test_close_all_positions_flags_option_losses_for_wash_sale(self):
        api = WebullAPI.__new__(WebullAPI)
        contract = {
            "symbol": "AAPL260101C00200000",
            "underlying_symbol": "AAPL",
            "strike_price": "200",
            "expiration_date": "2026-01-01",
            "option_type": "CALL",
        }
        api.positions = lambda: [
            {
                "instrument_type": "OPTION",
                "symbol": "AAPL260101C00200000",
                "quantity": "2",
                "cost_price": "5.00",
            }
        ]
        api.cancel_all_orders = lambda: []
        api.contract_from_position = lambda position: contract
        api.option_quote = lambda symbol: {"bid": "3.00", "ask": "3.20", "price": "3.10"}
        api.option_limit_price = lambda quote, side: Decimal("3.00")
        api.place_option = lambda *a, **k: "order-123"

        losses = []
        submitted = api.close_all_positions(
            loss_callback=lambda symbol, reason: losses.append((symbol, reason))
        )
        self.assertEqual(submitted, ["order-123"])
        self.assertEqual(losses, [("AAPL", "option loss closeout submitted")])

    def test_close_all_positions_does_not_skip_a_fractional_equity_position(self):
        api = WebullAPI.__new__(WebullAPI)
        api.positions = lambda: [
            {
                "instrument_type": "EQUITY",
                "symbol": "TSLA",
                "quantity": "0.5",
                "cost_price": "250.00",
            }
        ]
        api.cancel_all_orders = lambda: []
        api.stock_quote = lambda symbol: {"bid": "255.00", "ask": "255.20"}
        api.quote_price = staticmethod(lambda quote: Decimal("255.10"))
        api.stock_limit_price = lambda quote, side: Decimal("255.00")

        placed = []

        def fake_place_stock(symbol, side, quantity, limit_price=None, fractional=False):
            placed.append((symbol, side, quantity, fractional))
            return "order-456"

        api.place_stock = fake_place_stock

        submitted = api.close_all_positions()

        self.assertEqual(submitted, ["order-456"])
        self.assertEqual(placed, [("TSLA", "SELL", Decimal("0.5"), True)])


class PayloadSizingTests(unittest.TestCase):
    def test_payload_errors_are_detected(self):
        self.assertTrue(WebullAPI._payload_too_large("HTTP 413"))
        self.assertTrue(WebullAPI._payload_too_large("Payload Too Large"))
        self.assertFalse(WebullAPI._payload_too_large("INVALID_SYMBOL"))

    def test_oversized_stock_snapshot_is_bisected(self):
        api = WebullAPI.__new__(WebullAPI)
        calls = []

        def stock_quotes(symbols, category):
            calls.append(list(symbols))
            if len(symbols) > 2:
                raise RuntimeError("Webull API error 413: payload too large")
            return [{"symbol": symbol} for symbol in symbols]

        api.stock_quotes = stock_quotes
        quotes, invalid = api.stock_quotes_resilient(
            ["A", "B", "C", "D", "E"],
            "US_STOCK",
        )

        self.assertEqual(
            [item["symbol"] for item in quotes],
            ["A", "B", "C", "D", "E"],
        )
        self.assertEqual(invalid, set())
        self.assertGreater(len(calls), 1)


class HistoryBarResponseShapeTests(unittest.TestCase):
    """Live incident: the real batch-history-bar response is
    {"result": [{"symbol": ..., "result": [...bars]}]} - one layer
    deeper than _parse_amplitudes/_parse_closes/recent_minute_closes
    ever assumed (they expected a flat list with bars under "bars" or
    "candles"). This silently gave VOLFILT's daily volatility pre-filter
    and the SMA trend filter 0/N coverage every single day (both fell
    back to their safe "no filtering" default, so nothing crashed - it
    just never worked), and broke volatility-scalp's M1 bar-seeding
    outright (recent_minute_closes always returned {}).
    """

    def test_history_bars_resilient_unwraps_the_outer_result_key(self):
        api = WebullAPI.__new__(WebullAPI)

        def fake_call(callback, group):
            return {
                "result": [
                    {
                        "symbol": "TBB",
                        "result": [{"close": "19.53"}, {"close": "19.50"}],
                        "instrument_id": "925401989",
                    }
                ]
            }

        api._call = fake_call
        page = api._history_bars_resilient(["TBB"], "US_STOCK", "M1", "20")
        self.assertEqual(page, [
            {
                "symbol": "TBB",
                "result": [{"close": "19.53"}, {"close": "19.50"}],
                "instrument_id": "925401989",
            }
        ])

    def test_history_bars_resilient_passes_through_an_already_flat_list(self):
        """Defensive: if the SDK ever hands back an already-unwrapped
        list (e.g. a test double, or a future SDK version), don't break.
        """
        api = WebullAPI.__new__(WebullAPI)

        def fake_call(callback, group):
            return [{"symbol": "TBB", "bars": [{"close": "19.53"}]}]

        api._call = fake_call
        page = api._history_bars_resilient(["TBB"], "US_STOCK", "M1", "20")
        self.assertEqual(page, [{"symbol": "TBB", "bars": [{"close": "19.53"}]}])

    def test_extract_bars_prefers_bars_then_candles_then_result(self):
        self.assertEqual(WebullAPI._extract_bars({"bars": [1]}), [1])
        self.assertEqual(WebullAPI._extract_bars({"candles": [2]}), [2])
        self.assertEqual(WebullAPI._extract_bars({"result": [3]}), [3])
        self.assertIsNone(WebullAPI._extract_bars({}))
        self.assertIsNone(WebullAPI._extract_bars({"result": "not-a-list"}))

    def test_recent_minute_closes_parses_the_real_nested_response_shape(self):
        api = WebullAPI.__new__(WebullAPI)
        fake_timespan = SimpleNamespace(M1=SimpleNamespace(name="M1"))

        def fake_call(callback, group):
            return {
                "result": [
                    {
                        "symbol": "TBB",
                        "result": [
                            {"time": "2026-08-21T17:43:00.000+0000", "close": "19.53"},
                            {"time": "2026-08-21T17:34:00.000+0000", "close": "19.54"},
                            {"time": "2026-08-21T17:30:00.000+0000", "close": "19.5285"},
                        ],
                        "instrument_id": "925401989",
                    }
                ]
            }

        api._call = fake_call
        api.data = SimpleNamespace(
            market_data=SimpleNamespace(get_batch_history_bar=lambda *a, **k: None)
        )

        with unittest.mock.patch.dict(
            sys.modules,
            {"webull.data.common.timespan": SimpleNamespace(Timespan=fake_timespan)},
        ):
            closes = api.recent_minute_closes(["TBB"], "US_STOCK", count=20)

        self.assertEqual(closes, {"TBB": [19.5285, 19.54, 19.53]})

    def test_sma_trend_parses_the_real_nested_response_shape(self):
        api = WebullAPI.__new__(WebullAPI)
        fake_category = SimpleNamespace(US_STOCK=SimpleNamespace(name="US_STOCK"))
        fake_timespan = SimpleNamespace(D=SimpleNamespace(name="DAY"))

        def fake_call(callback, group):
            return {
                "result": [
                    {"symbol": "NVDA", "result": [{"close": "100"}, {"close": "80"}]}
                ]
            }

        api._call = fake_call
        api.data = SimpleNamespace(
            market_data=SimpleNamespace(get_batch_history_bar=lambda *a, **k: None)
        )

        with unittest.mock.patch.dict(
            sys.modules,
            {
                "webull.data.common.category": SimpleNamespace(Category=fake_category),
                "webull.data.common.timespan": SimpleNamespace(Timespan=fake_timespan),
            },
        ):
            sma = api.sma_trend(["NVDA"], days=2)

        self.assertEqual(sma, {"NVDA": 90.0})


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


class StockScreenerTests(StrategyConfigMixin, unittest.TestCase):
    def test_default_stock_capital_fractions_are_popular_penny_discovery(self):
        config = Settings()
        self.assertEqual(
            config.stock_capital_fractions(),
            {
                "POPULAR": Decimal("0.70"),
                "PENNY": Decimal("0.10"),
                "DISCOVERY": Decimal("0.20"),
            },
        )

    def test_screener_number_handles_bad_and_nonfinite_values(self):
        self.assertEqual(
            WebullAPI._screener_number({"market_value": "1.5e11"}, "market_value"),
            1.5e11,
        )
        self.assertEqual(
            WebullAPI._screener_number({"market_value": "nope"}, "market_value"),
            0.0,
        )
        self.assertEqual(
            WebullAPI._screener_number({}, "market_value"),
            0.0,
        )
        self.assertEqual(
            WebullAPI._screener_number({"volume": "inf"}, "volume"),
            0.0,
        )

    def test_top_active_stocks_pages_until_limit_or_data_runs_out(self):
        api = WebullAPI.__new__(WebullAPI)
        fake_category = SimpleNamespace(
            US_STOCK=SimpleNamespace(name="US_STOCK"),
        )
        pages = {
            1: [
                {"symbol": "big1", "market_value": "2e11", "volume": "1000"},
                {"symbol": "big2", "market_value": "1.5e11", "volume": "900"},
            ],
            2: [
                {"symbol": "small1", "market_value": "5e8", "volume": "500"},
            ],
        }
        calls = []

        def fake_call(callback, group):
            calls.append(group)
            return callback()

        def fake_get_most_active(**kwargs):
            return pages.get(kwargs["page_index"], [])

        api._call = fake_call
        api.data = SimpleNamespace(
            screener=SimpleNamespace(get_most_active=fake_get_most_active)
        )

        with unittest.mock.patch.dict(
            sys.modules,
            {"webull.data.common.category": SimpleNamespace(Category=fake_category)},
        ):
            universe = api.top_active_stocks(total_limit=10, page_size=2)

        self.assertEqual(set(universe), {"BIG1", "BIG2", "SMALL1"})
        self.assertEqual(universe["BIG1"]["market_value"], 2e11)
        self.assertEqual(universe["SMALL1"]["market_value"], 5e8)
        self.assertTrue(all(call == "market" for call in calls))

    def test_top_active_stocks_stops_at_requested_limit(self):
        api = WebullAPI.__new__(WebullAPI)
        fake_category = SimpleNamespace(
            US_STOCK=SimpleNamespace(name="US_STOCK"),
        )

        def fake_call(callback, group):
            return callback()

        def fake_get_most_active(**kwargs):
            size = kwargs["page_size"]
            index = kwargs["page_index"]
            return [
                {"symbol": f"s{index}-{i}", "market_value": "1e9"}
                for i in range(size)
            ]

        api._call = fake_call
        api.data = SimpleNamespace(
            screener=SimpleNamespace(get_most_active=fake_get_most_active)
        )

        with unittest.mock.patch.dict(
            sys.modules,
            {"webull.data.common.category": SimpleNamespace(Category=fake_category)},
        ):
            universe = api.top_active_stocks(total_limit=5, page_size=2)

        self.assertEqual(len(universe), 5)

    def test_top_gainers_pages_using_change_ratio_screener(self):
        api = WebullAPI.__new__(WebullAPI)
        fake_category = SimpleNamespace(US_STOCK=SimpleNamespace(name="US_STOCK"))
        calls = []

        def fake_call(callback, group):
            calls.append(group)
            return callback()

        def fake_get_gainers_losers(**kwargs):
            self.assertEqual(kwargs["sort_by"], "CHANGE_RATIO")
            self.assertEqual(kwargs["direction"], "DESC")
            if kwargs["page_index"] == 1:
                return [{"symbol": "mover1", "market_value": "5e9", "change_ratio": "12.5"}]
            return []

        api._call = fake_call
        api.data = SimpleNamespace(
            screener=SimpleNamespace(get_gainers_losers=fake_get_gainers_losers)
        )

        with unittest.mock.patch.dict(
            sys.modules,
            {"webull.data.common.category": SimpleNamespace(Category=fake_category)},
        ):
            gainers = api.top_gainers(total_limit=10, page_size=5)

        self.assertEqual(set(gainers), {"MOVER1"})
        self.assertEqual(gainers["MOVER1"]["change_ratio"], 12.5)
        self.assertTrue(all(call == "market" for call in calls))

    def test_top_losers_pages_using_ascending_change_ratio_screener(self):
        api = WebullAPI.__new__(WebullAPI)
        fake_category = SimpleNamespace(US_STOCK=SimpleNamespace(name="US_STOCK"))

        def fake_call(callback, group):
            return callback()

        def fake_get_gainers_losers(**kwargs):
            self.assertEqual(kwargs["sort_by"], "CHANGE_RATIO")
            self.assertEqual(kwargs["direction"], "ASC")
            if kwargs["page_index"] == 1:
                return [{"symbol": "dropper1", "market_value": "5e9", "change_ratio": "-8.2"}]
            return []

        api._call = fake_call
        api.data = SimpleNamespace(
            screener=SimpleNamespace(get_gainers_losers=fake_get_gainers_losers)
        )

        with unittest.mock.patch.dict(
            sys.modules,
            {"webull.data.common.category": SimpleNamespace(Category=fake_category)},
        ):
            losers = api.top_losers(total_limit=10, page_size=5)

        self.assertEqual(set(losers), {"DROPPER1"})
        self.assertEqual(losers["DROPPER1"]["change_ratio"], -8.2)

    def test_filter_with_popular_reinstated_keeps_configured_names(self):
        from webull_bot.bot import AutoTrader

        fake_bot = AutoTrader.__new__(AutoTrader)
        fake_bot.config = SimpleNamespace(
            popular_stocks=lambda: ["AAPL", "MSFT"],
        )
        # Simulate the volatility filter dropping the two well-known,
        # lower-amplitude names while keeping an obscure volatile one.
        fake_bot.filter_by_historical_volatility = lambda candidates: [
            symbol for symbol in candidates if symbol not in ("AAPL", "MSFT")
        ]
        reinstate = AutoTrader.filter_with_popular_reinstated.__get__(fake_bot)

        result = reinstate(["AAPL", "MSFT", "OBSCUREVOL", "OBSCUREFLAT"])

        self.assertEqual(
            result,
            ["AAPL", "MSFT", "OBSCUREVOL", "OBSCUREFLAT"],
        )

    def test_filter_with_popular_reinstated_does_not_add_unavailable_symbols(self):
        from webull_bot.bot import AutoTrader

        fake_bot = AutoTrader.__new__(AutoTrader)
        fake_bot.config = SimpleNamespace(
            popular_stocks=lambda: ["NOTINUNIVERSE"],
        )
        fake_bot.filter_by_historical_volatility = lambda candidates: list(candidates)
        reinstate = AutoTrader.filter_with_popular_reinstated.__get__(fake_bot)

        result = reinstate(["ONE", "TWO"])

        self.assertEqual(result, ["ONE", "TWO"])

    def test_safe_top_gainers_survives_screener_failure(self):
        from webull_bot.bot import AutoTrader

        fake_bot = AutoTrader.__new__(AutoTrader)

        def boom(*args, **kwargs):
            raise RuntimeError("Webull API error 500: boom")

        fake_bot.api = SimpleNamespace(top_gainers=boom)
        safe_call = AutoTrader.safe_top_gainers.__get__(fake_bot)

        self.assertEqual(safe_call(100, 50), {})

    def test_safe_top_losers_survives_screener_failure(self):
        from webull_bot.bot import AutoTrader

        fake_bot = AutoTrader.__new__(AutoTrader)

        def boom(*args, **kwargs):
            raise RuntimeError("Webull API error 500: boom")

        fake_bot.api = SimpleNamespace(top_losers=boom)
        safe_call = AutoTrader.safe_top_losers.__get__(fake_bot)

        self.assertEqual(safe_call(5, 5), {})

    def test_safe_market_pulse_active_falls_back_to_empty_not_prior_universe(self):
        """market_pulse must stay small on a screener failure, not balloon
        to the whole trading universe.
        """
        from webull_bot.bot import AutoTrader

        fake_bot = AutoTrader.__new__(AutoTrader)

        def boom(*args, **kwargs):
            raise RuntimeError("Webull API error 503: boom")

        fake_bot.api = SimpleNamespace(top_active_stocks=boom)
        fake_bot.stock_symbols = ["OLD1", "OLD2", "OLD3"]
        safe_call = AutoTrader.safe_market_pulse_active.__get__(fake_bot)

        self.assertEqual(safe_call(5, 5), {})

    def test_market_pulse_entries_compacts_screener_rows(self):
        from webull_bot.bot import AutoTrader

        entries = AutoTrader._market_pulse_entries(
            {"NVDA": {"change_ratio": 0.125, "volume": 1_000_000}}
        )
        self.assertEqual(entries, [{"symbol": "NVDA", "chg": 12.5, "vol": 1_000_000}])

    def test_refresh_market_pulse_is_throttled_and_small(self):
        from webull_bot.bot import AutoTrader

        # A fixed monotonic clock, not the real one - refresh_market_pulse
        # compares elapsed-since-last-refresh against MARKET_PULSE_REFRESH_
        # SECONDS, and the real clock's absolute value depends on how long
        # the host has been up (not guaranteed to already be past that
        # threshold on every CI runner), same pattern used elsewhere in
        # this file for time.monotonic()-based throttles.
        now = 100_000.0

        fake_bot = AutoTrader.__new__(AutoTrader)
        fake_bot.config = SimpleNamespace(agent_market_pulse_symbols=2)
        fake_bot.last_market_pulse_refresh = now - 999
        fake_bot.market_pulse_cache = {"gainers": [], "losers": [], "most_active": []}
        fake_bot.strategy = SimpleNamespace(most_active_symbols=set())
        calls = []
        fake_bot.safe_top_gainers = lambda limit, page: (
            calls.append("gainers"),
            {"G1": {"change_ratio": 0.05, "volume": 100}},
        )[1]
        fake_bot.safe_top_losers = lambda limit, page: (
            calls.append("losers"),
            {"L1": {"change_ratio": -0.05, "volume": 200}},
        )[1]
        fake_bot.safe_market_pulse_active = lambda limit, page: (
            calls.append("most_active"),
            {"A1": {"change_ratio": 0.01, "volume": 300}},
        )[1]
        refresh = AutoTrader.refresh_market_pulse.__get__(fake_bot)

        with unittest.mock.patch("time.monotonic", return_value=now):
            refresh()
        self.assertEqual(sorted(calls), ["gainers", "losers", "most_active"])
        self.assertEqual(fake_bot.market_pulse_cache["gainers"][0]["symbol"], "G1")
        self.assertEqual(fake_bot.market_pulse_cache["losers"][0]["symbol"], "L1")
        self.assertEqual(fake_bot.market_pulse_cache["most_active"][0]["symbol"], "A1")
        # Regression coverage: safe_market_pulse_active returns a dict
        # keyed by symbol (not a list of {"symbol": ...} dicts) - reading
        # it the wrong way here would raise instead of silently no-op'ing,
        # since a bare string has no .get().
        self.assertEqual(fake_bot.strategy.most_active_symbols, {"A1"})

        # A second call within MARKET_PULSE_REFRESH_SECONDS makes no new
        # screener calls - this must stay off the ~4x/second poll loop.
        calls.clear()
        with unittest.mock.patch("time.monotonic", return_value=now):
            refresh()
        self.assertEqual(calls, [])

    def test_refresh_agent_discoveries_sources_from_market_pulse(self):
        """agent_popular_symbols must keep working from the deterministic
        screener data even when the research agent itself is disabled -
        that's the whole point of decoupling discovery from the LLM call.
        """
        from webull_bot.bot import AutoTrader

        fake_bot = AutoTrader.__new__(AutoTrader)
        fake_bot.market_agent = None
        fake_bot.stock_symbols = ["NVDA", "TSLA", "AMD"]
        fake_bot.market_pulse_cache = {
            "gainers": [{"symbol": "NVDA", "chg": 5.0, "vol": 1}],
            "losers": [{"symbol": "TSLA", "chg": -3.0, "vol": 1}],
            "most_active": [{"symbol": "UNKNOWN", "chg": 0.1, "vol": 1}],
        }
        fake_bot.refresh_market_pulse = lambda: None
        refresh = AutoTrader.refresh_agent_discoveries.__get__(fake_bot)

        refresh()

        self.assertEqual(fake_bot.agent_popular_symbols, {"NVDA", "TSLA"})


class BrokerConflictTests(unittest.TestCase):
    def test_is_broker_position_conflict_matches_reverse_rejection(self):
        from webull_bot.bot import AutoTrader

        self.assertTrue(
            AutoTrader.is_broker_position_conflict(
                RuntimeError(
                    "HTTP Status: 417, Code: "
                    "OAUTH_OPENAPI_ORDER_NOT_SUPPORT_REVERSE_OPTION, Msg: "
                    "This order cannot be entered because it will reverse "
                    "an existing position."
                )
            )
        )
        self.assertFalse(
            AutoTrader.is_broker_position_conflict(RuntimeError("timeout"))
        )

    def test_handle_broker_conflict_clears_tracking_and_blacklists_symbol(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            broker_conflict_symbols=set(),
            pending_stock_exits={"ASHR"},
            pending_option_exits=set(),
            stop_exit_submitted={"ASHR": 123.0},
            stop_loss_escalated={"ASHR"},
            stop_condition_since={"ASHR": 100.0},
        )
        handle = AutoTrader.handle_broker_conflict.__get__(fake_bot)

        handle("ASHR", RuntimeError("reverse position"))

        self.assertIn("ASHR", fake_bot.broker_conflict_symbols)
        self.assertNotIn("ASHR", fake_bot.pending_stock_exits)
        self.assertNotIn("ASHR", fake_bot.stop_exit_submitted)
        self.assertNotIn("ASHR", fake_bot.stop_loss_escalated)
        self.assertNotIn("ASHR", fake_bot.stop_condition_since)

    def test_is_fractional_trading_not_enabled_matches_account_agreement_rejection(self):
        from webull_bot.bot import AutoTrader

        self.assertTrue(
            AutoTrader.is_fractional_trading_not_enabled(
                RuntimeError(
                    "HTTP Status: 417, Code: "
                    "OAUTH_OPENAPI_OPENAPI_FRACT_VERSION2_ACCOUNT_NOT_TRADE, "
                    "Msg: https://sp.webull.com/agreement/third-party"
                )
            )
        )
        self.assertFalse(
            AutoTrader.is_fractional_trading_not_enabled(RuntimeError("timeout"))
        )

    def test_handle_fractional_trading_not_enabled_disables_it_once(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(fractional_trading_enabled=True)
        handle = AutoTrader.handle_fractional_trading_not_enabled.__get__(fake_bot)

        handle(RuntimeError("FRACT_VERSION2_ACCOUNT_NOT_TRADE"))
        self.assertFalse(fake_bot.fractional_trading_enabled)

        # A second rejection while already disabled shouldn't re-log/re-flip
        # anything - just a no-op guard.
        handle(RuntimeError("FRACT_VERSION2_ACCOUNT_NOT_TRADE"))
        self.assertFalse(fake_bot.fractional_trading_enabled)

    def test_is_fractional_ticker_unsupported_matches_per_security_rejection(self):
        from webull_bot.bot import AutoTrader

        self.assertTrue(
            AutoTrader.is_fractional_ticker_unsupported(
                RuntimeError(
                    "HTTP Status: 417, Code: "
                    "OAUTH_OPENAPI_FRACT_TICKER_DONT_SUPPORT_TRADE, Msg: "
                    "This security is not available for fractional shares "
                    "trading."
                )
            )
        )
        self.assertFalse(
            AutoTrader.is_fractional_ticker_unsupported(RuntimeError("timeout"))
        )
        # Distinct from the account-wide rejection - must not cross-match.
        self.assertFalse(
            AutoTrader.is_fractional_ticker_unsupported(
                RuntimeError("FRACT_VERSION2_ACCOUNT_NOT_TRADE")
            )
        )

    def test_handle_fractional_ticker_unsupported_blacklists_only_that_symbol(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(fractional_unsupported_symbols=set())
        handle = AutoTrader.handle_fractional_ticker_unsupported.__get__(fake_bot)

        handle("XHG", RuntimeError("FRACT_TICKER_DONT_SUPPORT_TRADE"))

        self.assertEqual(fake_bot.fractional_unsupported_symbols, {"XHG"})

        # A different symbol is unaffected.
        self.assertNotIn("AAPL", fake_bot.fractional_unsupported_symbols)

    def test_handle_fractional_ticker_unsupported_is_idempotent(self):
        from webull_bot.bot import AutoTrader
        from webull_bot import bot as bot_module

        fake_bot = SimpleNamespace(fractional_unsupported_symbols={"XHG"})
        handle = AutoTrader.handle_fractional_ticker_unsupported.__get__(fake_bot)

        # No new log line for a symbol already known-unsupported - would
        # otherwise spam a warning every cycle forever.
        with unittest.mock.patch.object(bot_module.log, "warning") as warn:
            handle("XHG", RuntimeError("FRACT_TICKER_DONT_SUPPORT_TRADE"))
        warn.assert_not_called()

    def test_is_short_selling_unsupported_matches_sub_2k_equity_rejection(self):
        from webull_bot.bot import AutoTrader

        self.assertTrue(
            AutoTrader.is_short_selling_unsupported(
                RuntimeError(
                    "HTTP Status: 417, Code: "
                    "OAUTH_OPENAPI_NEW_NO_POSITION_MARGIN_ACCOUNT_CAN_NOT_"
                    "SELL_SHORT_FOR_LT_2K, Msg: You currently have no open "
                    "positions in MZZ. Short selling is not permitted for "
                    "accounts under $2,000 in equity or with an overdue "
                    "Intraday Margin Deficit (IMD)."
                )
            )
        )
        self.assertFalse(
            AutoTrader.is_short_selling_unsupported(RuntimeError("timeout"))
        )
        # Distinct from the other account-wide/per-security rejections -
        # must not cross-match.
        self.assertFalse(
            AutoTrader.is_short_selling_unsupported(
                RuntimeError("FRACT_VERSION2_ACCOUNT_NOT_TRADE")
            )
        )

    def test_handle_short_selling_unsupported_disables_it_once(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(short_selling_supported=True)
        handle = AutoTrader.handle_short_selling_unsupported.__get__(fake_bot)

        handle(RuntimeError("CAN_NOT_SELL_SHORT_FOR_LT_2K"))
        self.assertFalse(fake_bot.short_selling_supported)

        # A second rejection while already disabled shouldn't re-log/
        # re-flip anything - just a no-op guard.
        from webull_bot import bot as bot_module

        with unittest.mock.patch.object(bot_module.log, "error") as err:
            handle(RuntimeError("CAN_NOT_SELL_SHORT_FOR_LT_2K"))
        err.assert_not_called()
        self.assertFalse(fake_bot.short_selling_supported)

    def test_is_symbol_restricted_to_closing_only_matches_the_broker_rejection(self):
        from webull_bot.bot import AutoTrader

        self.assertTrue(
            AutoTrader.is_symbol_restricted_to_closing_only(
                RuntimeError(
                    "HTTP Status: 417, Code: "
                    "OAUTH_OPENAPI_CAN_NOT_CREATE_A_OPEN_ORDER, Msg: This "
                    "symbol is restricted to closing orders only."
                )
            )
        )
        self.assertFalse(
            AutoTrader.is_symbol_restricted_to_closing_only(RuntimeError("timeout"))
        )
        self.assertFalse(
            AutoTrader.is_symbol_restricted_to_closing_only(
                RuntimeError("CAN_NOT_SELL_SHORT_FOR_LT_2K")
            )
        )

    def test_handle_symbol_restricted_to_closing_only_blocks_just_that_symbol(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(entry_restricted_symbols=set())
        handle = AutoTrader.handle_symbol_restricted_to_closing_only.__get__(fake_bot)

        handle("RFAI", RuntimeError("CAN_NOT_CREATE_A_OPEN_ORDER"))
        self.assertIn("RFAI", fake_bot.entry_restricted_symbols)
        self.assertEqual(len(fake_bot.entry_restricted_symbols), 1)

        # A second rejection for the same symbol is a silent no-op.
        from webull_bot import bot as bot_module

        with unittest.mock.patch.object(bot_module.log, "warning") as warn:
            handle("RFAI", RuntimeError("CAN_NOT_CREATE_A_OPEN_ORDER"))
        warn.assert_not_called()

    def test_restriction_blocks_entries_but_not_broker_conflict_symbols(self):
        """entry_restricted_symbols must be a distinct set from
        broker_conflict_symbols - the latter skips a symbol's exit
        management entirely too (see trade_stocks' top-of-loop check),
        which would be exactly backwards for a "closing orders only"
        restriction: exits must keep working normally.
        """
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            entry_restricted_symbols=set(), broker_conflict_symbols=set()
        )
        handle = AutoTrader.handle_symbol_restricted_to_closing_only.__get__(fake_bot)
        handle("RFAI", RuntimeError("CAN_NOT_CREATE_A_OPEN_ORDER"))
        self.assertIn("RFAI", fake_bot.entry_restricted_symbols)
        self.assertNotIn("RFAI", fake_bot.broker_conflict_symbols)


class VolatilityScalpBarSeedTests(unittest.TestCase):
    """Warm-starts a symbol's volatility window from real M1 bars instead
    of waiting on several live snapshot polls - see
    AutoTrader.seed_volatility_windows and WebullAPI.recent_minute_closes.
    """

    def test_seeds_only_symbols_with_an_empty_window(self):
        from webull_bot.bot import AutoTrader

        requested = []

        class FakeApi:
            def recent_minute_closes(self, symbols, category, count):
                requested.append((tuple(sorted(symbols)), category, count))
                return {s: [10.0, 10.1, 9.9] for s in symbols}

        seeded = []
        fake_bot = SimpleNamespace(
            api=FakeApi(),
            stock_categories={"AAA": "US_STOCK", "BBB": "US_ETF"},
            config=SimpleNamespace(volatility_scalp_lookback_samples=20),
            strategy=SimpleNamespace(
                volatility_price_history={"BBB": [5.0]},
                seed_volatility_window=lambda symbol, closes: seeded.append(
                    (symbol, closes)
                ),
                prices={},
            ),
        )
        seed = AutoTrader.seed_volatility_windows.__get__(fake_bot)
        seed(["AAA", "BBB"])
        # BBB already has a window - only AAA should ever be fetched/seeded.
        self.assertEqual(requested, [(("AAA",), "US_STOCK", 20)])
        self.assertEqual(seeded, [("AAA", [10.0, 10.1, 9.9])])
        # Also seeds self.strategy.prices, so a bar-seeded (but not yet
        # live-scanned) symbol is already visible to cohort selection.
        self.assertEqual(fake_bot.strategy.prices, {"AAA": Decimal("9.9")})

    def test_never_overwrites_an_already_live_price(self):
        from webull_bot.bot import AutoTrader

        class FakeApi:
            def recent_minute_closes(self, symbols, category, count):
                return {s: [10.0, 10.1, 9.9] for s in symbols}

        fake_bot = SimpleNamespace(
            api=FakeApi(),
            stock_categories={"AAA": "US_STOCK"},
            config=SimpleNamespace(volatility_scalp_lookback_samples=20),
            strategy=SimpleNamespace(
                volatility_price_history={},
                seed_volatility_window=lambda *a, **k: None,
                # Already has a live-scanned price for AAA - the stale
                # bar close (9.9) must not clobber it.
                prices={"AAA": Decimal("11.25")},
            ),
        )
        seed = AutoTrader.seed_volatility_windows.__get__(fake_bot)
        seed(["AAA"])
        self.assertEqual(fake_bot.strategy.prices, {"AAA": Decimal("11.25")})

    def test_noop_when_everything_is_already_seeded(self):
        from webull_bot.bot import AutoTrader

        class FakeApi:
            def recent_minute_closes(self, *a, **k):
                raise AssertionError("must not fetch bars for an already-seeded symbol")

        fake_bot = SimpleNamespace(
            api=FakeApi(),
            stock_categories={},
            config=SimpleNamespace(volatility_scalp_lookback_samples=20),
            strategy=SimpleNamespace(
                volatility_price_history={"AAA": [10.0]},
                seed_volatility_window=lambda *a, **k: None,
            ),
        )
        seed = AutoTrader.seed_volatility_windows.__get__(fake_bot)
        seed(["AAA"])  # must not raise

    def test_a_failed_category_fetch_does_not_block_other_categories(self):
        from webull_bot.bot import AutoTrader

        class FakeApi:
            def recent_minute_closes(self, symbols, category, count):
                if category == "US_STOCK":
                    raise RuntimeError("boom")
                return {s: [1.0, 1.1] for s in symbols}

        seeded = []
        fake_bot = SimpleNamespace(
            api=FakeApi(),
            stock_categories={"AAA": "US_STOCK", "BBB": "US_ETF"},
            config=SimpleNamespace(volatility_scalp_lookback_samples=20),
            strategy=SimpleNamespace(
                volatility_price_history={},
                seed_volatility_window=lambda symbol, closes: seeded.append(
                    (symbol, closes)
                ),
                prices={},
            ),
        )
        seed = AutoTrader.seed_volatility_windows.__get__(fake_bot)
        with self.assertLogs("webull-bot", level="WARNING"):
            seed(["AAA", "BBB"])
        self.assertEqual(seeded, [("BBB", [1.0, 1.1])])


class VolatilityScalpCohortSelectionTests(unittest.TestCase):
    """select_volatility_scalp_symbols - the daily curated cohort (a
    handful of the cheapest, most volatile names), re-ranked
    periodically from data already collected during normal scanning.
    """

    @staticmethod
    def _fake_bot(prices, stdev_by_symbol, **config_overrides):
        from webull_bot.bot import AutoTrader

        config = dict(
            volatility_scalp_enabled=True,
            volatility_scalp_symbol_count=3,
            volatility_scalp_max_price=Decimal("1.50"),
            volatility_scalp_reselect_seconds=1800,
        )
        config.update(config_overrides)
        fake_bot = SimpleNamespace(
            config=SimpleNamespace(**config),
            last_volatility_symbol_selection=float("-inf"),
            volatility_scalp_symbols=set(),
            volatility_scalp_positions=set(),
            strategy=SimpleNamespace(
                prices=prices,
                realized_volatility_percent=lambda symbol: stdev_by_symbol.get(symbol),
            ),
        )
        fake_bot.select_volatility_scalp_symbols = (
            AutoTrader.select_volatility_scalp_symbols.__get__(fake_bot)
        )
        return fake_bot

    def test_picks_the_top_n_by_volatility_among_symbols_under_the_price_cap(self):
        prices = {
            "WILD": Decimal("0.89"),
            "CALM": Decimal("0.50"),
            "PRICEY": Decimal("50.00"),  # over the cap - excluded regardless of stdev
            "MID": Decimal("1.20"),
            "OTHER": Decimal("0.30"),
        }
        stdev = {
            "WILD": Decimal("0.05"),
            "CALM": Decimal("0.001"),
            "PRICEY": Decimal("0.20"),
            "MID": Decimal("0.03"),
            "OTHER": Decimal("0.02"),
        }
        fake_bot = self._fake_bot(prices, stdev)
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            fake_bot.select_volatility_scalp_symbols()
        self.assertEqual(fake_bot.volatility_scalp_symbols, {"WILD", "MID", "OTHER"})

    def test_excludes_symbols_with_no_volatility_reading_yet(self):
        prices = {"NEW": Decimal("0.80"), "WILD": Decimal("0.89")}
        stdev = {"WILD": Decimal("0.05")}  # NEW has no reading yet
        fake_bot = self._fake_bot(prices, stdev)
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            fake_bot.select_volatility_scalp_symbols()
        self.assertEqual(fake_bot.volatility_scalp_symbols, {"WILD"})

    def test_disabled_in_config_never_selects_anything(self):
        prices = {"WILD": Decimal("0.89")}
        stdev = {"WILD": Decimal("0.05")}
        fake_bot = self._fake_bot(prices, stdev, volatility_scalp_enabled=False)
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            fake_bot.select_volatility_scalp_symbols()
        self.assertEqual(fake_bot.volatility_scalp_symbols, set())

    def test_respects_its_own_reselect_throttle(self):
        prices = {"WILD": Decimal("0.89")}
        stdev = {"WILD": Decimal("0.05")}
        fake_bot = self._fake_bot(prices, stdev)
        fake_bot.last_volatility_symbol_selection = 99.0
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            fake_bot.select_volatility_scalp_symbols()
        # Still 30 minutes (default) away from the next reselect - the
        # cohort should stay untouched (empty, since it started empty).
        self.assertEqual(fake_bot.volatility_scalp_symbols, set())

    def test_an_empty_result_does_not_consume_the_throttle(self):
        """Live incident: the very first call on every startup runs
        before any symbol has been scanned (self.strategy.prices is
        still empty), finds zero candidates, but used to stamp last_
        volatility_symbol_selection anyway - "spending" the throttle on
        a result with no real data behind it and leaving the cohort
        empty for the full 30-minute reselect window before ever trying
        again. An empty-candidates call must leave the throttle
        untouched so the very next cycle (once real data exists) can
        actually select something.
        """
        fake_bot = self._fake_bot({}, {})  # nothing scanned yet
        with unittest.mock.patch("time.monotonic", return_value=0.001):
            fake_bot.select_volatility_scalp_symbols()
        self.assertEqual(fake_bot.last_volatility_symbol_selection, float("-inf"))

        # Now real data exists - must not be throttled out just because
        # almost no time has passed since the (empty) first call.
        fake_bot.strategy.prices = {"WILD": Decimal("0.89")}
        fake_bot.strategy.realized_volatility_percent = lambda symbol: Decimal("0.05")
        with unittest.mock.patch("time.monotonic", return_value=0.002):
            fake_bot.select_volatility_scalp_symbols()
        self.assertEqual(fake_bot.volatility_scalp_symbols, {"WILD"})

    def test_reselecting_can_drop_a_cooled_off_symbol_and_add_a_new_one(self):
        prices = {"OLD": Decimal("0.50"), "NEW": Decimal("0.80")}
        fake_bot = self._fake_bot(
            prices, {"OLD": Decimal("0.05")}, volatility_scalp_symbol_count=1
        )
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            fake_bot.select_volatility_scalp_symbols()
        self.assertEqual(fake_bot.volatility_scalp_symbols, {"OLD"})

        # OLD cools off, NEW heats up, and enough time passes for reselection.
        fake_bot.strategy.realized_volatility_percent = lambda symbol: {
            "OLD": Decimal("0.001"),
            "NEW": Decimal("0.08"),
        }.get(symbol)
        with unittest.mock.patch("time.monotonic", return_value=2000.0):
            fake_bot.select_volatility_scalp_symbols()
        self.assertEqual(fake_bot.volatility_scalp_symbols, {"NEW"})


class PositionPnlTests(StrategyConfigMixin, unittest.TestCase):
    """position_unrealized_pnl is since cost (accumulates for as long as a
    position is held); position_day_pnl is since yesterday's 4pm ET close
    (Webull's day_profit_loss), independent of when the position was
    originally opened - the dashboard's "P&L Today" panel wants the
    latter, not the former, or a position held across several days
    misreports days-old drift as if it happened today.
    """

    def test_unrealized_pnl_uses_the_reported_field_net_of_the_sell_fee(self):
        strategy = TradingStrategy(self.config())
        position = {"unrealized_profit_loss": "5.00"}
        self.assertEqual(strategy.position_unrealized_pnl(position), Decimal("4.98"))

    def test_day_pnl_uses_the_reported_field_net_of_the_sell_fee(self):
        strategy = TradingStrategy(self.config())
        position = {"day_profit_loss": "5.00"}
        self.assertEqual(strategy.position_day_pnl(position), Decimal("4.98"))

    def test_day_pnl_and_unrealized_pnl_diverge_for_a_multi_day_hold(self):
        """The exact shape of the live incident this was built for: a
        position bought several days ago sits on a since-cost gain, but
        was flat today.
        """
        strategy = TradingStrategy(self.config())
        position = {
            "unrealized_profit_loss": "2.32",
            "day_profit_loss": "0.00",
        }
        self.assertEqual(strategy.position_unrealized_pnl(position), Decimal("2.30"))
        self.assertEqual(strategy.position_day_pnl(position), Decimal("-0.02"))

    def test_day_pnl_is_zero_when_unreported_rather_than_a_since_cost_guess(self):
        strategy = TradingStrategy(self.config())
        position = {"unrealized_profit_loss": "5.00", "cost_price": "100", "last_price": "105"}
        self.assertEqual(strategy.position_day_pnl(position), Decimal("0"))

    def test_day_pnl_still_zero_unreported_for_an_explicit_whole_quantity(self):
        strategy = TradingStrategy(self.config())
        position = {"quantity": "3", "unrealized_profit_loss": "5.00"}
        self.assertEqual(strategy.position_day_pnl(position), Decimal("0"))

    def test_day_pnl_falls_back_to_since_cost_for_an_unreported_fractional_position(self):
        """Live complaint: open/daily P&L read wrong for fractional
        holdings. Webull's fractional order type is core-hours-only and
        cannot be held overnight, so a fractional position was always
        opened earlier the same day - since-cost and since-today are the
        same number for it, unlike the multi-day-hold case above where
        that substitution would be wrong.
        """
        strategy = TradingStrategy(self.config())
        position = {"quantity": "0.5", "unrealized_profit_loss": "1.00"}
        self.assertEqual(
            strategy.position_day_pnl(position),
            strategy.position_unrealized_pnl(position),
        )
        self.assertEqual(strategy.position_day_pnl(position), Decimal("0.98"))


class StatusWriterTests(unittest.TestCase):
    def test_write_includes_pending_orders_for_the_dashboard(self):
        path = Path("tests/.generated_status/status.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(path))
            writer.write(
                mode="LIVE",
                buying_power=Decimal("1000"),
                positions=[],
                watchlist=[],
                agent_summary=None,
                paused=False,
                stock_count=10,
                option_count=0,
                pending_orders=[
                    {
                        "order_id": "order-1",
                        "instrument_type": "STOCK",
                        "symbol": "TSLA",
                        "action": "STOP",
                        "limit_price": "99.00",
                        "age_seconds": 12,
                        "cancel_requested": False,
                    }
                ],
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["pending_orders"]), 1)
            self.assertEqual(payload["pending_orders"][0]["symbol"], "TSLA")
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_write_defaults_pending_orders_to_empty_list(self):
        path = Path("tests/.generated_status/status2.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(path))
            writer.write(
                mode="LIVE",
                buying_power=Decimal("1000"),
                positions=[],
                watchlist=[],
                agent_summary=None,
                paused=False,
                stock_count=10,
                option_count=0,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["pending_orders"], [])
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_pnl_today_total_is_realized_plus_open_not_since_cost_unrealized(self):
        """Regression test: pnl_today used to blend the daily-reset
        realized total with a since-cost unrealized figure that could
        span several days for a position held that long - both fields
        under a write() call must actually be "today" scoped.
        """
        path = Path("tests/.generated_status/status10.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(path))
            writer.write(
                mode="LIVE",
                buying_power=Decimal("1000"),
                positions=[],
                watchlist=[],
                agent_summary=None,
                paused=False,
                stock_count=10,
                option_count=0,
                realized_pnl_today=Decimal("3.00"),
                open_pnl_total=Decimal("-1.25"),
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["pnl_today"]["realized"], "3.00")
            self.assertEqual(payload["pnl_today"]["open"], "-1.25")
            self.assertEqual(payload["pnl_today"]["total"], "1.75")
            self.assertNotIn("unrealized", payload["pnl_today"])
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_pnl_today_total_prefers_webulls_own_account_day_pnl(self):
        """Live incident: the bot's own realized+open estimate drifted
        from what Webull's app showed, worst for fractional holdings.
        realized_pnl_today is only ever an at-submission-time estimate
        (record_realized_exit's own docstring: "actual fill price can
        differ slightly"), and drift compounds over many trades on a
        high-frequency account - Webull's own account_day_pnl_total, when
        available, is ground truth and must win over the local sum.

        Second live incident, same day: showing the bot's own
        realized_pnl_today next to a Webull-sourced total produced a
        headline total that visibly didn't match its own breakdown
        (0 + 0.11 != -0.46 in the wild). realized must be backed out as
        total - open instead, so the breakdown always sums to the total
        exactly.
        """
        path = Path("tests/.generated_status/status11.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(path))
            writer.write(
                mode="LIVE",
                buying_power=Decimal("1000"),
                positions=[],
                watchlist=[],
                agent_summary=None,
                paused=False,
                stock_count=10,
                option_count=0,
                realized_pnl_today=Decimal("3.00"),
                open_pnl_total=Decimal("-1.25"),
                account_day_pnl_total=Decimal("-0.22"),
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            # open still shows the bot's own (largely Webull-sourced)
            # per-position breakdown; realized is backed out from the
            # authoritative total instead of the bot's own drifting
            # estimate, so open + realized == total always holds.
            self.assertEqual(payload["pnl_today"]["open"], "-1.25")
            self.assertEqual(payload["pnl_today"]["total"], "-0.22")
            self.assertEqual(payload["pnl_today"]["realized"], "1.03")
            self.assertEqual(
                Decimal(payload["pnl_today"]["realized"])
                + Decimal(payload["pnl_today"]["open"]),
                Decimal(payload["pnl_today"]["total"]),
            )
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_pnl_today_total_falls_back_to_local_sum_when_webull_unreported(self):
        path = Path("tests/.generated_status/status12.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(path))
            writer.write(
                mode="LIVE",
                buying_power=Decimal("1000"),
                positions=[],
                watchlist=[],
                agent_summary=None,
                paused=False,
                stock_count=10,
                option_count=0,
                realized_pnl_today=Decimal("3.00"),
                open_pnl_total=Decimal("-1.25"),
                account_day_pnl_total=None,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["pnl_today"]["total"], "1.75")
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_record_balance_appears_in_the_written_payload(self):
        path = Path("tests/.generated_status/status5.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(path))
            writer.record_balance(Decimal("1234.56"))
            writer.write(
                mode="LIVE",
                buying_power=Decimal("1000"),
                positions=[],
                watchlist=[],
                agent_summary=None,
                paused=False,
                stock_count=10,
                option_count=0,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["balance_history"]), 1)
            self.assertEqual(payload["balance_history"][0]["balance"], "1234.56")
            self.assertIn("time", payload["balance_history"][0])
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_balance_history_survives_a_new_statuswriter_instance(self):
        status_path = Path("tests/.generated_status/status6.json")
        state_path = Path("tests/.generated_status/trade_history6.json")
        shutil.rmtree(status_path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(status_path), state_file=str(state_path))
            writer.record_balance(Decimal("500.25"))

            restarted = StatusWriter(str(status_path), state_file=str(state_path))
            self.assertEqual(len(restarted.balance_history), 1)
            self.assertEqual(restarted.balance_history[0]["balance"], "500.25")
        finally:
            shutil.rmtree(status_path.parent, ignore_errors=True)

    def test_pre_existing_plain_list_state_file_still_loads_as_trades(self):
        """The state file predates balance history and may already exist on
        a running deployment as a bare JSON list of trades (the old
        format) - it must keep loading correctly, just with no balance
        history yet, rather than erroring or silently discarding trades.
        """
        status_path = Path("tests/.generated_status/status7.json")
        state_path = Path("tests/.generated_status/trade_history7.json")
        shutil.rmtree(status_path.parent, ignore_errors=True)
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps([{"symbol": "OLDFMT", "instrument_type": "STOCK"}]),
                encoding="utf-8",
            )
            writer = StatusWriter(str(status_path), state_file=str(state_path))
            self.assertEqual(len(writer.trades), 1)
            self.assertEqual(writer.trades[0]["symbol"], "OLDFMT")
            self.assertEqual(list(writer.balance_history), [])
        finally:
            shutil.rmtree(status_path.parent, ignore_errors=True)

    def test_recorded_trade_history_survives_a_new_statuswriter_instance(self):
        status_path = Path("tests/.generated_status/status3.json")
        state_path = Path("tests/.generated_status/trade_history3.json")
        shutil.rmtree(status_path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(status_path), state_file=str(state_path))
            writer.record_trade(
                "STOCK",
                "TSLA",
                "PROFIT",
                Decimal("101.00"),
                "order-1",
                pnl=Decimal("5.00"),
                entry_price=Decimal("96.00"),
            )

            # A fresh instance (simulating a restart) pointed at the same
            # state file must rehydrate the trade instead of starting empty.
            restarted = StatusWriter(str(status_path), state_file=str(state_path))
            self.assertEqual(len(restarted.trades), 1)
            self.assertEqual(restarted.trades[0]["symbol"], "TSLA")
            self.assertEqual(restarted.trades[0]["entry_price"], "96.00")
        finally:
            shutil.rmtree(status_path.parent, ignore_errors=True)

    def test_statuswriter_without_state_file_starts_empty_and_does_not_persist(self):
        status_path = Path("tests/.generated_status/status4.json")
        shutil.rmtree(status_path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(status_path))
            writer.record_trade("STOCK", "TSLA", "BUY", Decimal("100.00"), "order-1")
            self.assertEqual(len(writer.trades), 1)
            self.assertIsNone(writer.state_path)
        finally:
            shutil.rmtree(status_path.parent, ignore_errors=True)

    def test_recorded_exit_trade_includes_entry_price(self):
        status_path = Path("tests/.generated_status/status5.json")
        shutil.rmtree(status_path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(status_path))
            writer.record_trade(
                "STOCK",
                "TSLA",
                "STOP",
                Decimal("95.00"),
                "order-1",
                pnl=Decimal("-5.00"),
                entry_price=Decimal("100.00"),
            )
            trade = writer.trades[0]
            self.assertEqual(trade["entry_price"], "100.00")
            self.assertEqual(trade["limit_price"], "95.00")
            self.assertEqual(trade["pnl"], "-5.00")
        finally:
            shutil.rmtree(status_path.parent, ignore_errors=True)

    def test_recorded_entry_trade_has_no_entry_price(self):
        status_path = Path("tests/.generated_status/status6.json")
        shutil.rmtree(status_path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(status_path))
            writer.record_trade("STOCK", "TSLA", "BUY", Decimal("100.00"), "order-1")
            self.assertIsNone(writer.trades[0]["entry_price"])
        finally:
            shutil.rmtree(status_path.parent, ignore_errors=True)

    def test_write_excludes_still_pending_orders_from_recent_trades(self):
        """By request: "no pending order should go into the recent
        trades." record_trade writes to self.trades optimistically at
        order-submission time (before it's actually filled) - write()
        now filters the DISPLAYED recent-trades list against whatever's
        passed as still-pending, so a resting (not yet filled, not yet
        cancelled) order shows in pending_orders but not recent_trades
        at the same time.
        """
        status_path = Path("tests/.generated_status/status_pending_filter.json")
        shutil.rmtree(status_path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(status_path))
            writer.record_trade("STOCK", "TSLA", "BUY", Decimal("100.00"), "order-1")
            writer.record_trade("STOCK", "AAPL", "PROFIT", Decimal("50.00"), "order-2")
            writer.write(
                mode="LIVE",
                buying_power=Decimal("1000"),
                positions=[],
                watchlist=[],
                agent_summary=None,
                paused=False,
                stock_count=10,
                option_count=0,
                pending_orders=[
                    {
                        "order_id": "order-1",
                        "instrument_type": "STOCK",
                        "symbol": "TSLA",
                        "action": "BUY",
                        "limit_price": "100.00",
                        "age_seconds": 3,
                        "cancel_requested": False,
                    }
                ],
            )
            payload = json.loads(status_path.read_text(encoding="utf-8"))
            symbols = [trade["symbol"] for trade in payload["recent_trades"]]
            self.assertNotIn("TSLA", symbols)
            self.assertIn("AAPL", symbols)
        finally:
            shutil.rmtree(status_path.parent, ignore_errors=True)

    def test_discard_trade_removes_only_the_matching_order(self):
        """Regression test for a live incident: a cancelled order's
        optimistically-recorded trade-log entry stayed on the dashboard's
        Recent Trades list forever, shown as a completed profit that never
        happened - see AutoTrader.reverse_phantom_exit.
        """
        status_path = Path("tests/.generated_status/status7.json")
        shutil.rmtree(status_path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(status_path))
            writer.record_trade(
                "STOCK", "TSLA", "PROFIT", Decimal("101.00"), "order-1",
                pnl=Decimal("5.00"),
            )
            writer.record_trade(
                "STOCK", "AAPL", "PROFIT", Decimal("201.00"), "order-2",
                pnl=Decimal("3.00"),
            )
            writer.discard_trade("order-1")
            self.assertEqual(len(writer.trades), 1)
            self.assertEqual(writer.trades[0]["order_id"], "order-2")
        finally:
            shutil.rmtree(status_path.parent, ignore_errors=True)

    def test_discard_trade_for_an_unknown_order_id_is_a_safe_noop(self):
        status_path = Path("tests/.generated_status/status8.json")
        shutil.rmtree(status_path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(status_path))
            writer.record_trade(
                "STOCK", "TSLA", "PROFIT", Decimal("101.00"), "order-1",
                pnl=Decimal("5.00"),
            )
            writer.discard_trade("order-does-not-exist")
            self.assertEqual(len(writer.trades), 1)
        finally:
            shutil.rmtree(status_path.parent, ignore_errors=True)

    def test_discarded_trade_stays_gone_after_a_new_statuswriter_instance(self):
        status_path = Path("tests/.generated_status/status9.json")
        state_path = Path("tests/.generated_status/trade_history9.json")
        shutil.rmtree(status_path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(status_path), state_file=str(state_path))
            writer.record_trade(
                "STOCK", "TSLA", "PROFIT", Decimal("101.00"), "order-1",
                pnl=Decimal("5.00"),
            )
            writer.discard_trade("order-1")

            restarted = StatusWriter(str(status_path), state_file=str(state_path))
            self.assertEqual(len(restarted.trades), 0)
        finally:
            shutil.rmtree(status_path.parent, ignore_errors=True)

    def test_rekey_trade_lets_a_later_discard_find_the_repriced_entry(self):
        """Regression test for a live incident: CTRM's PROFIT order was
        cancelled and repriced (see AutoTrader.reprice_resting_exits) -
        the visible Recent Trades entry was still filed under the
        original (now-cancelled) order_id, but the eventual "never
        filled" reversal only ever learns the newest order_id. Without
        rekey_trade, discard_trade(new_order_id) finds nothing to remove,
        and the cancelled order's phantom profit stays on the dashboard
        forever - it sat there for 2.5+ hours before this fix.
        """
        status_path = Path("tests/.generated_status/status11.json")
        shutil.rmtree(status_path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(status_path))
            writer.record_trade(
                "STOCK", "CTRM", "PROFIT", Decimal("2.38"), "order-1",
                pnl=Decimal("0.05"),
            )
            # Two reprices, matching the live incident's cancel-and-
            # replace chain: order-1 -> order-2 -> order-3.
            writer.rekey_trade("order-1", "order-2")
            writer.rekey_trade("order-2", "order-3")
            writer.discard_trade("order-3")
            self.assertEqual(len(writer.trades), 0)
        finally:
            shutil.rmtree(status_path.parent, ignore_errors=True)

    def test_rekey_trade_for_an_unknown_order_id_is_a_safe_noop(self):
        status_path = Path("tests/.generated_status/status12.json")
        shutil.rmtree(status_path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(status_path))
            writer.record_trade(
                "STOCK", "CTRM", "PROFIT", Decimal("2.38"), "order-1",
                pnl=Decimal("0.05"),
            )
            writer.rekey_trade("order-does-not-exist", "order-2")
            self.assertEqual(len(writer.trades), 1)
            self.assertEqual(writer.trades[0]["order_id"], "order-1")
        finally:
            shutil.rmtree(status_path.parent, ignore_errors=True)


class DashboardCommandTests(unittest.TestCase):
    def test_command_queue_round_trip(self):
        path = Path("tests/.generated_commands/commands.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            queue = CommandQueue(str(path))
            self.assertEqual(queue.pop_all(), [])
            command_id = queue.enqueue("close_all")
            self.assertTrue(command_id)
            popped = queue.pop_all()
            self.assertEqual(len(popped), 1)
            self.assertEqual(popped[0]["type"], "close_all")
            self.assertEqual(popped[0]["id"], command_id)
            self.assertEqual(queue.pop_all(), [])
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_command_queue_accumulates_multiple_commands(self):
        path = Path("tests/.generated_commands/commands2.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            queue = CommandQueue(str(path))
            queue.enqueue("sell", symbol="TSLA", instrument_type="EQUITY")
            queue.enqueue("watchlist_add", symbol="AAPL")
            popped = queue.pop_all()
            self.assertEqual([c["type"] for c in popped], ["sell", "watchlist_add"])
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_process_ui_commands_dispatches_close_all(self):
        from webull_bot.bot import AutoTrader

        calls = []
        fake_bot = SimpleNamespace(
            commands=SimpleNamespace(pop_all=lambda: [{"type": "close_all"}]),
            close_instruments=lambda types: calls.append(types),
        )
        process = AutoTrader.process_ui_commands.__get__(fake_bot)
        process([])

        self.assertEqual(calls, [{"EQUITY", "OPTION"}])

    def test_process_ui_commands_survives_unknown_type_and_handler_error(self):
        from webull_bot.bot import AutoTrader

        def boom(command, positions, core_session_active=False):
            raise RuntimeError("boom")

        fake_bot = SimpleNamespace(
            commands=SimpleNamespace(
                pop_all=lambda: [{"type": "unknown"}, {"type": "sell"}]
            ),
            _manual_sell=boom,
        )
        process = AutoTrader.process_ui_commands.__get__(fake_bot)
        process([])  # must not raise despite the unknown type and handler error

    def test_manual_cancel_order_cancels_a_tracked_working_order(self):
        from webull_bot.bot import AutoTrader

        cancelled = []
        fake_bot = SimpleNamespace(
            working_orders={
                "order-1": {
                    "key": "STOCK:TSLA",
                    "action": "STOP",
                    "cancel_requested_at": None,
                }
            },
            api=SimpleNamespace(cancel=lambda order_id: cancelled.append(order_id)),
        )
        cancel = AutoTrader._manual_cancel_order.__get__(fake_bot)

        cancel({"order_id": "order-1"})

        self.assertEqual(cancelled, ["order-1"])
        self.assertIsNotNone(
            fake_bot.working_orders["order-1"]["cancel_requested_at"]
        )

    def test_manual_cancel_order_skips_unknown_or_already_requested(self):
        from webull_bot.bot import AutoTrader

        cancelled = []
        fake_bot = SimpleNamespace(
            working_orders={
                "order-2": {
                    "key": "STOCK:AAPL",
                    "action": "PROFIT",
                    "cancel_requested_at": 123.0,
                }
            },
            api=SimpleNamespace(cancel=lambda order_id: cancelled.append(order_id)),
        )
        cancel = AutoTrader._manual_cancel_order.__get__(fake_bot)

        cancel({"order_id": "does-not-exist"})
        cancel({"order_id": "order-2"})  # already cancel-requested

        self.assertEqual(cancelled, [])

    def test_manual_buy_sizes_by_dollars_during_core_session(self):
        from webull_bot.bot import AutoTrader

        placed = []
        config = Settings(
            stock_core_session_position_fraction=Decimal("0.10"),
            max_order_notional=Decimal("1000"),
            max_open_positions=10,
        )
        fake_bot = SimpleNamespace(
            config=config,
            broker_conflict_symbols=set(),
            wash_sales=SimpleNamespace(blocked_until=lambda symbol: None),
            fractional_trading_enabled=True,
            position_buckets={},
            strategy=SimpleNamespace(
                open_position_count=TradingStrategy.open_position_count,
                update_stock_snapshot=lambda quote, price: None,
                dollar_stock_quantity=TradingStrategy.dollar_stock_quantity.__get__(
                    TradingStrategy(config)
                ),
            ),
            api=SimpleNamespace(
                stock_position=lambda symbol, positions: (Decimal("0"), Decimal("0")),
                stock_quote=lambda symbol: {"bid": "49.90", "ask": "50.10"},
                quote_price=lambda quote: Decimal("50.00"),
                stock_limit_price=lambda quote, side: Decimal("50.00"),
                place_stock=lambda symbol, side, quantity, limit_price=None, fractional=False: (
                    placed.append((symbol, side, quantity, fractional)) or "order-1"
                ),
            ),
        )
        recorded = []
        fake_bot.record_trade = lambda *a, **k: recorded.append((a, k))
        manual_buy = AutoTrader._manual_buy.__get__(fake_bot)

        remaining = manual_buy(
            {"symbol": "MSFT"}, [], Decimal("1000"), True
        )

        self.assertEqual(len(placed), 1)
        symbol, side, quantity, fractional = placed[0]
        self.assertEqual((symbol, side), ("MSFT", "BUY"))
        self.assertTrue(fractional)
        # 10% of $1000 = $100 target notional at ~$50/share -> ~2 shares,
        # well above fractional_stock_quantity's old 1-share cap.
        self.assertGreater(quantity, Decimal("1"))
        self.assertLess(remaining, Decimal("1000"))
        self.assertEqual(fake_bot.position_buckets.get("MSFT"), "MANUAL")
        # Regression coverage: a manual buy's dashboard row must show the
        # price paid, not a blank Entry column.
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0][1].get("entry_price"), Decimal("50.00"))

    def test_manual_buy_skips_when_already_holding_a_position(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            broker_conflict_symbols=set(),
            api=SimpleNamespace(
                stock_position=lambda symbol, positions: (Decimal("5"), Decimal("40")),
            ),
        )
        manual_buy = AutoTrader._manual_buy.__get__(fake_bot)

        remaining = manual_buy({"symbol": "MSFT"}, [], Decimal("1000"))

        self.assertEqual(remaining, Decimal("1000"))

    def test_process_ui_commands_dispatches_buy_and_threads_buying_power(self):
        from webull_bot.bot import AutoTrader

        calls = []
        fake_bot = SimpleNamespace(
            commands=SimpleNamespace(
                pop_all=lambda: [{"type": "buy", "symbol": "MSFT"}]
            ),
            _manual_buy=lambda command, positions, buying_power, core_session_active: (
                calls.append((command, buying_power, core_session_active))
                or Decimal("42")
            ),
        )
        process = AutoTrader.process_ui_commands.__get__(fake_bot)

        result = process([], Decimal("1000"), True)

        self.assertEqual(result, Decimal("42"))
        self.assertEqual(calls, [({"type": "buy", "symbol": "MSFT"}, Decimal("1000"), True)])

    def test_manual_sell_prices_at_the_ask_outside_core_session(self):
        from webull_bot.bot import AutoTrader

        placed = []
        recorded_pnl = []

        fake_bot = SimpleNamespace(
            pending_stock_exits=set(),
            pending_option_exits=set(),
            fractional_trading_enabled=True,
            is_fractional_quantity=AutoTrader.is_fractional_quantity,
            api=SimpleNamespace(
                stock_quote=lambda symbol: {"bid": "99.00", "ask": "99.20"},
                quote_ask=lambda quote: Decimal(str(quote["ask"])),
                stock_limit_price=lambda quote, side: Decimal("99.00"),
                place_stock=lambda symbol, side, quantity, limit_price=None, fractional=False, market=False: (
                    placed.append((symbol, side, quantity, limit_price, fractional, market))
                    or "order-1"
                ),
            ),
            wash_sales=SimpleNamespace(block=lambda symbol, reason: None),
        )
        fake_bot.record_realized_exit = lambda cost, price, qty, multiplier=1: (
            recorded_pnl.append((cost, price, qty)) or (price - cost) * qty * multiplier
        )
        fake_bot.record_trade = lambda *a, **k: None
        manual_sell = AutoTrader._manual_sell.__get__(fake_bot)

        positions = [
            {
                "instrument_type": "EQUITY",
                "symbol": "TSLA",
                "quantity": "3",
                "cost_price": "100.00",
            }
        ]
        # core_session_active defaults to False - a top-of-spread LIMIT
        # order, not a MARKET order, since MARKET orders outside core
        # hours aren't reliably supported.
        manual_sell({"symbol": "TSLA", "instrument_type": "EQUITY"}, positions)

        self.assertEqual(
            placed,
            [("TSLA", "SELL", Decimal("3"), Decimal("99.20"), False, False)],
        )
        self.assertIn("TSLA", fake_bot.pending_stock_exits)
        self.assertEqual(recorded_pnl, [(Decimal("100.00"), Decimal("99.20"), Decimal("3"))])

    def test_manual_sell_uses_a_market_order_during_core_session(self):
        from webull_bot.bot import AutoTrader

        placed = []

        fake_bot = SimpleNamespace(
            pending_stock_exits=set(),
            pending_option_exits=set(),
            fractional_trading_enabled=True,
            is_fractional_quantity=AutoTrader.is_fractional_quantity,
            api=SimpleNamespace(
                stock_quote=lambda symbol: {"bid": "99.00", "ask": "99.20"},
                quote_ask=lambda quote: Decimal(str(quote["ask"])),
                stock_limit_price=lambda quote, side: Decimal("99.00"),
                place_stock=lambda symbol, side, quantity, limit_price=None, fractional=False, market=False: (
                    placed.append((symbol, side, quantity, limit_price, fractional, market))
                    or "order-1"
                ),
            ),
            wash_sales=SimpleNamespace(block=lambda symbol, reason: None),
        )
        fake_bot.record_realized_exit = lambda cost, price, qty, multiplier=1: (
            price - cost
        ) * qty * multiplier
        fake_bot.record_trade = lambda *a, **k: None
        manual_sell = AutoTrader._manual_sell.__get__(fake_bot)

        positions = [
            {
                "instrument_type": "EQUITY",
                "symbol": "TSLA",
                "quantity": "3",
                "cost_price": "100.00",
            }
        ]
        manual_sell(
            {"symbol": "TSLA", "instrument_type": "EQUITY"},
            positions,
            True,
        )

        self.assertEqual(
            placed,
            [("TSLA", "SELL", Decimal("3"), None, False, True)],
        )

    def test_manual_sell_of_a_fractional_position_never_uses_market(self):
        """A fractional-quantity position must still go through the
        fractional order machinery (MARKET+CORE forced by fractional=True
        already) rather than the plain market=True path, even during core
        hours - the two paths shouldn't both try to force MARKET at once.
        """
        from webull_bot.bot import AutoTrader

        placed = []

        fake_bot = SimpleNamespace(
            pending_stock_exits=set(),
            pending_option_exits=set(),
            fractional_trading_enabled=True,
            is_fractional_quantity=AutoTrader.is_fractional_quantity,
            api=SimpleNamespace(
                stock_quote=lambda symbol: {"bid": "99.00", "ask": "99.20"},
                quote_ask=lambda quote: Decimal(str(quote["ask"])),
                stock_limit_price=lambda quote, side: Decimal("99.00"),
                place_stock=lambda symbol, side, quantity, limit_price=None, fractional=False, market=False: (
                    placed.append((symbol, side, quantity, limit_price, fractional, market))
                    or "order-1"
                ),
            ),
            wash_sales=SimpleNamespace(block=lambda symbol, reason: None),
        )
        fake_bot.record_realized_exit = lambda cost, price, qty, multiplier=1: (
            price - cost
        ) * qty * multiplier
        fake_bot.record_trade = lambda *a, **k: None
        manual_sell = AutoTrader._manual_sell.__get__(fake_bot)

        positions = [
            {
                "instrument_type": "EQUITY",
                "symbol": "TSLA",
                "quantity": "2.5",
                "cost_price": "100.00",
            }
        ]
        manual_sell(
            {"symbol": "TSLA", "instrument_type": "EQUITY"},
            positions,
            True,
        )

        placed_symbol, placed_side, placed_qty, placed_limit, placed_fractional, placed_market = (
            placed[0]
        )
        self.assertTrue(placed_fractional)
        self.assertFalse(placed_market)

    def test_manual_sell_skips_when_no_matching_position(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(pending_stock_exits=set(), pending_option_exits=set())
        manual_sell = AutoTrader._manual_sell.__get__(fake_bot)

        manual_sell({"symbol": "TSLA", "instrument_type": "EQUITY"}, [])

    def test_add_to_watchlist_resolves_category_and_appends_symbol(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            user_watchlist=set(),
            stock_categories={},
            stock_symbols=["AAPL"],
            priority_scan_symbols=set(),
            api=SimpleNamespace(stock_categories=lambda symbols: {"TSLA": "US_STOCK"}),
        )
        add = AutoTrader.add_to_watchlist.__get__(fake_bot)

        add("tsla")

        self.assertIn("TSLA", fake_bot.user_watchlist)
        self.assertEqual(fake_bot.stock_categories.get("TSLA"), "US_STOCK")
        self.assertIn("TSLA", fake_bot.stock_symbols)
        # Regression coverage: a freshly-added symbol has zero
        # accumulated activity score and can otherwise lose out to every
        # already-active watchlist symbol in prioritized_stock_batch's
        # ranking forever - live incident, HOWL never once got scanned
        # after being added. Must be queued for a guaranteed first scan.
        self.assertIn("TSLA", fake_bot.priority_scan_symbols)


class WashSaleTrackerTests(unittest.TestCase):
    def _tracker(self, path, block_days):
        return WashSaleTracker(str(path), block_days, timezone.utc, logging.getLogger("test-wash"))

    def test_block_then_blocked_until_reflects_configured_days(self):
        path = Path("tests/.generated_wash/blocks.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            tracker = self._tracker(path, 31)
            until = tracker.block("AAPL", "stop-loss exit submitted")
            expected = datetime.now(timezone.utc) + timedelta(days=31)
            self.assertLess(abs((until - expected).total_seconds()), 5)
            self.assertIsNotNone(tracker.blocked_until("AAPL"))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_block_expires_after_configured_days(self):
        path = Path("tests/.generated_wash/expired.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            tracker = self._tracker(path, 31)
            blocked_at = datetime.now(timezone.utc) - timedelta(days=32)
            tracker.blocks["AAPL"] = {"blocked_at": blocked_at.isoformat()}
            self.assertIsNone(tracker.blocked_until("AAPL"))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_legacy_string_format_is_migrated_and_shortened_to_new_day_count(self):
        """A block written under the old fixed 60-day rule must be
        re-evaluated against the new (lower) WASH_SALE_BLOCK_DAYS as soon as
        the tracker loads it, not frozen at whatever the old rule computed.
        """
        path = Path("tests/.generated_wash/legacy.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Legacy entry: a loss 40 days ago, blocked for the old 60 days,
            # so the stored "until" is 20 days in the future.
            legacy_until = datetime.now(timezone.utc) + timedelta(days=20)
            path.write_text(
                json.dumps({"AAPL": legacy_until.isoformat()}),
                encoding="utf-8",
            )

            tracker = self._tracker(path, 31)

            # Under the new 31-day rule, a loss 40 days ago is already past
            # its block window (40 > 31), so it should be gone, not still
            # blocked for another 20 days.
            self.assertIsNone(tracker.blocked_until("AAPL"))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_legacy_migration_keeps_a_still_active_block_shortened_not_dropped(self):
        path = Path("tests/.generated_wash/legacy_active.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Legacy entry: a loss 10 days ago, blocked for the old 60 days,
            # so the stored "until" is 50 days in the future.
            legacy_until = datetime.now(timezone.utc) + timedelta(days=50)
            path.write_text(
                json.dumps({"AAPL": legacy_until.isoformat()}),
                encoding="utf-8",
            )

            tracker = self._tracker(path, 31)

            # Under the new 31-day rule, a loss 10 days ago is still blocked
            # for 21 more days (31 - 10), not the old 50.
            until = tracker.blocked_until("AAPL")
            self.assertIsNotNone(until)
            expected = datetime.now(timezone.utc) + timedelta(days=21)
            self.assertLess(abs((until - expected).total_seconds()), 5)
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_changing_block_days_after_load_immediately_changes_blocked_until(self):
        path = Path("tests/.generated_wash/dynamic.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            tracker = self._tracker(path, 60)
            tracker.block("TSLA", "manual sell at a loss")
            far_future = tracker.blocked_until("TSLA")

            tracker.block_days = 31
            nearer_future = tracker.blocked_until("TSLA")

            self.assertLess(nearer_future, far_future)
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)


class DailyPnlTrackerTests(unittest.TestCase):
    def _tracker(self, path):
        return DailyPnlTracker(str(path), timezone.utc, logging.getLogger("test-daily-pnl"))

    def test_fresh_file_starts_at_zero(self):
        path = Path("tests/.generated_daily_pnl/fresh.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            tracker = self._tracker(path)
            self.assertEqual(tracker.realized_pnl, Decimal("0"))
            self.assertEqual(tracker.realized_loss, Decimal("0"))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_record_then_new_instance_loads_the_same_totals(self):
        path = Path("tests/.generated_daily_pnl/roundtrip.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            tracker = self._tracker(path)
            tracker.record(Decimal("42.50"), Decimal("10.00"))

            reloaded = self._tracker(path)
            self.assertEqual(reloaded.realized_pnl, Decimal("42.50"))
            self.assertEqual(reloaded.realized_loss, Decimal("10.00"))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_stale_date_does_not_carry_over_to_a_new_day(self):
        path = Path("tests/.generated_daily_pnl/stale.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
            path.write_text(
                json.dumps(
                    {
                        "date": yesterday,
                        "realized_pnl": "500.00",
                        "realized_loss": "50.00",
                    }
                ),
                encoding="utf-8",
            )

            tracker = self._tracker(path)

            self.assertEqual(tracker.realized_pnl, Decimal("0"))
            self.assertEqual(tracker.realized_loss, Decimal("0"))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_reset_zeroes_and_persists(self):
        path = Path("tests/.generated_daily_pnl/reset.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            tracker = self._tracker(path)
            tracker.record(Decimal("30.00"), Decimal("5.00"))

            tracker.reset()

            self.assertEqual(tracker.realized_pnl, Decimal("0"))
            self.assertEqual(tracker.realized_loss, Decimal("0"))
            reloaded = self._tracker(path)
            self.assertEqual(reloaded.realized_pnl, Decimal("0"))
            self.assertEqual(reloaded.realized_loss, Decimal("0"))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_corrupt_file_logs_warning_and_starts_fresh(self):
        path = Path("tests/.generated_daily_pnl/corrupt.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("not json", encoding="utf-8")

            with self.assertLogs("test-daily-pnl", level="WARNING"):
                tracker = self._tracker(path)

            self.assertEqual(tracker.realized_pnl, Decimal("0"))
            self.assertEqual(tracker.realized_loss, Decimal("0"))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)


class OrderBookImbalanceTests(unittest.TestCase):
    def test_obi_supports_entry_passes_through_when_no_data(self):
        self.assertTrue(TradingStrategy.obi_supports_entry(None))

    def test_obi_supports_entry_blocks_below_threshold(self):
        self.assertFalse(TradingStrategy.obi_supports_entry(Decimal("0.40")))

    def test_obi_supports_entry_allows_at_or_above_threshold(self):
        self.assertTrue(TradingStrategy.obi_supports_entry(Decimal("0.60")))
        self.assertTrue(TradingStrategy.obi_supports_entry(Decimal("0.75")))

    def test_depth_imbalance_computes_ratio_from_bids_asks_shape(self):
        depth = {
            "bids": [
                {"price": "10.00", "volume": "300"},
                {"price": "9.99", "volume": "200"},
            ],
            "asks": [
                {"price": "10.01", "volume": "100"},
                {"price": "10.02", "volume": "100"},
            ],
        }
        score = WebullAPI.depth_imbalance(depth, 2)
        self.assertEqual(score, Decimal("500") / Decimal("700"))

    def test_depth_imbalance_tries_alternate_key_shapes(self):
        depth = {"bidList": [{"size": "50"}], "askList": [{"size": "50"}]}
        score = WebullAPI.depth_imbalance(depth, 5)
        self.assertEqual(score, Decimal("0.5"))

    def test_depth_imbalance_returns_none_for_empty_or_missing_depth(self):
        self.assertIsNone(WebullAPI.depth_imbalance(None, 5))
        self.assertIsNone(WebullAPI.depth_imbalance({}, 5))
        self.assertIsNone(
            WebullAPI.depth_imbalance({"bids": [], "asks": []}, 5)
        )

    def test_stock_depth_latches_unsupported_on_permission_error(self):
        calls = []

        def fake_call(callback, group):
            calls.append(group)
            raise RuntimeError(
                "Webull API error 403: unauthorized, please subscribe for "
                "permission"
            )

        fake_api = SimpleNamespace(_call=fake_call)
        depth_fn = WebullAPI.stock_depth.__get__(fake_api)
        self.assertIsNone(depth_fn("AAPL", "US_STOCK"))
        self.assertTrue(fake_api._depth_unsupported)
        # Second call must short-circuit without hitting the API again.
        self.assertIsNone(depth_fn("AAPL", "US_STOCK"))
        self.assertEqual(len(calls), 1)

    def test_stock_depth_latches_on_any_error_not_just_permission_denied(self):
        # Regression test: this endpoint can fail with a plain 500
        # INTERNAL_ERROR on an account without the entitlement, not a
        # clean permission-denied response. stock_depth must swallow that
        # too - a raised exception here previously escaped all the way out
        # of the BUY gate in trade_stocks() and aborted the whole symbol's
        # entry for the cycle, so no order could ever place.
        calls = []

        def fake_call(callback, group):
            calls.append(group)
            raise RuntimeError("HTTP Status: 500, Code: INTERNAL_ERROR, Msg: ")

        fake_api = SimpleNamespace(_call=fake_call)
        depth_fn = WebullAPI.stock_depth.__get__(fake_api)
        self.assertIsNone(depth_fn("CVX", "US_STOCK"))
        self.assertTrue(fake_api._depth_unsupported)
        self.assertIsNone(depth_fn("CVX", "US_STOCK"))
        self.assertEqual(len(calls), 1)


class PairsStrategyTests(unittest.TestCase):
    @staticmethod
    def _seeded(values):
        strat = PairsStrategy()
        pair = ("A", "B")
        strat._spread_history[pair].extend(values)
        return strat, pair

    @staticmethod
    def _expected_z(values):
        mean = statistics.mean(values)
        stdev = statistics.pstdev(values)
        return Decimal(str((values[-1] - mean) / stdev))

    def test_no_data_below_minimum_samples(self):
        strat, pair = self._seeded([0.001] * (PAIRS_MIN_SAMPLES - 1))
        decision = strat.decision(pair, is_open=False)
        self.assertEqual(decision.action, "NO_DATA")

    def test_no_data_when_history_is_perfectly_flat(self):
        # stdev == 0 must not raise a division error - just no signal.
        strat, pair = self._seeded([0.01] * (PAIRS_MIN_SAMPLES + 10))
        decision = strat.decision(pair, is_open=False)
        self.assertEqual(decision.action, "NO_DATA")
        self.assertIsNone(decision.z_score)

    def test_enters_long_b_short_a_when_a_rich(self):
        values = [0.0] * 40 + [0.05]
        strat, pair = self._seeded(values)
        expected_z = self._expected_z(values)
        self.assertGreaterEqual(expected_z, PAIRS_ENTRY_Z)
        decision = strat.decision(pair, is_open=False)
        self.assertEqual(decision.action, "ENTER_LONG_B_SHORT_A")
        self.assertAlmostEqual(
            float(decision.z_score), float(expected_z), places=9
        )

    def test_enters_long_a_short_b_when_b_rich(self):
        values = [0.0] * 40 + [-0.05]
        strat, pair = self._seeded(values)
        decision = strat.decision(pair, is_open=False)
        self.assertEqual(decision.action, "ENTER_LONG_A_SHORT_B")

    def test_no_entry_when_spread_within_normal_range(self):
        values = [0.001, -0.001] * 20 + [0.0005]
        strat, pair = self._seeded(values)
        expected_z = self._expected_z(values)
        self.assertLess(abs(expected_z), PAIRS_ENTRY_Z)
        decision = strat.decision(pair, is_open=False)
        self.assertEqual(decision.action, "NO_DATA")

    def test_stop_when_open_and_spread_keeps_diverging(self):
        values = [0.0] * 40 + [0.5]
        strat, pair = self._seeded(values)
        expected_z = self._expected_z(values)
        self.assertGreaterEqual(abs(expected_z), PAIRS_STOP_Z)
        strat.mark_entered(pair)
        decision = strat.decision(pair, is_open=True)
        self.assertEqual(decision.action, "STOP")

    def test_unwind_when_open_and_spread_reverted(self):
        # A volatile-then-flat spread: 60 samples oscillating +/-0.002 (a
        # real, nonzero stdev to revert from), then a final sample back at
        # the set's own mean - a genuine "it came back" shape, not just a
        # quiet history that never moved.
        values = [0.002, -0.002] * 30 + [0.0]
        strat, pair = self._seeded(values)
        expected_z = self._expected_z(values)
        self.assertLessEqual(abs(expected_z), PAIRS_EXIT_Z)
        strat.mark_entered(pair)
        decision = strat.decision(pair, is_open=True)
        self.assertEqual(decision.action, "UNWIND")

    def test_unwind_after_max_hold_time_regardless_of_z(self):
        # A moderate z (between PAIRS_EXIT_Z and PAIRS_STOP_Z) that would
        # otherwise just HOLD - only the max-hold override should move it.
        values = [0.002, -0.002] * 20 + [0.004]
        strat, pair = self._seeded(values)
        expected_z = self._expected_z(values)
        self.assertGreater(abs(expected_z), PAIRS_EXIT_Z)
        self.assertLess(abs(expected_z), PAIRS_STOP_Z)
        strat.mark_entered(pair)
        without_override = strat.decision(pair, is_open=True)
        self.assertEqual(without_override.action, "HOLD")
        strat._entered_at[pair] = (
            time.monotonic() - (PAIRS_MAX_HOLD_MINUTES + 1) * 60
        )
        decision = strat.decision(pair, is_open=True)
        self.assertEqual(decision.action, "UNWIND")
        self.assertIn("max hold", decision.reason)

    def test_mark_exited_clears_entry_time(self):
        strat, pair = self._seeded([0.0] * PAIRS_MIN_SAMPLES)
        strat.mark_entered(pair)
        strat.mark_exited(pair)
        self.assertNotIn(pair, strat._entered_at)


class AccountStateCashReserveTests(unittest.TestCase):
    def test_fresh_refresh_subtracts_the_reserve_once(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                account_refresh_seconds=Decimal("5"),
                min_cash_reserve_dollars=Decimal("10"),
            ),
            api=SimpleNamespace(
                balance=lambda: {},
                buying_power_from_balance=lambda balance: Decimal("120.00"),
                account_day_pnl_from_balance=lambda balance: Decimal("-1.50"),
                account_value_from_balance=lambda balance: Decimal("500.00"),
                positions=lambda: [{"symbol": "AAPL"}],
            ),
            cached_buying_power=Decimal("0"),
            cached_raw_buying_power=Decimal("0"),
            cached_account_day_pnl=None,
            cached_positions=[],
            last_account_refresh=0.0,
            short_selling_supported=True,
        )
        account_state = AutoTrader.account_state.__get__(fake_bot)

        with self.assertLogs("webull-bot", level="WARNING"):
            buying_power, positions = account_state()

        self.assertEqual(buying_power, Decimal("110.00"))
        self.assertEqual(positions, [{"symbol": "AAPL"}])
        self.assertEqual(fake_bot.cached_account_day_pnl, Decimal("-1.50"))
        self.assertEqual(fake_bot.cached_account_value, Decimal("500.00"))
        # The reserve reduces cached_buying_power (used for trading
        # sizing) but not cached_raw_buying_power (used for the
        # dashboard's displayed figures) - showing the reserved-down
        # number there reads as a silent gap against Webull's own app.
        self.assertEqual(fake_bot.cached_raw_buying_power, Decimal("120.00"))

    def test_cache_hit_does_not_subtract_the_reserve_again(self):
        """Regression test: account_state() only re-fetches from the
        broker every ACCOUNT_REFRESH_SECONDS - subtracting the reserve at
        every call site that reads the cached value (instead of once,
        right at the fresh fetch) would compound on every poll cycle
        within that window and drive spendable capital toward zero almost
        immediately.
        """
        from webull_bot.bot import AutoTrader

        calls = []
        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                account_refresh_seconds=Decimal("5"),
                min_cash_reserve_dollars=Decimal("10"),
            ),
            api=SimpleNamespace(
                buying_power=lambda: (calls.append(1), Decimal("120.00"))[1],
                positions=lambda: [],
            ),
            cached_buying_power=Decimal("0"),
            cached_positions=[],
            last_account_refresh=time.monotonic(),
        )
        account_state = AutoTrader.account_state.__get__(fake_bot)

        buying_power, _ = account_state()

        self.assertEqual(buying_power, Decimal("0"))
        self.assertEqual(calls, [])

    def test_reserve_never_takes_buying_power_below_zero(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                account_refresh_seconds=Decimal("5"),
                min_cash_reserve_dollars=Decimal("10"),
            ),
            api=SimpleNamespace(
                balance=lambda: {},
                buying_power_from_balance=lambda balance: Decimal("3.00"),
                account_day_pnl_from_balance=lambda balance: None,
                account_value_from_balance=lambda balance: None,
                positions=lambda: [],
            ),
            cached_buying_power=Decimal("0"),
            cached_raw_buying_power=Decimal("0"),
            cached_account_day_pnl=None,
            cached_positions=[],
            last_account_refresh=0.0,
            short_selling_supported=True,
        )
        account_state = AutoTrader.account_state.__get__(fake_bot)

        buying_power, _ = account_state()

        self.assertEqual(buying_power, Decimal("0"))


class ShortSellingEquityGateTests(unittest.TestCase):
    """account_state proactively disables short selling once cached
    account equity is seen under Webull's own $2,000 minimum, instead of
    spending a live order attempt (certain to be rejected) to discover
    it - see SHORT_SELLING_MIN_EQUITY.
    """

    @staticmethod
    def _fake_bot(account_value, short_selling_supported=True):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                account_refresh_seconds=Decimal("5"),
                min_cash_reserve_dollars=Decimal("10"),
            ),
            api=SimpleNamespace(
                balance=lambda: {},
                buying_power_from_balance=lambda balance: Decimal("120.00"),
                account_day_pnl_from_balance=lambda balance: Decimal("0"),
                account_value_from_balance=lambda balance: account_value,
                positions=lambda: [],
            ),
            cached_buying_power=Decimal("0"),
            cached_raw_buying_power=Decimal("0"),
            cached_account_day_pnl=None,
            cached_positions=[],
            last_account_refresh=0.0,
            short_selling_supported=short_selling_supported,
        )
        fake_bot.account_state = AutoTrader.account_state.__get__(fake_bot)
        return fake_bot

    def test_disables_short_selling_when_equity_is_under_the_minimum(self):
        fake_bot = self._fake_bot(Decimal("500.00"))
        with self.assertLogs("webull-bot", level="WARNING") as logs:
            fake_bot.account_state()
        self.assertFalse(fake_bot.short_selling_supported)
        self.assertIn("500.00", logs.output[0])

    def test_leaves_short_selling_enabled_when_equity_clears_the_minimum(self):
        fake_bot = self._fake_bot(Decimal("5000.00"))
        from webull_bot import bot as bot_module

        with unittest.mock.patch.object(bot_module.log, "warning") as warn:
            fake_bot.account_state()
        warn.assert_not_called()
        self.assertTrue(fake_bot.short_selling_supported)

    def test_does_not_relog_once_already_disabled(self):
        fake_bot = self._fake_bot(Decimal("500.00"), short_selling_supported=False)
        from webull_bot import bot as bot_module

        with unittest.mock.patch.object(bot_module.log, "warning") as warn:
            fake_bot.account_state()
        warn.assert_not_called()

    def test_no_crash_when_account_value_is_unavailable(self):
        fake_bot = self._fake_bot(None)
        fake_bot.account_state()  # must not raise
        self.assertTrue(fake_bot.short_selling_supported)


class StopLossGuardTests(unittest.TestCase):
    @staticmethod
    def _fake_bot(recent_stop_losses=None, **config_overrides):
        from webull_bot.bot import AutoTrader

        defaults = dict(
            stop_loss_guard_enabled=True,
            stop_loss_guard_trade_limit=4,
            stop_loss_guard_lookback_seconds=1200,
            stop_loss_guard_cooldown_seconds=600,
        )
        defaults.update(config_overrides)
        config = SimpleNamespace(**defaults)
        fake_bot = SimpleNamespace(
            config=config,
            recent_stop_losses=deque(recent_stop_losses or []),
            stop_loss_guard_until=0.0,
        )
        return AutoTrader.stop_loss_guard_active.__get__(fake_bot), fake_bot

    def test_passes_through_below_the_trade_limit(self):
        now = time.monotonic()
        guard, _ = self._fake_bot([now - 10, now - 20, now - 30])
        self.assertFalse(guard())

    def test_trips_at_the_trade_limit_within_lookback(self):
        now = time.monotonic()
        guard, fake_bot = self._fake_bot([now - 10, now - 20, now - 30, now - 40])

        self.assertTrue(guard())
        # Once tripped, stays active for the cooldown even without any new
        # stop-losses - the whole point is to pause NEW entries, so the
        # very next call (same cycle or the next one) must still see it.
        self.assertTrue(guard())

    def test_old_stop_losses_outside_lookback_dont_count(self):
        now = time.monotonic()
        guard, _ = self._fake_bot(
            [now - 5000, now - 4000, now - 3000, now - 2000],
            stop_loss_guard_lookback_seconds=1200,
        )
        self.assertFalse(guard())

    def test_disabled_by_config(self):
        now = time.monotonic()
        guard, _ = self._fake_bot(
            [now - 10, now - 20, now - 30, now - 40],
            stop_loss_guard_enabled=False,
        )
        self.assertFalse(guard())

    def test_resumes_automatically_after_the_cooldown_elapses(self):
        """Unlike handle_portfolio_circuit_breaker (which stays paused
        until re-evaluated/manually resumed), the stop-loss guard is a
        pure rolling-window check - once the tripping stops age out of
        the lookback (which happens well before a short cooldown elapses
        in this test), it clears on its own.
        """
        from webull_bot.bot import AutoTrader

        config = SimpleNamespace(
            stop_loss_guard_enabled=True,
            stop_loss_guard_trade_limit=4,
            stop_loss_guard_lookback_seconds=1,
            stop_loss_guard_cooldown_seconds=1,
        )
        now = time.monotonic()
        fake_bot = SimpleNamespace(
            config=config,
            recent_stop_losses=deque([now - 0.9, now - 0.8, now - 0.7, now - 0.6]),
            stop_loss_guard_until=0.0,
        )
        guard = AutoTrader.stop_loss_guard_active.__get__(fake_bot)
        self.assertTrue(guard())

        # Simulate the cooldown having elapsed and the old stops now being
        # well outside the (short, 1s) lookback window too.
        fake_bot.stop_loss_guard_until = time.monotonic() - 10
        fake_bot.recent_stop_losses = deque(
            t - 20 for t in fake_bot.recent_stop_losses
        )
        self.assertFalse(guard())

    def test_record_trade_appends_a_stop_but_not_other_actions(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            last_trade={},
            last_exit_at={},
            trade_times=defaultdict(deque),
            working_orders={},
            status=SimpleNamespace(record_trade=lambda *a, **k: None),
            last_capital_deployed_at=0.0,
            recent_stop_losses=deque(),
            position_opened_at={},
            symbol_pnl_history=defaultdict(deque),
            submitted_order_ids_today=set(),
        )
        record_trade = AutoTrader.record_trade.__get__(fake_bot)

        record_trade("STOCK:AAPL", "order-1", "PROFIT", Decimal("10"), Decimal("1"), Decimal("9"))
        self.assertEqual(len(fake_bot.recent_stop_losses), 0)

        record_trade("STOCK:AAPL", "order-2", "STOP", Decimal("9"), Decimal("-1"), Decimal("10"))
        self.assertEqual(len(fake_bot.recent_stop_losses), 1)


class SymbolQuarantineTests(unittest.TestCase):
    KEY = "STOCK:AAPL"

    @classmethod
    def _fake_bot(cls, pnl_history=None, key=None, **config_overrides):
        from webull_bot.bot import AutoTrader

        defaults = dict(
            symbol_quarantine_enabled=True,
            symbol_quarantine_lookback_seconds=1800,
            symbol_quarantine_min_trades=3,
            symbol_quarantine_loss_dollars=Decimal("0.50"),
            symbol_quarantine_cooldown_seconds=900,
        )
        defaults.update(config_overrides)
        config = SimpleNamespace(**defaults)
        history = defaultdict(deque)
        if pnl_history is not None:
            history[key or cls.KEY] = deque(pnl_history)
        fake_bot = SimpleNamespace(
            config=config,
            symbol_pnl_history=history,
            symbol_quarantine_until={},
        )
        return AutoTrader.symbol_quarantined.__get__(fake_bot), fake_bot

    def test_passes_through_with_no_history(self):
        quarantined, _ = self._fake_bot(None)
        self.assertFalse(quarantined(self.KEY))

    def test_passes_through_below_min_trades(self):
        now = time.monotonic()
        quarantined, _ = self._fake_bot(
            [(now - 10, Decimal("-1")), (now - 20, Decimal("-1"))]
        )
        self.assertFalse(quarantined(self.KEY))

    def test_trips_when_net_loss_meets_threshold_within_lookback(self):
        now = time.monotonic()
        quarantined, _ = self._fake_bot(
            [
                (now - 10, Decimal("-0.20")),
                (now - 20, Decimal("-0.20")),
                (now - 30, Decimal("-0.20")),
            ]
        )
        self.assertTrue(quarantined(self.KEY))
        # Once tripped, stays quarantined for the cooldown even without a
        # new loss - same shape as stop_loss_guard_active.
        self.assertTrue(quarantined(self.KEY))

    def test_net_loss_under_threshold_does_not_trip(self):
        now = time.monotonic()
        quarantined, _ = self._fake_bot(
            [
                (now - 10, Decimal("-0.10")),
                (now - 20, Decimal("-0.10")),
                (now - 30, Decimal("-0.10")),
            ]
        )
        self.assertFalse(quarantined(self.KEY))

    def test_profitable_trades_dont_trip_even_at_the_trade_count(self):
        now = time.monotonic()
        quarantined, _ = self._fake_bot(
            [
                (now - 10, Decimal("5")),
                (now - 20, Decimal("-1")),
                (now - 30, Decimal("-1")),
            ]
        )
        self.assertFalse(quarantined(self.KEY))

    def test_old_trades_outside_lookback_dont_count(self):
        now = time.monotonic()
        quarantined, _ = self._fake_bot(
            [
                (now - 5000, Decimal("-1")),
                (now - 4000, Decimal("-1")),
                (now - 3000, Decimal("-1")),
            ],
            symbol_quarantine_lookback_seconds=1800,
        )
        self.assertFalse(quarantined(self.KEY))

    def test_disabled_by_config(self):
        now = time.monotonic()
        quarantined, _ = self._fake_bot(
            [
                (now - 10, Decimal("-1")),
                (now - 20, Decimal("-1")),
                (now - 30, Decimal("-1")),
            ],
            symbol_quarantine_enabled=False,
        )
        self.assertFalse(quarantined(self.KEY))

    def test_other_symbols_are_unaffected(self):
        now = time.monotonic()
        quarantined, _ = self._fake_bot(
            [
                (now - 10, Decimal("-1")),
                (now - 20, Decimal("-1")),
                (now - 30, Decimal("-1")),
            ]
        )
        self.assertTrue(quarantined(self.KEY))
        self.assertFalse(quarantined("STOCK:MSFT"))

    def test_record_trade_partitions_pnl_history_per_key(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            last_trade={},
            last_exit_at={},
            trade_times=defaultdict(deque),
            working_orders={},
            status=SimpleNamespace(record_trade=lambda *a, **k: None),
            last_capital_deployed_at=0.0,
            recent_stop_losses=deque(),
            position_opened_at={},
            symbol_pnl_history=defaultdict(deque),
            submitted_order_ids_today=set(),
        )
        record_trade = AutoTrader.record_trade.__get__(fake_bot)

        record_trade(
            "STOCK:AAPL", "order-1", "STOP", Decimal("9"),
            pnl=Decimal("-1"), entry_price=Decimal("10"),
        )
        record_trade(
            "STOCK:MSFT", "order-2", "PROFIT", Decimal("11"),
            pnl=Decimal("2"), entry_price=Decimal("10"),
        )

        self.assertEqual(len(fake_bot.symbol_pnl_history["STOCK:AAPL"]), 1)
        self.assertEqual(fake_bot.symbol_pnl_history["STOCK:AAPL"][0][1], Decimal("-1"))
        self.assertEqual(len(fake_bot.symbol_pnl_history["STOCK:MSFT"]), 1)
        self.assertEqual(fake_bot.symbol_pnl_history["STOCK:MSFT"][0][1], Decimal("2"))


class TimeAwareStopTests(StrategyConfigMixin, unittest.TestCase):
    def test_disabled_leaves_the_normal_adaptive_percent_unchanged(self):
        strategy = TradingStrategy(self.config())
        normal = strategy.adaptive_stop_percent("AAPL")
        widened = strategy.adaptive_stop_percent("AAPL", seconds_since_entry=1)
        self.assertEqual(normal, widened)

    def test_widens_within_the_grace_window_when_enabled(self):
        config = self.config()
        config.time_aware_stop_enabled = True
        config.time_aware_stop_widen_seconds = 60
        config.time_aware_stop_widen_multiplier = Decimal("2")
        strategy = TradingStrategy(config)

        normal = strategy.adaptive_stop_percent("AAPL")
        widened = strategy.adaptive_stop_percent("AAPL", seconds_since_entry=10)

        self.assertEqual(widened, normal * Decimal("2"))

    def test_tightens_back_to_normal_after_the_grace_window(self):
        config = self.config()
        config.time_aware_stop_enabled = True
        config.time_aware_stop_widen_seconds = 60
        config.time_aware_stop_widen_multiplier = Decimal("2")
        strategy = TradingStrategy(config)

        normal = strategy.adaptive_stop_percent("AAPL")
        aged = strategy.adaptive_stop_percent("AAPL", seconds_since_entry=120)

        self.assertEqual(aged, normal)

    def test_a_fresh_entry_survives_a_dip_that_would_otherwise_stop_it_out(self):
        config = self.config()
        config.time_aware_stop_enabled = True
        config.time_aware_stop_widen_seconds = 60
        config.time_aware_stop_widen_multiplier = Decimal("3")
        strategy = TradingStrategy(config)
        strategy.metrics["AAPL"] = {"range_ratio": Decimal("0")}

        cost = Decimal("100")
        # Exactly at the *normal* (unwidened) stop line - trips a normal
        # stop, but sits comfortably inside a 3x-widened one.
        stop_percent = strategy.adaptive_stop_percent("AAPL")
        dip_price = cost * (Decimal("1") - stop_percent)

        fresh = strategy.stock_decision(
            "STOCK:AAPL", dip_price, 10, cost, seconds_since_entry=5
        )
        aged = strategy.stock_decision(
            "STOCK:AAPL", dip_price, 10, cost, seconds_since_entry=120
        )

        self.assertNotEqual(fresh.action, "LOSS")
        self.assertEqual(aged.action, "LOSS")


class RegimeGateTests(unittest.TestCase):
    def test_passes_through_with_no_current_reading(self):
        history = deque([Decimal(x) for x in range(1, 21)])
        self.assertTrue(
            TradingStrategy.stock_market_regime_ok(history, None, Decimal("0.85"))
        )

    def test_passes_through_without_enough_history(self):
        history = deque([Decimal("1"), Decimal("2")])
        self.assertTrue(
            TradingStrategy.stock_market_regime_ok(
                history, Decimal("2"), Decimal("0.85")
            )
        )

    def test_rejects_when_current_is_in_the_top_of_its_own_range(self):
        history = deque([Decimal(x) for x in range(1, 21)])
        self.assertFalse(
            TradingStrategy.stock_market_regime_ok(
                history, Decimal("20"), Decimal("0.85")
            )
        )

    def test_allows_when_current_is_mid_range(self):
        history = deque([Decimal(x) for x in range(1, 21)])
        self.assertTrue(
            TradingStrategy.stock_market_regime_ok(
                history, Decimal("5"), Decimal("0.85")
            )
        )

    def test_uses_its_own_configurable_percentile_distinct_from_options(self):
        # 15th of 20 samples => rank 0.75 - rejected at a strict 0.70 gate,
        # allowed at a looser 0.90 one, proving the threshold is genuinely
        # parameterized rather than hardcoded to OPTION_VIXY_REJECT_PERCENTILE.
        history = deque([Decimal(x) for x in range(1, 21)])
        self.assertFalse(
            TradingStrategy.stock_market_regime_ok(
                history, Decimal("15"), Decimal("0.70")
            )
        )
        self.assertTrue(
            TradingStrategy.stock_market_regime_ok(
                history, Decimal("15"), Decimal("0.90")
            )
        )

    def test_disabled_regime_gate_active_is_always_false(self):
        """AutoTrader.trade_stocks only computes regime_gate_active at all
        when REGIME_GATE_ENABLED is set - mirrors guard_active's own
        config-gated pattern.
        """
        config = SimpleNamespace(
            regime_gate_enabled=False,
            regime_gate_reject_percentile=Decimal("0.85"),
        )
        vixy_history = deque([Decimal(x) for x in range(1, 21)])
        regime_gate_active = config.regime_gate_enabled and not (
            TradingStrategy.stock_market_regime_ok(
                vixy_history, vixy_history[-1], config.regime_gate_reject_percentile
            )
        )
        self.assertFalse(regime_gate_active)


class IdleCashRelaxationTests(unittest.TestCase):
    def test_ramp_progress_is_zero_within_the_grace_period(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                idle_cash_relaxation_enabled=True,
                idle_cash_grace_seconds=300,
                idle_cash_ramp_seconds=1800,
            ),
            last_capital_deployed_at=time.monotonic() - 60,
        )
        progress = AutoTrader.idle_cash_ramp_progress.__get__(fake_bot)

        self.assertEqual(progress(Decimal("50")), Decimal("0"))

    def test_ramp_progress_is_zero_with_no_spendable_cash(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                idle_cash_relaxation_enabled=True,
                idle_cash_grace_seconds=300,
                idle_cash_ramp_seconds=1800,
            ),
            last_capital_deployed_at=time.monotonic() - 99999,
        )
        progress = AutoTrader.idle_cash_ramp_progress.__get__(fake_bot)

        self.assertEqual(progress(Decimal("0")), Decimal("0"))

    def test_ramp_progress_climbs_linearly_after_grace_then_caps_at_one(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                idle_cash_relaxation_enabled=True,
                idle_cash_grace_seconds=300,
                idle_cash_ramp_seconds=1800,
            ),
            last_capital_deployed_at=time.monotonic() - 300 - 900,
        )
        progress = AutoTrader.idle_cash_ramp_progress.__get__(fake_bot)

        self.assertAlmostEqual(float(progress(Decimal("50"))), 0.5, places=2)

        fake_bot.last_capital_deployed_at = time.monotonic() - 300 - 999999
        self.assertEqual(progress(Decimal("50")), Decimal("1"))

    def test_ramp_progress_disabled_by_config(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                idle_cash_relaxation_enabled=False,
                idle_cash_grace_seconds=0,
                idle_cash_ramp_seconds=1800,
            ),
            last_capital_deployed_at=time.monotonic() - 999999,
        )
        progress = AutoTrader.idle_cash_ramp_progress.__get__(fake_bot)

        self.assertEqual(progress(Decimal("50")), Decimal("0"))

    def test_record_trade_resets_the_idle_cash_timer_on_a_new_entry(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            last_trade={},
            last_exit_at={},
            trade_times=defaultdict(deque),
            working_orders={},
            status=SimpleNamespace(record_trade=lambda *a, **k: None),
            last_capital_deployed_at=time.monotonic() - 999999,
            position_opened_at={},
            symbol_pnl_history=defaultdict(deque),
            consecutive_exit_failures=defaultdict(int),
            submitted_order_ids_today=set(),
        )
        record_trade = AutoTrader.record_trade.__get__(fake_bot)

        record_trade("STOCK:AAPL", "order-1", "BUY")

        self.assertGreater(fake_bot.last_capital_deployed_at, time.monotonic() - 1)

    def test_a_volatility_scalp_buy_does_not_reset_the_general_strategys_idle_timer(self):
        """By request, after finding buying power sitting idle: the
        idle-cash ramp only ever loosens the GENERAL strategy's own
        entry gates, but a volatility-scalp fill (firing every few
        minutes) was resetting this same clock anyway, starving the
        general strategy's gates from ever relaxing even while its own
        capital pool sat unused for hours.
        """
        from webull_bot.bot import AutoTrader

        stale = time.monotonic() - 999999
        fake_bot = SimpleNamespace(
            last_trade={},
            last_exit_at={},
            trade_times=defaultdict(deque),
            working_orders={},
            status=SimpleNamespace(record_trade=lambda *a, **k: None),
            last_capital_deployed_at=stale,
            position_opened_at={},
            symbol_pnl_history=defaultdict(deque),
            consecutive_exit_failures=defaultdict(int),
            submitted_order_ids_today=set(),
        )
        record_trade = AutoTrader.record_trade.__get__(fake_bot)

        record_trade(
            "STOCK:GAUZ",
            "order-1",
            "BUY",
            counts_toward_idle_cash_ramp=False,
        )

        self.assertEqual(fake_bot.last_capital_deployed_at, stale)

    def test_record_trade_does_not_reset_the_timer_on_an_exit(self):
        from webull_bot.bot import AutoTrader

        stale = time.monotonic() - 999999
        fake_bot = SimpleNamespace(
            last_trade={},
            last_exit_at={},
            trade_times=defaultdict(deque),
            working_orders={},
            status=SimpleNamespace(record_trade=lambda *a, **k: None),
            last_capital_deployed_at=stale,
            position_opened_at={},
            symbol_pnl_history=defaultdict(deque),
            submitted_order_ids_today=set(),
        )
        record_trade = AutoTrader.record_trade.__get__(fake_bot)

        record_trade("STOCK:AAPL", "order-1", "PROFIT", Decimal("10"), Decimal("1"), Decimal("9"))

        self.assertEqual(fake_bot.last_capital_deployed_at, stale)


class ExecutionGuardrailTests(unittest.TestCase):
    def test_price_sanity_ok_within_tolerance(self):
        from webull_bot.bot import AutoTrader

        check = AutoTrader.price_sanity_ok.__get__(
            SimpleNamespace(price_sanity_rejected_at={})
        )
        self.assertTrue(check("AAPL", Decimal("100.00"), Decimal("103.00")))

    def test_price_sanity_rejects_large_deviation(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(price_sanity_rejected_at={})
        check = AutoTrader.price_sanity_ok.__get__(fake_bot)
        with self.assertLogs("webull-bot", level="ERROR") as logs:
            self.assertFalse(check("AAPL", Decimal("100.00"), Decimal("110.00")))
        self.assertIn("AAPL", logs.output[0])
        self.assertIn("AAPL", fake_bot.price_sanity_rejected_at)

    def test_entry_price_sanity_cooldown_blocks_a_recent_rejection(self):
        """Live incident: one illiquid symbol's quote sat just past
        price_sanity_ok's tolerance and got retried (and re-rejected) on
        essentially every scan cycle for hours - nothing backed it off.
        """
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(price_sanity_cooldown_seconds=30),
            price_sanity_rejected_at={"AAPL": 100.0},
        )
        ready = AutoTrader.price_sanity_cooldown_ready.__get__(fake_bot)
        with unittest.mock.patch("time.monotonic", return_value=110.0):
            self.assertFalse(ready("AAPL"))

    def test_entry_price_sanity_cooldown_clears_after_the_window(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(price_sanity_cooldown_seconds=30),
            price_sanity_rejected_at={"AAPL": 100.0},
        )
        ready = AutoTrader.price_sanity_cooldown_ready.__get__(fake_bot)
        with unittest.mock.patch("time.monotonic", return_value=131.0):
            self.assertTrue(ready("AAPL"))

    def test_price_sanity_cooldown_ready_for_a_never_rejected_symbol(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(price_sanity_cooldown_seconds=30),
            price_sanity_rejected_at={},
        )
        ready = AutoTrader.price_sanity_cooldown_ready.__get__(fake_bot)
        self.assertTrue(ready("AAPL"))

    def test_entry_price_sanity_cooldown_is_per_symbol(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(price_sanity_cooldown_seconds=30),
            price_sanity_rejected_at={"AAPL": 100.0},
        )
        ready = AutoTrader.price_sanity_cooldown_ready.__get__(fake_bot)
        with unittest.mock.patch("time.monotonic", return_value=110.0):
            self.assertTrue(ready("MSFT"))

    @staticmethod
    def _fake_bot_for_order_errors():
        return SimpleNamespace(
            order_error_times=deque(),
            broker_conflict_symbols=set(),
            pending_stock_exits=set(),
            pending_option_exits=set(),
            stop_exit_submitted={},
            stop_loss_escalated=set(),
            stop_condition_since={},
        )

    def test_record_order_error_blacklists_only_the_offending_symbol(self):
        """Regression test: this used to trip a global kill switch that
        halted every symbol's entries AND exits until the process was
        restarted - in production, a single symbol stuck in a broker-side
        rejection (Webull's $0.10-$0.999 lot-size rule) repeatedly tripped
        this and froze the entire bot over a problem confined to one
        symbol. Must now blacklist only that symbol (reusing
        broker_conflict_symbols, which every entry path already checks),
        leaving every other symbol unaffected.
        """
        from webull_bot.bot import AutoTrader, CONSECUTIVE_ORDER_ERROR_LIMIT

        fake_bot = self._fake_bot_for_order_errors()
        record = AutoTrader.record_order_error.__get__(fake_bot)
        with self.assertLogs("webull-bot", level="CRITICAL"):
            for _ in range(CONSECUTIVE_ORDER_ERROR_LIMIT):
                record("OPTT", RuntimeError("boom"))
        self.assertIn("OPTT", fake_bot.broker_conflict_symbols)
        self.assertFalse(hasattr(fake_bot, "order_kill_switch_tripped"))

    def test_record_order_error_does_not_trip_below_threshold(self):
        from webull_bot.bot import AutoTrader, CONSECUTIVE_ORDER_ERROR_LIMIT

        fake_bot = self._fake_bot_for_order_errors()
        record = AutoTrader.record_order_error.__get__(fake_bot)
        for _ in range(CONSECUTIVE_ORDER_ERROR_LIMIT - 1):
            record("TEST", RuntimeError("boom"))
        self.assertEqual(fake_bot.broker_conflict_symbols, set())

    def test_record_order_error_prunes_entries_outside_window(self):
        from webull_bot.bot import AutoTrader, ORDER_ERROR_WINDOW_SECONDS

        fake_bot = self._fake_bot_for_order_errors()
        record = AutoTrader.record_order_error.__get__(fake_bot)
        fake_bot.order_error_times.append(
            time.monotonic() - ORDER_ERROR_WINDOW_SECONDS - 5
        )
        record("TEST", RuntimeError("boom"))
        self.assertEqual(len(fake_bot.order_error_times), 1)

    @staticmethod
    def _fake_bot_for_placement(placed, price="10.00"):
        from webull_bot.bot import AutoTrader
        from webull_bot.strategy import TradingStrategy

        class FakeApi:
            @staticmethod
            def quote_price(q):
                return Decimal(str(q["price"]))

            @staticmethod
            def stock_limit_price(q, side):
                return Decimal(str(q["price"]))

            def place_stock(self, symbol, side, quantity, limit_price=None, fractional=False):
                placed.append((symbol, side, quantity, limit_price, fractional))
                return "order-1"

        fake_bot = SimpleNamespace(
            api=FakeApi(),
            iceberg_orders={},
            price_sanity_rejected_at={},
            config=SimpleNamespace(price_sanity_cooldown_seconds=30),
            strategy=SimpleNamespace(minimum_lot_size=TradingStrategy.minimum_lot_size),
        )
        fake_bot.price_sanity_ok = AutoTrader.price_sanity_ok.__get__(fake_bot)
        fake_bot.price_sanity_cooldown_ready = AutoTrader.price_sanity_cooldown_ready.__get__(fake_bot)
        fake_bot.record_order_error = AutoTrader.record_order_error.__get__(fake_bot)
        return fake_bot

    def test_place_stock_scaled_below_threshold_places_single_order(self):
        from webull_bot.bot import AutoTrader, ICEBERG_MIN_SHARES

        placed = []
        fake_bot = self._fake_bot_for_placement(placed)
        place = AutoTrader.place_stock_scaled.__get__(fake_bot)
        quantity = ICEBERG_MIN_SHARES - 1
        order_id = place("AAA", "BUY", quantity, "STOCK:AAA", {"price": "10.00"})
        self.assertEqual(order_id, "order-1")
        self.assertEqual(len(placed), 1)
        self.assertEqual(placed[0][2], quantity)
        self.assertNotIn("AAA:BUY", fake_bot.iceberg_orders)

    def test_place_stock_scaled_at_threshold_slices_and_registers_remainder(self):
        from webull_bot.bot import (
            AutoTrader,
            ICEBERG_MIN_SHARES,
            ICEBERG_SLICE_SHARES,
        )

        placed = []
        fake_bot = self._fake_bot_for_placement(placed)
        place = AutoTrader.place_stock_scaled.__get__(fake_bot)
        total_qty = ICEBERG_MIN_SHARES + 25
        order_id = place("AAA", "BUY", total_qty, "STOCK:AAA", {"price": "10.00"})
        self.assertEqual(order_id, "order-1")
        self.assertEqual(placed[0][2], Decimal(ICEBERG_SLICE_SHARES))
        entry = fake_bot.iceberg_orders["AAA:BUY"]
        self.assertEqual(entry["remaining"], total_qty - ICEBERG_SLICE_SHARES)

    def test_place_stock_scaled_never_slices_below_the_lot_restricted_minimum(self):
        """Live incident: HOWL, priced in the $0.10-$0.999 band, sized to
        a 100+ share order (Webull's own minimum there) but iceberg-sliced
        into 10-share clips - every clip individually below the 100-share
        floor, so every single slice got rejected with 417
        OAUTH_OPENAPI_CANT_TRADE_FOR_PRICE_BETWEEN_0099_AND_0999. A
        lot-restricted order must go out whole, never sliced.
        """
        from webull_bot.bot import AutoTrader, ICEBERG_MIN_SHARES

        placed = []
        fake_bot = self._fake_bot_for_placement(placed)
        place = AutoTrader.place_stock_scaled.__get__(fake_bot)
        total_qty = ICEBERG_MIN_SHARES + 50
        order_id = place("HOWL", "BUY", total_qty, "STOCK:HOWL", {"price": "0.30"})
        self.assertEqual(order_id, "order-1")
        self.assertEqual(len(placed), 1)
        self.assertEqual(placed[0][2], total_qty)
        self.assertNotIn("HOWL:BUY", fake_bot.iceberg_orders)

    def test_place_stock_scaled_clamps_to_hard_notional_ceiling(self):
        from webull_bot.bot import AutoTrader

        placed = []
        fake_bot = self._fake_bot_for_placement(placed)
        place = AutoTrader.place_stock_scaled.__get__(fake_bot)
        # 10-share slice at $250 = $2500, over the $2000 ceiling -> clamps
        # to floor(2000/250) = 8 shares instead.
        order_id = place("AAA", "BUY", 100, "STOCK:AAA", {"price": "250"})
        self.assertEqual(order_id, "order-1")
        self.assertEqual(placed[0][2], Decimal("8"))

    def test_place_stock_scaled_returns_none_on_price_sanity_failure(self):
        from webull_bot.bot import AutoTrader

        placed = []

        class BadPriceApi:
            @staticmethod
            def quote_price(q):
                return Decimal(str(q["price"]))

            @staticmethod
            def stock_limit_price(q, side):
                return Decimal("999")

            def place_stock(self, *a, **k):
                placed.append((a, k))
                return "order-1"

        fake_bot = SimpleNamespace(
            api=BadPriceApi(),
            iceberg_orders={},
            price_sanity_rejected_at={},
            config=SimpleNamespace(price_sanity_cooldown_seconds=30),
        )
        fake_bot.price_sanity_ok = AutoTrader.price_sanity_ok.__get__(fake_bot)
        fake_bot.price_sanity_cooldown_ready = AutoTrader.price_sanity_cooldown_ready.__get__(fake_bot)
        fake_bot.record_order_error = AutoTrader.record_order_error.__get__(fake_bot)
        place = AutoTrader.place_stock_scaled.__get__(fake_bot)
        with self.assertLogs("webull-bot", level="ERROR"):
            order_id = place("AAA", "BUY", 5, "STOCK:AAA", {"price": "10.00"})
        self.assertIsNone(order_id)
        self.assertEqual(placed, [])

    def test_process_iceberg_orders_places_next_slice_after_interval(self):
        from webull_bot.bot import (
            AutoTrader,
            ICEBERG_SLICE_INTERVAL_SECONDS,
            ICEBERG_SLICE_SHARES,
        )
        from webull_bot.strategy import TradingStrategy

        placed = []

        class FakeApi:
            def stock_quote(self, symbol):
                return {"symbol": symbol, "bid": "10.00", "ask": "10.02", "price": "10.01"}

            @staticmethod
            def quote_price(q):
                return Decimal(str(q["price"]))

            @staticmethod
            def stock_limit_price(q, side):
                return Decimal(str(q["price"]))

            def place_stock(self, symbol, side, quantity, limit_price=None):
                placed.append((symbol, side, quantity, limit_price))
                return "order-2"

        fake_bot = SimpleNamespace(
            api=FakeApi(),
            iceberg_orders={
                "AAA:BUY": {
                    "symbol": "AAA",
                    "side": "BUY",
                    "key": "STOCK:AAA",
                    "remaining": Decimal("15"),
                    "last_slice_at": time.monotonic()
                    - ICEBERG_SLICE_INTERVAL_SECONDS
                    - 1,
                }
            },
            price_sanity_rejected_at={},
            strategy=SimpleNamespace(minimum_lot_size=TradingStrategy.minimum_lot_size),
        )
        fake_bot.price_sanity_ok = AutoTrader.price_sanity_ok.__get__(fake_bot)
        fake_bot.record_order_error = AutoTrader.record_order_error.__get__(fake_bot)
        recorded = []
        fake_bot.record_trade = (
            lambda key, order_id, action, entry_price=None, quantity=None: recorded.append(
                (key, order_id, action, entry_price)
            )
        )
        process = AutoTrader.process_iceberg_orders.__get__(fake_bot)
        process()
        self.assertEqual(len(placed), 1)
        self.assertEqual(placed[0][2], Decimal(ICEBERG_SLICE_SHARES))
        self.assertEqual(
            fake_bot.iceberg_orders["AAA:BUY"]["remaining"],
            Decimal("15") - Decimal(ICEBERG_SLICE_SHARES),
        )
        # Regression coverage: an iceberg slice's dashboard row must show
        # the price paid, not a blank Entry column.
        self.assertEqual(
            recorded, [("STOCK:AAA", "order-2", "BUY", Decimal("10.01"))]
        )

    def test_process_iceberg_orders_skips_before_interval_elapses(self):
        from webull_bot.bot import AutoTrader

        class FakeApi:
            def stock_quote(self, symbol):
                raise AssertionError("must not fetch a quote before the interval")

        fake_bot = SimpleNamespace(
            api=FakeApi(),
            iceberg_orders={
                "AAA:BUY": {
                    "symbol": "AAA",
                    "side": "BUY",
                    "key": "STOCK:AAA",
                    "remaining": Decimal("15"),
                    "last_slice_at": time.monotonic(),
                }
            },
        )
        process = AutoTrader.process_iceberg_orders.__get__(fake_bot)
        process()
        self.assertEqual(fake_bot.iceberg_orders["AAA:BUY"]["remaining"], Decimal("15"))

    def test_process_iceberg_orders_removes_entry_when_fully_filled(self):
        from webull_bot.bot import AutoTrader, ICEBERG_SLICE_INTERVAL_SECONDS, ICEBERG_SLICE_SHARES
        from webull_bot.strategy import TradingStrategy

        class FakeApi:
            def stock_quote(self, symbol):
                return {"symbol": symbol, "bid": "10.00", "ask": "10.02", "price": "10.01"}

            @staticmethod
            def quote_price(q):
                return Decimal(str(q["price"]))

            @staticmethod
            def stock_limit_price(q, side):
                return Decimal(str(q["price"]))

            def place_stock(self, symbol, side, quantity, limit_price=None):
                return "order-3"

        fake_bot = SimpleNamespace(
            api=FakeApi(),
            iceberg_orders={
                "AAA:BUY": {
                    "symbol": "AAA",
                    "side": "BUY",
                    "key": "STOCK:AAA",
                    "remaining": Decimal(str(ICEBERG_SLICE_SHARES)),
                    "last_slice_at": time.monotonic()
                    - ICEBERG_SLICE_INTERVAL_SECONDS
                    - 1,
                }
            },
            record_trade=lambda *a, **k: None,
            price_sanity_rejected_at={},
            strategy=SimpleNamespace(minimum_lot_size=TradingStrategy.minimum_lot_size),
        )
        fake_bot.price_sanity_ok = AutoTrader.price_sanity_ok.__get__(fake_bot)
        fake_bot.record_order_error = AutoTrader.record_order_error.__get__(fake_bot)
        process = AutoTrader.process_iceberg_orders.__get__(fake_bot)
        process()
        self.assertNotIn("AAA:BUY", fake_bot.iceberg_orders)

    def test_process_iceberg_orders_places_the_full_remainder_in_the_lot_restricted_band(self):
        """Companion to the place_stock_scaled regression test - if price
        drifts into the $0.10-$0.999 band after the first clip already
        went out, a later slice must not keep trying 10-share clips
        either.
        """
        from webull_bot.bot import AutoTrader, ICEBERG_SLICE_INTERVAL_SECONDS
        from webull_bot.strategy import TradingStrategy

        placed = []

        class FakeApi:
            def stock_quote(self, symbol):
                return {"symbol": symbol, "bid": "0.29", "ask": "0.31", "price": "0.30"}

            @staticmethod
            def quote_price(q):
                return Decimal(str(q["price"]))

            @staticmethod
            def stock_limit_price(q, side):
                return Decimal(str(q["price"]))

            def place_stock(self, symbol, side, quantity, limit_price=None):
                placed.append((symbol, side, quantity, limit_price))
                return "order-4"

        fake_bot = SimpleNamespace(
            api=FakeApi(),
            iceberg_orders={
                "HOWL:BUY": {
                    "symbol": "HOWL",
                    "side": "BUY",
                    "key": "STOCK:HOWL",
                    "remaining": Decimal("120"),
                    "last_slice_at": time.monotonic()
                    - ICEBERG_SLICE_INTERVAL_SECONDS
                    - 1,
                }
            },
            price_sanity_rejected_at={},
            strategy=SimpleNamespace(minimum_lot_size=TradingStrategy.minimum_lot_size),
        )
        fake_bot.price_sanity_ok = AutoTrader.price_sanity_ok.__get__(fake_bot)
        fake_bot.record_order_error = AutoTrader.record_order_error.__get__(fake_bot)
        fake_bot.record_trade = lambda *a, **k: None
        process = AutoTrader.process_iceberg_orders.__get__(fake_bot)
        process()
        self.assertEqual(len(placed), 1)
        self.assertEqual(placed[0][2], Decimal("120"))
        self.assertNotIn("HOWL:BUY", fake_bot.iceberg_orders)


class FractionalExitGuardTests(unittest.TestCase):
    def test_is_fractional_quantity(self):
        from webull_bot.bot import AutoTrader

        self.assertTrue(AutoTrader.is_fractional_quantity(Decimal("2.5847")))
        self.assertFalse(AutoTrader.is_fractional_quantity(Decimal("5")))
        self.assertFalse(AutoTrader.is_fractional_quantity(Decimal("0")))

    def test_stall_equity_quotes_batches_into_one_call_per_category(self):
        """Regression test: boost_stalled_positions used to call
        api.stock_quote(symbol) individually inside its loop - which
        itself makes two API calls per symbol (a category lookup plus a
        single-symbol quote fetch) - so a held-position count in the
        teens meant dozens of sequential, individually rate-limited round
        trips blocking the entire single-threaded main loop for minutes
        at a stretch. Candidates across two categories must batch into
        exactly one call per category, not one call per symbol.
        """
        from webull_bot.bot import AutoTrader

        calls = []

        class FakeApi:
            def stock_quotes_resilient(self, symbols, category):
                calls.append((category, tuple(symbols)))
                return [
                    {"symbol": s, "bid": "10.00"} for s in symbols
                ], set()

        # Not a real time.monotonic() reading - last_trade defaults to 0.0
        # per-symbol below, and the real monotonic clock isn't guaranteed
        # to already exceed stall_seconds (120) on every CI runner (same
        # class of flaky-clock bug fixed elsewhere in this file).
        now = 10_000.0
        fake_bot = SimpleNamespace(
            api=FakeApi(),
            stock_categories={"NVDA": "US_ETF"},
            pending_stock_exits=set(),
            last_trade={},
        )
        fake_bot.cooldown_ready = lambda key: True
        fake_bot.is_fractional_quantity = AutoTrader.is_fractional_quantity
        fetch = AutoTrader._stall_equity_quotes.__get__(fake_bot)
        positions = [
            {"instrument_type": "EQUITY", "symbol": "AAPL", "quantity": "5", "cost_price": "100"},
            {"instrument_type": "EQUITY", "symbol": "MSFT", "quantity": "3", "cost_price": "200"},
            {"instrument_type": "EQUITY", "symbol": "NVDA", "quantity": "2", "cost_price": "50"},
            {"instrument_type": "OPTION", "symbol": "AAPL260918C00200000", "quantity": "1", "cost_price": "1"},
        ]

        quote_by_symbol = fetch(positions, core_session_active=True, stall_seconds=120, now=now)

        # Exactly one call per category (US_STOCK: AAPL+MSFT, US_ETF: NVDA)
        # - never one call per symbol, and the OPTION leg is never
        # considered at all.
        self.assertEqual(len(calls), 2)
        called_categories = {category for category, _ in calls}
        self.assertEqual(called_categories, {"US_STOCK", "US_ETF"})
        self.assertEqual(set(quote_by_symbol), {"AAPL", "MSFT", "NVDA"})

    def test_boost_stalled_positions_skips_fractional_position_outside_core_hours(self):
        # Regression test: Webull rejects ANY order (buy or sell) on a
        # non-integer quantity outside core hours regardless of the
        # client-side fractional/order-type flags - previously this kept
        # retrying every stall-breaker interval and spamming the same
        # OAUTH_OPENAPI_FRACT_ONLT_CORE_TIME rejection.
        from webull_bot.bot import AutoTrader

        calls = []

        class FakeApi:
            def stock_quotes_resilient(self, symbols, category):
                calls.append(symbols)
                return [
                    {"symbol": s, "bid": "100.00", "ask": "100.05"} for s in symbols
                ], set()

            @staticmethod
            def quote_bid(q):
                return Decimal(str(q["bid"]))

            @staticmethod
            def quote_ask(q):
                return Decimal(str(q["ask"]))

            def place_stock(self, *a, **k):
                raise AssertionError("must not place an order outside core hours")

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                stall_breaker_enabled=True,
                stall_breaker_seconds=1,
                stall_breaker_min_profit=Decimal("0.01"),
                sell_fee_dollars=Decimal("0.02"),
                stock_entry_max_spread_percent=Decimal("0.50"),
            ),
            api=FakeApi(),
            stock_categories={},
            last_trade={},
            last_stall_boost=0.0,
            pending_stock_exits=set(),
            pending_option_exits=set(),
        )
        fake_bot.cooldown_ready = lambda key: True
        fake_bot.is_fractional_quantity = AutoTrader.is_fractional_quantity
        fake_bot._stall_equity_quotes = AutoTrader._stall_equity_quotes.__get__(fake_bot)
        fake_bot._stall_exit_price = AutoTrader._stall_exit_price.__get__(fake_bot)
        boost = AutoTrader.boost_stalled_positions.__get__(fake_bot)
        positions = [
            {
                "instrument_type": "EQUITY",
                "symbol": "COST",
                "quantity": "2.5847",
                "cost_price": "95.00",
            }
        ]
        boost(positions, options_active=False, core_session_active=False)
        self.assertEqual(calls, [])

    def test_boost_stalled_positions_skips_sub_lot_position_in_penny_band(self):
        # Regression test: Webull rejects ANY order (either side) under
        # 100 shares while price sits in $0.10-$0.999
        # (OAUTH_OPENAPI_CANT_TRADE_FOR_PRICE_BETWEEN_0099_AND_0999),
        # regardless of how many shares are actually held - a position
        # that fell into this band with fewer than 100 shares can't be
        # exited by a normal order at all until price moves back out.
        from webull_bot.bot import AutoTrader
        from webull_bot.strategy import TradingStrategy

        calls = []

        class FakeApi:
            def stock_quotes_resilient(self, symbols, category):
                calls.extend(symbols)
                return [
                    {"symbol": s, "bid": "0.50", "ask": "0.51"} for s in symbols
                ], set()

            @staticmethod
            def quote_bid(q):
                return Decimal(str(q["bid"]))

            @staticmethod
            def quote_ask(q):
                return Decimal(str(q["ask"]))

            def place_stock(self, *a, **k):
                raise AssertionError(
                    "must not place a sub-100-share order in the "
                    "lot-restricted band"
                )

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                stall_breaker_enabled=True,
                stall_breaker_seconds=1,
                stall_breaker_min_profit=Decimal("0.01"),
                sell_fee_dollars=Decimal("0.02"),
                stock_entry_max_spread_percent=Decimal("0.50"),
            ),
            api=FakeApi(),
            strategy=SimpleNamespace(
                minimum_lot_size=TradingStrategy.minimum_lot_size,
                exit_blocked_by_lot_restriction=TradingStrategy.exit_blocked_by_lot_restriction,
            ),
            stock_categories={},
            last_trade={},
            last_stall_boost=0.0,
            pending_stock_exits=set(),
            pending_option_exits=set(),
        )
        fake_bot.cooldown_ready = lambda key: True
        fake_bot.is_fractional_quantity = AutoTrader.is_fractional_quantity
        fake_bot._stall_equity_quotes = AutoTrader._stall_equity_quotes.__get__(fake_bot)
        fake_bot._stall_exit_price = AutoTrader._stall_exit_price.__get__(fake_bot)
        boost = AutoTrader.boost_stalled_positions.__get__(fake_bot)
        positions = [
            {
                "instrument_type": "EQUITY",
                "symbol": "OPTT",
                "quantity": "5",
                "cost_price": "0.40",
            }
        ]
        boost(positions, options_active=False, core_session_active=True)
        self.assertEqual(calls, ["OPTT"])

    def test_stall_check_is_per_symbol_not_a_global_activity_clock(self):
        """Regression test: an account that's generally active (new
        entries landing every minute or two, well under
        STALL_BREAKER_SECONDS) previously blocked the stall-breaker from
        running at all, via one global "has ANYTHING filled recently"
        clock - even though a specific older position had been sitting
        untouched with no order activity of its own the whole time. The
        check must be per-symbol (self.last_trade[key]), not global.
        """
        from webull_bot.bot import AutoTrader
        from webull_bot.strategy import TradingStrategy

        calls = []

        class FakeApi:
            def stock_quotes_resilient(self, symbols, category):
                calls.extend(symbols)
                return [
                    {"symbol": s, "bid": "101.00", "ask": "101.05"} for s in symbols
                ], set()

            @staticmethod
            def quote_bid(q):
                return Decimal(str(q["bid"]))

            @staticmethod
            def quote_ask(q):
                return Decimal(str(q["ask"]))

            @staticmethod
            def price_tick_size(price):
                from webull_bot.webull_api import WebullAPI

                return WebullAPI.price_tick_size(price)

            def place_stock(self, *a, **k):
                return "order-1"

        now = time.monotonic()
        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                stall_breaker_enabled=True,
                stall_breaker_seconds=120,
                stall_breaker_min_profit=Decimal("0.01"),
                sell_fee_dollars=Decimal("0.02"),
                stock_entry_max_spread_percent=Decimal("0.50"),
            ),
            api=FakeApi(),
            strategy=SimpleNamespace(
                minimum_lot_size=TradingStrategy.minimum_lot_size,
                exit_blocked_by_lot_restriction=TradingStrategy.exit_blocked_by_lot_restriction,
            ),
            stock_categories={},
            # A different symbol traded 10 seconds ago (the account is
            # "generally active"), but STALE's own last order was 500
            # seconds ago - well past stall_breaker_seconds.
            last_trade={"STOCK:OTHER": now - 10, "STOCK:STALE": now - 500},
            # Not 0.0 - the real time.monotonic() isn't guaranteed to
            # already be past stall_breaker_seconds (120) on every CI
            # runner (same class of flaky-clock bug fixed earlier this
            # session, reintroduced here since 0.0 is only safe for a
            # tiny threshold like the other fixtures in this class use).
            last_stall_boost=now - 999999,
            pending_stock_exits=set(),
            pending_option_exits=set(),
        )
        fake_bot.cooldown_ready = lambda key: True
        fake_bot.is_fractional_quantity = AutoTrader.is_fractional_quantity
        fake_bot.record_realized_exit = lambda *a, **k: Decimal("0.05")
        fake_bot.record_trade = lambda *a, **k: None
        fake_bot._stall_equity_quotes = AutoTrader._stall_equity_quotes.__get__(fake_bot)
        fake_bot._stall_exit_price = AutoTrader._stall_exit_price.__get__(fake_bot)
        boost = AutoTrader.boost_stalled_positions.__get__(fake_bot)
        positions = [
            {
                "instrument_type": "EQUITY",
                "symbol": "STALE",
                "quantity": "10",
                "cost_price": "100.00",
            }
        ]

        boost(positions, options_active=False, core_session_active=True)

        self.assertEqual(calls, ["STALE"])
        self.assertIn("STALE", fake_bot.pending_stock_exits)


class StallExitPriceSpreadSanityTests(unittest.TestCase):
    """Live incident: TBB (1 share, cost=19.42) sat with bid=19.39/
    ask=19.89 - a ~2.5% spread, well past stock_entry_max_spread_percent's
    default 0.50%. The bid never cleared cost+min_profit+fee, so
    _stall_exit_price fell back to resting a passive SELL at the ask -
    but the ask was far above where TBB was actually trading (prints at
    19.41), so the order sat until order_timeout_seconds, got cancelled,
    and boost_stalled_positions resubmitted the identical unreachable
    limit next stall cycle. Forever, without ever filling. The fix:
    _stall_exit_price now refuses the ask fallback when the spread itself
    is wider than the same bound entries are held to, and simply waits
    (no order submitted this cycle) instead of spinning on a doomed one.
    """

    @staticmethod
    def _price_fn():
        from webull_bot.bot import AutoTrader

        from webull_bot.webull_api import WebullAPI

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(stock_entry_max_spread_percent=Decimal("0.50")),
            api=SimpleNamespace(
                quote_bid=lambda q: Decimal(str(q["bid"])) if q.get("bid") else None,
                quote_ask=lambda q: Decimal(str(q["ask"])) if q.get("ask") else None,
                quote_price=WebullAPI.quote_price,
                price_tick_size=WebullAPI.price_tick_size,
            ),
        )
        return AutoTrader._stall_exit_price.__get__(fake_bot)

    def test_refuses_to_rest_at_an_unreachable_ask_on_a_wide_spread(self):
        price_fn = self._price_fn()
        quote = {"bid": "19.39", "ask": "19.89"}
        result = price_fn(
            quote,
            average_cost=Decimal("19.42"),
            min_profit=Decimal("0.01"),
            fee_per_share=Decimal("0.02"),
        )
        self.assertIsNone(result)

    def test_still_uses_the_ask_when_the_spread_is_tight(self):
        price_fn = self._price_fn()
        quote = {"bid": "19.40", "ask": "19.48"}
        result = price_fn(
            quote,
            average_cost=Decimal("19.42"),
            min_profit=Decimal("0.01"),
            fee_per_share=Decimal("0.02"),
        )
        self.assertEqual(result, Decimal("19.48"))

    def test_bid_alone_clearing_the_floor_is_unaffected_by_the_spread_check(self):
        price_fn = self._price_fn()
        quote = {"bid": "19.89", "ask": "20.50"}
        result = price_fn(
            quote,
            average_cost=Decimal("19.42"),
            min_profit=Decimal("0.01"),
            fee_per_share=Decimal("0.02"),
        )
        self.assertEqual(result, Decimal("19.89"))

    def test_a_wider_explicit_max_spread_percent_allows_the_ask_fallback(self):
        """Live incident: GAUZ routinely quoted 2-7% spreads (its normal
        character, not a glitch) - the volatility-scalp cohort's own
        exit pricing passes a much wider max_spread_percent explicitly
        instead of falling back to the tight default, so the ask
        fallback stays usable on exactly the wide-spread names this
        cohort exists to trade.
        """
        price_fn = self._price_fn()
        # (19.89 - 19.39) / 19.39 * 100 = 2.58% - would be refused under
        # the default 0.50% bound (see test_refuses_to_rest_at_an_
        # unreachable_ask_on_a_wide_spread above), but is allowed here.
        quote = {"bid": "19.39", "ask": "19.89"}
        result = price_fn(
            quote,
            average_cost=Decimal("19.42"),
            min_profit=Decimal("0.01"),
            fee_per_share=Decimal("0.02"),
            max_spread_percent=Decimal("8"),
        )
        self.assertEqual(result, Decimal("19.89"))

    def test_caps_the_ask_fallback_near_the_last_trade_not_the_top_of_spread(self):
        """By request: "you cannot always go to the top of the spread
        when it is big, you must ask a reasonable price, not too far
        from the last [trade]." The spread here (2.58%) passes the
        wider explicit bound, and the raw ask alone would clear the
        floor - but real prints are at 19.41, far below the 19.89 ask,
        so resting there wouldn't reflect where the stock is actually
        trading. Caps at last_price * (1 + max_spread_percent/200).
        """
        price_fn = self._price_fn()
        # average_cost=19.42 keeps this consistent with the sibling
        # tests above: bid (19.39) alone does NOT clear the floor
        # (19.45), so this actually reaches the ask-fallback/cap logic
        # rather than returning the bid immediately.
        quote = {"bid": "19.39", "ask": "19.89", "price": "19.41"}
        result = price_fn(
            quote,
            average_cost=Decimal("19.42"),
            min_profit=Decimal("0.01"),
            fee_per_share=Decimal("0.02"),
            max_spread_percent=Decimal("8"),
        )
        # cap = 19.41 * 1.04 = 20.1864 -> quantized down to 20.18, still
        # above the raw ask (19.89) here, so this specific cap doesn't
        # bind - confirms the cap tracks the last price, not a fixed
        # ceiling, and doesn't wrongly reject a reachable ask.
        self.assertEqual(result, Decimal("19.89"))

    def test_cap_can_push_the_price_below_the_floor_and_skip_the_cycle(self):
        """When the ask sits far above the last print (a truly stale/
        unrealistic quote) even within the allowed spread, the capped
        price can end up below the profit floor - correctly skips
        rather than resting at an unrealistic price.
        """
        price_fn = self._price_fn()
        # ask=19.89 alone would clear a floor of ~19.33, but the last
        # print is only 19.00 - cap = 19.00 * 1.04 = 19.76, still above
        # floor here, so use a tighter floor scenario instead: push
        # average_cost up so only the raw ask (not the capped price)
        # would clear it.
        quote = {"bid": "19.39", "ask": "19.89", "price": "19.00"}
        result = price_fn(
            quote,
            average_cost=Decimal("19.80"),
            min_profit=Decimal("0.01"),
            fee_per_share=Decimal("0.02"),
            max_spread_percent=Decimal("8"),
        )
        # floor = 19.80 + 0.01 + 0.02 = 19.83. Raw ask (19.89) alone
        # would clear it, but the cap (19.00 * 1.04 = 19.76) does not -
        # must skip, not rest at an unrealistic price just because the
        # raw ask alone would have looked profitable.
        self.assertIsNone(result)


class StallBreakerWideSpreadResubmitTests(unittest.TestCase):
    def test_boost_stalled_positions_does_not_resubmit_at_an_unfillable_ask(self):
        from webull_bot.bot import AutoTrader

        calls = []

        class FakeApi:
            def stock_quotes_resilient(self, symbols, category):
                calls.extend(symbols)
                return [
                    {"symbol": s, "bid": "19.39", "ask": "19.89"} for s in symbols
                ], set()

            @staticmethod
            def quote_bid(q):
                return Decimal(str(q["bid"]))

            @staticmethod
            def quote_ask(q):
                return Decimal(str(q["ask"]))

            def place_stock(self, *a, **k):
                raise AssertionError(
                    "must not rest a limit at an ask far above the last "
                    "trade price on a wide-spread, illiquid symbol"
                )

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                stall_breaker_enabled=True,
                stall_breaker_seconds=1,
                stall_breaker_min_profit=Decimal("0.01"),
                sell_fee_dollars=Decimal("0.02"),
                stock_entry_max_spread_percent=Decimal("0.50"),
            ),
            api=FakeApi(),
            stock_categories={},
            last_trade={},
            last_stall_boost=0.0,
            pending_stock_exits=set(),
            pending_option_exits=set(),
        )
        fake_bot.cooldown_ready = lambda key: True
        fake_bot.is_fractional_quantity = AutoTrader.is_fractional_quantity
        fake_bot._stall_equity_quotes = AutoTrader._stall_equity_quotes.__get__(fake_bot)
        fake_bot._stall_exit_price = AutoTrader._stall_exit_price.__get__(fake_bot)
        boost = AutoTrader.boost_stalled_positions.__get__(fake_bot)
        positions = [
            {
                "instrument_type": "EQUITY",
                "symbol": "TBB",
                "quantity": "1",
                "cost_price": "19.42",
            }
        ]
        boost(positions, options_active=False, core_session_active=True)
        self.assertEqual(calls, ["TBB"])
        self.assertNotIn("TBB", fake_bot.pending_stock_exits)


class EntrySizingSplitTests(unittest.TestCase):
    @staticmethod
    def _size_fn(config, fractional_trading_enabled=True):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=config,
            strategy=TradingStrategy(config),
            fractional_trading_enabled=fractional_trading_enabled,
        )
        return AutoTrader.size_stock_entry.__get__(fake_bot)

    def test_core_session_prefers_fractional_when_it_produces_a_quantity(self):
        config = Settings(
            stock_core_session_position_fraction=Decimal("0.15"),
            stock_whole_share_core_session_fraction=Decimal("0.35"),
            stock_quantity=100,
        )
        size = self._size_fn(config)
        # fractional_remaining/whole_share_remaining are precomputed by the
        # caller once per cycle (10000 * 0.15 / 10000 * 0.35) - see
        # trade_stocks - not derived live inside size_stock_entry itself.
        quantity, buffered_price, fractional = size(
            Decimal("50"), Decimal("10000"), Decimal("1500"), Decimal("3500"), True
        )
        self.assertTrue(fractional)
        self.assertGreater(quantity, 0)

    def test_core_session_falls_back_to_whole_share_when_fractional_fraction_is_zero(self):
        config = Settings(
            stock_core_session_position_fraction=Decimal("0"),
            stock_whole_share_core_session_fraction=Decimal("0.35"),
            stock_quantity=1000,
            max_order_notional=Decimal("100000"),
        )
        size = self._size_fn(config)
        quantity, buffered_price, fractional = size(
            Decimal("50"), Decimal("10000"), Decimal("0"), Decimal("3500"), True
        )
        self.assertFalse(fractional)
        # whole-share budget = min(entry_budget=10000, 3500) = 3500
        # buffered_price = 50*1.03 = 51.5 -> floor(3500/51.5) = 67
        self.assertEqual(quantity, 67)

    def test_fractional_unsupported_ticker_uses_whole_share_budget(self):
        """A per-security FRACT_TICKER_DONT_SUPPORT_TRADE rejection
        (see is_fractional_ticker_unsupported) must not disable fractional
        trading account-wide - fractional_supported=False forces whole-
        share sizing for just this symbol, unlike fractional_trading_
        enabled=False which is account-wide.
        """
        config = Settings(
            stock_core_session_position_fraction=Decimal("0.15"),
            stock_whole_share_core_session_fraction=Decimal("0.20"),
            stock_quantity=1000,
            max_order_notional=Decimal("100000"),
        )
        size = self._size_fn(config)
        quantity, buffered_price, fractional = size(
            Decimal("50"), Decimal("10000"), Decimal("1500"), Decimal("2000"), True, True, False
        )
        self.assertFalse(fractional)
        self.assertEqual(quantity, 38)

    def test_fractional_trading_disabled_uses_whole_share_budget(self):
        config = Settings(
            stock_core_session_position_fraction=Decimal("0.15"),
            stock_whole_share_core_session_fraction=Decimal("0.20"),
            stock_quantity=1000,
            max_order_notional=Decimal("100000"),
        )
        size = self._size_fn(config, fractional_trading_enabled=False)
        quantity, buffered_price, fractional = size(
            Decimal("50"), Decimal("10000"), Decimal("1500"), Decimal("2000"), True
        )
        self.assertFalse(fractional)
        # whole-share budget = min(10000, 2000) = 2000
        # floor(2000/51.5) = 38
        self.assertEqual(quantity, 38)

    def test_no_fractional_slot_available_uses_whole_share_budget(self):
        """Position-cap reservation gate (see trade_stocks'
        max_fractional_positions): even with a full fractional pool and
        fractional trading enabled, a caller-signaled "no slot available"
        must force whole-share sizing - a fractional position can't be
        exited outside core hours, so letting fractional alone fill every
        MAX_OPEN_POSITIONS slot would strand the account for the rest of
        the day.
        """
        config = Settings(
            stock_core_session_position_fraction=Decimal("0.15"),
            stock_whole_share_core_session_fraction=Decimal("0.20"),
            stock_quantity=1000,
            max_order_notional=Decimal("100000"),
        )
        size = self._size_fn(config)
        quantity, buffered_price, fractional = size(
            Decimal("50"),
            Decimal("10000"),
            Decimal("1500"),
            Decimal("2000"),
            True,
            False,
        )
        self.assertFalse(fractional)
        self.assertEqual(quantity, 38)

    def test_outside_core_hours_whole_share_budget_is_not_capped(self):
        config = Settings(
            stock_core_session_position_fraction=Decimal("0.15"),
            stock_whole_share_core_session_fraction=Decimal("0.01"),
            stock_quantity=1000,
            max_order_notional=Decimal("100000"),
        )
        size = self._size_fn(config)
        # Deliberately tiny whole_share_remaining - must NOT apply outside
        # core hours, or this test would only afford ~1 share instead of
        # ~194.
        quantity, buffered_price, fractional = size(
            Decimal("50"), Decimal("10000"), Decimal("1500"), Decimal("100"), False
        )
        self.assertFalse(fractional)
        # Full entry_budget=10000 used, uncapped -> floor(10000/51.5) = 194
        self.assertEqual(quantity, 194)

    def test_max_fractional_position_slots_reserves_proportionally(self):
        from webull_bot.bot import AutoTrader

        # 0.15 fractional : 0.35 whole-share of a 20-slot cap -> 6 slots
        # reserved for fractional, guaranteeing 14 remain for whole-share/
        # other styles even if fractional fills every slot it can.
        self.assertEqual(
            AutoTrader.max_fractional_position_slots(
                20, Decimal("0.15"), Decimal("0.35")
            ),
            6,
        )

    def test_max_fractional_position_slots_reserves_at_least_one(self):
        from webull_bot.bot import AutoTrader

        self.assertEqual(
            AutoTrader.max_fractional_position_slots(
                20, Decimal("0.01"), Decimal("0.99")
            ),
            1,
        )

    def test_max_fractional_position_slots_falls_back_when_no_capital_allocated(self):
        from webull_bot.bot import AutoTrader

        self.assertEqual(
            AutoTrader.max_fractional_position_slots(20, Decimal("0"), Decimal("0")),
            20,
        )

    def test_fractional_failure_falls_through_to_whole_share(self):
        config = Settings(
            stock_core_session_position_fraction=Decimal("0.15"),
            stock_whole_share_core_session_fraction=Decimal("0.35"),
            stock_quantity=1000,
            max_order_notional=Decimal("100000"),
            fractional_shares_min_notional=Decimal("5"),
        )
        size = self._size_fn(config)
        # fractional_remaining=$4.50, under the $5 fractional minimum, so
        # the fractional attempt must produce 0 and fall through to
        # whole-share sizing instead of returning 0 outright.
        quantity, buffered_price, fractional = size(
            Decimal("5"), Decimal("1000"), Decimal("4.5"), Decimal("10.5"), True
        )
        self.assertFalse(fractional)
        self.assertGreater(quantity, 0)


class OvernightHoldTests(unittest.TestCase):
    def test_overnight_hold_symbols_excludes_intraday_only_buckets(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            position_buckets={
                "AAPL": "popular",
                "KO": "PAIRS_LONG",
                "PEP": "PAIRS_SHORT",
                "GME": "MANUAL",
            },
            short_symbols=set(),
        )
        held = AutoTrader.overnight_hold_symbols.__get__(fake_bot)()
        self.assertEqual(held, {"AAPL", "GME"})

    def test_overnight_hold_symbols_excludes_main_strategy_shorts(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            position_buckets={"AAPL": "popular", "GME": "popular"},
            short_symbols={"GME"},
        )
        held = AutoTrader.overnight_hold_symbols.__get__(fake_bot)()
        self.assertEqual(held, {"AAPL"})

    def test_overnight_hold_disabled_returns_empty_set(self):
        import webull_bot.bot as bot_module
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            position_buckets={"AAPL": "popular"}, short_symbols=set()
        )
        original = bot_module.OVERNIGHT_HOLD_ENABLED
        bot_module.OVERNIGHT_HOLD_ENABLED = False
        try:
            held = AutoTrader.overnight_hold_symbols.__get__(fake_bot)()
        finally:
            bot_module.OVERNIGHT_HOLD_ENABLED = original
        self.assertEqual(held, set())

    def test_exclude_pairs_symbols_removes_pairs_tickers(self):
        from webull_bot.bot import AutoTrader
        from webull_bot.pairs import PAIRS

        universe = ["AAPL", "MSFT"] + [symbol for pair in PAIRS for symbol in pair]
        remaining, excluded = AutoTrader.exclude_pairs_symbols(universe)
        self.assertEqual(set(excluded), {symbol for pair in PAIRS for symbol in pair})
        self.assertEqual(remaining, ["AAPL", "MSFT"])


class FractionalPreCloseSweepTests(unittest.TestCase):
    @staticmethod
    def _fake_bot(positions, quotes=None, config=None):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=config or SimpleNamespace(eod_retry_seconds=Decimal("10")),
            # Not 0.0 - the real time.monotonic() isn't guaranteed to
            # already be past eod_retry_seconds on every CI runner (same
            # class of flaky-clock bug fixed earlier this session).
            last_fractional_sweep=time.monotonic() - 999999,
            pending_stock_exits={"AAPL", "MSFT"},
            wash_sales=SimpleNamespace(block=lambda *a, **k: None),
            is_fractional_quantity=AutoTrader.is_fractional_quantity,
        )
        quotes = quotes or {}
        fake_bot.api = SimpleNamespace(
            positions=lambda: positions,
            stock_quote=lambda symbol: {"symbol": symbol, "price": str(quotes.get(symbol, "0"))},
            quote_price=lambda quote: Decimal(str(quote["price"])),
        )
        return fake_bot

    def test_closes_only_profitable_fractional_positions(self):
        """MSFT is a fractional position sitting at a profit (must close);
        BABA is fractional but underwater (must NOT be force-sold - it's
        already undefendable either way, and forcing a realized loss here
        isn't necessary the way capturing a gain is); FPE is whole-share
        (excluded regardless of P&L); the OPTION leg is never touched.
        """
        from webull_bot.bot import AutoTrader

        positions = [
            {"instrument_type": "EQUITY", "symbol": "MSFT", "quantity": "0.011", "cost_price": "493.23"},
            {"instrument_type": "EQUITY", "symbol": "BABA", "quantity": "0.109", "cost_price": "128.93"},
            {"instrument_type": "EQUITY", "symbol": "FPE", "quantity": "1", "cost_price": "17.81"},
            {"instrument_type": "OPTION", "symbol": "AAPL260918C00200000", "quantity": "0.5", "cost_price": "1.00"},
        ]
        quotes = {"MSFT": "497.33", "BABA": "122.20"}
        fake_bot = self._fake_bot(positions, quotes)
        calls = {}

        def fake_close_all_positions(instrument_types, loss_callback=None, exclude_symbols=None):
            calls["instrument_types"] = instrument_types
            calls["exclude_symbols"] = exclude_symbols
            return ["order-1"]

        fake_bot.api.close_all_positions = fake_close_all_positions
        sweep = AutoTrader.close_fractional_positions_before_core_close.__get__(fake_bot)

        sweep()

        self.assertEqual(calls["instrument_types"], {"EQUITY"})
        # Every EQUITY position except the profitable fractional one
        # (MSFT) is excluded - the losing fractional one (BABA) and the
        # whole-share one (FPE). The option leg is never considered at
        # all (not an EQUITY position).
        self.assertEqual(calls["exclude_symbols"], {"BABA", "FPE"})
        self.assertNotIn("MSFT", fake_bot.pending_stock_exits)
        self.assertIn("AAPL", fake_bot.pending_stock_exits)

    def test_noop_when_nothing_is_fractional(self):
        from webull_bot.bot import AutoTrader

        positions = [
            {"instrument_type": "EQUITY", "symbol": "FPE", "quantity": "1", "cost_price": "17.81"},
        ]
        fake_bot = self._fake_bot(positions)

        def fail_if_called(*a, **k):
            raise AssertionError("must not attempt to close anything")

        fake_bot.api.close_all_positions = fail_if_called
        sweep = AutoTrader.close_fractional_positions_before_core_close.__get__(fake_bot)

        sweep()  # must not raise

    def test_noop_when_every_fractional_position_is_underwater(self):
        from webull_bot.bot import AutoTrader

        positions = [
            {"instrument_type": "EQUITY", "symbol": "BABA", "quantity": "0.109", "cost_price": "128.93"},
        ]
        fake_bot = self._fake_bot(positions, {"BABA": "122.20"})

        def fail_if_called(*a, **k):
            raise AssertionError("must not attempt to close anything")

        fake_bot.api.close_all_positions = fail_if_called
        sweep = AutoTrader.close_fractional_positions_before_core_close.__get__(fake_bot)

        sweep()  # must not raise

    def test_throttled_within_eod_retry_seconds(self):
        from webull_bot.bot import AutoTrader

        positions = [
            {"instrument_type": "EQUITY", "symbol": "MSFT", "quantity": "0.011", "cost_price": "493.23"},
        ]
        fake_bot = self._fake_bot(positions, {"MSFT": "497.33"})
        fake_bot.last_fractional_sweep = time.monotonic()
        calls = []
        fake_bot.api.close_all_positions = lambda *a, **k: (calls.append(1), [])[1]
        sweep = AutoTrader.close_fractional_positions_before_core_close.__get__(fake_bot)

        sweep()

        self.assertEqual(calls, [])

    def test_survives_a_broker_failure(self):
        from webull_bot.bot import AutoTrader

        positions = [
            {"instrument_type": "EQUITY", "symbol": "MSFT", "quantity": "0.011", "cost_price": "493.23"},
        ]
        fake_bot = self._fake_bot(positions, {"MSFT": "497.33"})

        def boom(*a, **k):
            raise RuntimeError("boom")

        fake_bot.api.close_all_positions = boom
        sweep = AutoTrader.close_fractional_positions_before_core_close.__get__(fake_bot)

        sweep()  # must not raise

    def test_survives_a_quote_failure_for_one_symbol(self):
        """A quote failure for one fractional symbol must not stop the
        sweep from still closing others that quoted fine.
        """
        from webull_bot.bot import AutoTrader

        positions = [
            {"instrument_type": "EQUITY", "symbol": "MSFT", "quantity": "0.011", "cost_price": "493.23"},
            {"instrument_type": "EQUITY", "symbol": "BROKEN", "quantity": "0.02", "cost_price": "10.00"},
        ]
        fake_bot = self._fake_bot(positions, {"MSFT": "497.33"})

        def flaky_quote(symbol):
            if symbol == "BROKEN":
                raise RuntimeError("quote unavailable")
            return {"symbol": symbol, "price": "497.33"}

        fake_bot.api.stock_quote = flaky_quote
        calls = {}
        fake_bot.api.close_all_positions = lambda instrument_types, loss_callback=None, exclude_symbols=None: (
            calls.update(exclude_symbols=exclude_symbols) or ["order-1"]
        )
        sweep = AutoTrader.close_fractional_positions_before_core_close.__get__(fake_bot)

        sweep()

        self.assertEqual(calls["exclude_symbols"], {"BROKEN"})


class ExtendedHoursProfitSweepTests(unittest.TestCase):
    """close_profitable_positions_during_extended_hours - by request,
    after pre-market losses: "capturing any profits to close out the
    day as much as possible" outside core hours. Same shape as
    FractionalPreCloseSweepTests but for ALL equity positions (not
    just fractional ones), on its own dedicated cadence.
    """

    @staticmethod
    def _fake_bot(positions, quotes=None, config=None):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=config
            or SimpleNamespace(extended_hours_profit_sweep_seconds=60),
            last_extended_hours_profit_sweep=time.monotonic() - 999999,
            pending_stock_exits={"AAPL", "MSFT"},
            wash_sales=SimpleNamespace(block=lambda *a, **k: None),
        )
        quotes = quotes or {}
        fake_bot.api = SimpleNamespace(
            positions=lambda: positions,
            stock_quote=lambda symbol: {"symbol": symbol, "price": str(quotes.get(symbol, "0"))},
            quote_price=lambda quote: Decimal(str(quote["price"])),
        )
        return fake_bot

    def test_closes_only_profitable_equity_positions(self):
        from webull_bot.bot import AutoTrader

        positions = [
            {"instrument_type": "EQUITY", "symbol": "MSFT", "quantity": "1", "cost_price": "493.23"},
            {"instrument_type": "EQUITY", "symbol": "BABA", "quantity": "1", "cost_price": "128.93"},
            {"instrument_type": "OPTION", "symbol": "AAPL260918C00200000", "quantity": "0.5", "cost_price": "1.00"},
        ]
        quotes = {"MSFT": "497.33", "BABA": "122.20"}
        fake_bot = self._fake_bot(positions, quotes)
        calls = {}

        def fake_close_all_positions(instrument_types, loss_callback=None, exclude_symbols=None):
            calls["instrument_types"] = instrument_types
            calls["exclude_symbols"] = exclude_symbols
            return ["order-1"]

        fake_bot.api.close_all_positions = fake_close_all_positions
        sweep = AutoTrader.close_profitable_positions_during_extended_hours.__get__(fake_bot)

        sweep()

        self.assertEqual(calls["instrument_types"], {"EQUITY"})
        self.assertEqual(calls["exclude_symbols"], {"BABA"})
        self.assertNotIn("MSFT", fake_bot.pending_stock_exits)

    def test_noop_when_every_position_is_underwater(self):
        from webull_bot.bot import AutoTrader

        positions = [
            {"instrument_type": "EQUITY", "symbol": "BABA", "quantity": "1", "cost_price": "128.93"},
        ]
        fake_bot = self._fake_bot(positions, {"BABA": "122.20"})

        def fail_if_called(*a, **k):
            raise AssertionError("must not attempt to close anything")

        fake_bot.api.close_all_positions = fail_if_called
        sweep = AutoTrader.close_profitable_positions_during_extended_hours.__get__(fake_bot)

        sweep()  # must not raise

    def test_throttled_within_its_own_sweep_interval(self):
        from webull_bot.bot import AutoTrader

        positions = [
            {"instrument_type": "EQUITY", "symbol": "MSFT", "quantity": "1", "cost_price": "493.23"},
        ]
        fake_bot = self._fake_bot(positions, {"MSFT": "497.33"})
        fake_bot.last_extended_hours_profit_sweep = time.monotonic()
        calls = []
        fake_bot.api.close_all_positions = lambda *a, **k: (calls.append(1), [])[1]
        sweep = AutoTrader.close_profitable_positions_during_extended_hours.__get__(fake_bot)

        sweep()

        self.assertEqual(calls, [])

    def test_survives_a_broker_failure(self):
        from webull_bot.bot import AutoTrader

        positions = [
            {"instrument_type": "EQUITY", "symbol": "MSFT", "quantity": "1", "cost_price": "493.23"},
        ]
        fake_bot = self._fake_bot(positions, {"MSFT": "497.33"})

        def boom(*a, **k):
            raise RuntimeError("boom")

        fake_bot.api.close_all_positions = boom
        sweep = AutoTrader.close_profitable_positions_during_extended_hours.__get__(fake_bot)

        sweep()  # must not raise


class CloseAllPositionsExclusionTests(unittest.TestCase):
    def test_close_all_positions_excludes_given_symbols(self):
        positions = [
            {"instrument_type": "EQUITY", "symbol": "AAPL", "quantity": "5", "cost_price": "150"},
            {"instrument_type": "EQUITY", "symbol": "TSLA", "quantity": "3", "cost_price": "200"},
        ]
        placed = []
        fake_api = SimpleNamespace(
            positions=lambda: positions,
            cancel_all_orders=lambda: [],
            stock_quote=lambda symbol: {
                "symbol": symbol,
                "bid": "200",
                "ask": "200.05",
                "price": "200.02",
            },
            quote_price=lambda q: Decimal(str(q["price"])),
            stock_limit_price=lambda q, side: Decimal(str(q["price"])),
            place_stock=lambda symbol, side, qty, limit_price, fractional=False: (
                placed.append((symbol, side, qty)) or "order-x"
            ),
        )
        close = WebullAPI.close_all_positions.__get__(fake_api)
        submitted = close({"EQUITY"}, exclude_symbols={"AAPL"})
        self.assertEqual(len(submitted), 1)
        self.assertEqual(placed[0][0], "TSLA")

    def test_close_all_positions_covers_short_positions_with_buy_side(self):
        positions = [
            {"instrument_type": "EQUITY", "symbol": "PEP", "quantity": "-10", "cost_price": "170"},
        ]
        placed = []
        pricing_calls = []
        fake_api = SimpleNamespace(
            positions=lambda: positions,
            cancel_all_orders=lambda: [],
            stock_quote=lambda symbol: {
                "symbol": symbol,
                "bid": "168",
                "ask": "168.05",
                "price": "168.02",
            },
            quote_price=lambda q: Decimal(str(q["price"])),
            stock_limit_price=lambda q, side: (
                pricing_calls.append(side) or Decimal(str(q["price"]))
            ),
            place_stock=lambda symbol, side, qty, limit_price, fractional=False: (
                placed.append((symbol, side, qty)) or "order-y"
            ),
        )
        close = WebullAPI.close_all_positions.__get__(fake_api)
        close({"EQUITY"})
        self.assertEqual(placed[0], ("PEP", "BUY", Decimal("10")))
        self.assertEqual(pricing_calls, ["COVER"])

    def test_close_all_positions_one_rejection_does_not_abort_the_rest(self):
        # Regression test: a single position's order getting rejected (e.g.
        # a sub-100-share position stuck in Webull's $0.10-$0.999 lot-
        # restricted band) previously propagated straight out of the
        # unwrapped for-loop, silently skipping every other position in
        # the batch - including the EOD closeout of everything else in the
        # account.
        positions = [
            {"instrument_type": "EQUITY", "symbol": "OPTT", "quantity": "5", "cost_price": "0.40"},
            {"instrument_type": "EQUITY", "symbol": "TSLA", "quantity": "3", "cost_price": "200"},
        ]
        placed = []

        def fake_place_stock(symbol, side, qty, limit_price, fractional=False):
            if symbol == "OPTT":
                raise RuntimeError(
                    "HTTP Status: 417, Code: "
                    "OAUTH_OPENAPI_CANT_TRADE_FOR_PRICE_BETWEEN_0099_AND_0999"
                )
            placed.append((symbol, side, qty))
            return "order-z"

        fake_api = SimpleNamespace(
            positions=lambda: positions,
            cancel_all_orders=lambda: [],
            stock_quote=lambda symbol: {
                "symbol": symbol,
                "bid": "0.50" if symbol == "OPTT" else "200",
                "ask": "0.51" if symbol == "OPTT" else "200.05",
                "price": "0.50" if symbol == "OPTT" else "200.02",
            },
            quote_price=lambda q: Decimal(str(q["price"])),
            stock_limit_price=lambda q, side: Decimal(str(q["price"])),
            place_stock=fake_place_stock,
        )
        close = WebullAPI.close_all_positions.__get__(fake_api)
        submitted = close({"EQUITY"})
        self.assertEqual(submitted, ["order-z"])
        self.assertEqual(placed, [("TSLA", "SELL", Decimal("3"))])


class OrderStatusExtractionTests(unittest.TestCase):
    """Regression tests against the confirmed live get_order_detail shape:
    status is nested inside orders[0], not at the top level - a live
    account was checked directly to confirm this after order_status's
    original flat-key-only extraction silently failed open on every real
    response (no top-level "status" key ever exists), which meant the
    phantom-pnl reversal in _reverse_if_never_filled never actually
    fired despite record_realized_exit correctly detecting the
    cancellation upstream.
    """

    def test_extracts_status_from_nested_orders_list(self):
        detail = {
            "client_order_id": "abc",
            "orders": [
                {"symbol": "FPE", "status": "CANCELLED", "filled_quantity": "0"}
            ],
        }
        self.assertEqual(WebullAPI.order_status(detail), "CANCELLED")

    def test_extracts_filled_status_from_nested_orders_list(self):
        detail = {
            "client_order_id": "abc",
            "orders": [
                {"symbol": "WETO", "status": "FILLED", "filled_quantity": "1"}
            ],
        }
        self.assertEqual(WebullAPI.order_status(detail), "FILLED")

    def test_falls_back_to_flat_top_level_status(self):
        detail = {"status": "FAILED"}
        self.assertEqual(WebullAPI.order_status(detail), "FAILED")

    def test_returns_none_for_an_empty_or_unrecognized_shape(self):
        self.assertIsNone(WebullAPI.order_status({}))
        self.assertIsNone(WebullAPI.order_status({"orders": []}))
        self.assertIsNone(WebullAPI.order_status({"orders": [{}]}))


def _fake_webull_api(**config_overrides):
    """A SimpleNamespace standing in for WebullAPI, with the real
    _quote_decimal/_sane_bid_or_ask implementations bound to it - lets
    tests exercise the real pricing methods (stock_limit_price etc.)
    without a real WebullAPI/SDK instance. _sane_bid_or_ask needs self
    (it reads self.config and calls self._quote_decimal), so it's bound
    via __get__ after fake_api exists rather than assigned directly like
    _quote_decimal (a plain @staticmethod, callable either way).
    """
    config = SimpleNamespace(
        quote_price_sanity_percent=Decimal("0.08"), **config_overrides
    )
    fake_api = SimpleNamespace(
        config=config,
        _quote_decimal=WebullAPI._quote_decimal,
        price_tick_size=WebullAPI.price_tick_size,
    )
    fake_api._sane_bid_or_ask = WebullAPI._sane_bid_or_ask.__get__(fake_api)
    fake_api._bid_ask_last_midpoint = WebullAPI._bid_ask_last_midpoint.__get__(fake_api)
    return fake_api


class PriceTickSizeTests(unittest.TestCase):
    """By request: smaller/cheaper stocks quote with real sub-penny
    precision (a live GAUZ quote showed bid=0.4592) - blanket-rounding
    every computed price to whole cents threw away up to a cent of real
    value per share on exactly the stocks where a cent is a meaningful
    fraction of the price.
    """

    def test_under_a_dollar_uses_sub_penny_precision(self):
        self.assertEqual(
            WebullAPI.price_tick_size(Decimal("0.4592")), Decimal("0.0001")
        )

    def test_a_dollar_or_more_uses_whole_cents(self):
        self.assertEqual(WebullAPI.price_tick_size(Decimal("1.00")), Decimal("0.01"))
        self.assertEqual(WebullAPI.price_tick_size(Decimal("20.15")), Decimal("0.01"))

    def test_stock_limit_price_keeps_sub_penny_precision_under_a_dollar(self):
        fake_api = _fake_webull_api(stock_limit_offset=Decimal("0"))
        price_fn = WebullAPI.stock_limit_price.__get__(fake_api)
        # BUY uses the bid/ask/last midpoint = (0.4592 + 0.4839) / 2 =
        # 0.47155, rounded DOWN to the nearest 0.0001 = 0.4715 - not
        # flattened to a whole cent (0.47).
        quote = {"bid": "0.4592", "ask": "0.4839"}
        self.assertEqual(price_fn(quote, "BUY"), Decimal("0.4715"))

    def test_stall_exit_price_keeps_sub_penny_precision_under_a_dollar(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(stock_entry_max_spread_percent=Decimal("5")),
            api=SimpleNamespace(
                quote_bid=lambda q: Decimal(str(q["bid"])),
                quote_ask=lambda q: Decimal(str(q["ask"])),
                price_tick_size=WebullAPI.price_tick_size,
            ),
        )
        price_fn = AutoTrader._stall_exit_price.__get__(fake_bot)
        # bid 0.4592 clears cost(0.41) + min_profit(0.01) + fee(0.001) -
        # must return the real bid, not a cent-flattened 0.45.
        result = price_fn(
            {"bid": "0.4592", "ask": "0.4839"},
            average_cost=Decimal("0.41"),
            min_profit=Decimal("0.01"),
            fee_per_share=Decimal("0.001"),
        )
        self.assertEqual(result, Decimal("0.4592"))


class ShortPricingTests(unittest.TestCase):
    def test_short_entry_uses_passive_mid_price(self):
        fake_api = _fake_webull_api(stock_limit_offset=Decimal("0.005"))
        price_fn = WebullAPI.stock_limit_price.__get__(fake_api)
        quote = {"bid": "10.00", "ask": "10.10"}
        self.assertEqual(price_fn(quote, "SHORT"), Decimal("10.05"))

    def test_cover_crosses_above_the_ask(self):
        fake_api = _fake_webull_api(stock_limit_offset=Decimal("0.01"))
        price_fn = WebullAPI.stock_limit_price.__get__(fake_api)
        quote = {"bid": "10.00", "ask": "10.10", "price": "10.05"}
        # 10.10 * 1.01 = 10.2010, quantized up to the next cent = 10.21.
        self.assertEqual(price_fn(quote, "COVER"), Decimal("10.21"))

    def test_sell_side_falls_back_to_price_when_bid_is_a_broken_quote(self):
        """Regression test for a live incident: FPE's ask sat at $20.08
        while its last-trade price stayed ~$17.7-17.8 for hours, and every
        exit-pricing path that trusted the raw ask submitted a limit order
        that could never fill. bid/ask readers must fall through to the
        quote's own last-trade price instead of trusting a bid/ask that
        diverges implausibly from it.
        """
        fake_api = _fake_webull_api(stock_limit_offset=Decimal("0.01"))
        price_fn = WebullAPI.stock_limit_price.__get__(fake_api)
        # bid (2.00) is wildly below price (10.00) - an insane read, not a
        # real market. Falls through to price, not the broken bid.
        quote = {"bid": "2.00", "ask": "10.10", "price": "10.00"}
        # 10.00 * (1 - 0.01) = 9.90.
        self.assertEqual(price_fn(quote, "SELL"), Decimal("9.90"))

    def test_cover_falls_back_to_price_when_ask_is_a_broken_quote(self):
        fake_api = _fake_webull_api(stock_limit_offset=Decimal("0.01"))
        price_fn = WebullAPI.stock_limit_price.__get__(fake_api)
        # ask (20.08) is wildly above price (17.81) - the exact FPE shape.
        quote = {"bid": "17.70", "ask": "20.08", "price": "17.81"}
        # 17.81 * 1.01 = 17.9881, quantized up to the next cent = 17.99.
        self.assertEqual(price_fn(quote, "COVER"), Decimal("17.99"))

    def test_quote_ask_returns_none_for_a_quote_that_diverges_from_price(self):
        fake_api = _fake_webull_api()
        ask_fn = WebullAPI.quote_ask.__get__(fake_api)
        self.assertIsNone(ask_fn({"ask": "20.08", "price": "17.81"}))

    def test_quote_bid_returns_none_for_a_quote_that_diverges_from_price(self):
        fake_api = _fake_webull_api()
        bid_fn = WebullAPI.quote_bid.__get__(fake_api)
        self.assertIsNone(bid_fn({"bid": "16.11", "price": "17.81"}))

    def test_quote_ask_passes_through_a_normal_quote(self):
        fake_api = _fake_webull_api()
        ask_fn = WebullAPI.quote_ask.__get__(fake_api)
        self.assertEqual(ask_fn({"ask": "17.85", "price": "17.81"}), Decimal("17.85"))


class PhantomExitReversalTests(unittest.TestCase):
    """Regression tests: record_realized_exit runs at order SUBMISSION
    time (an estimate off the limit price), not at confirmed fill - an
    order that's later found to have never filled must have that
    estimate reversed, or a cancelled/failed/abandoned exit permanently
    inflates the daily realized total as if it had actually happened.
    """

    def test_reverse_phantom_exit_undoes_a_recorded_profit(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            daily_realized_pnl=Decimal("0.50"),
            daily_realized_loss=Decimal("0"),
            daily_pnl=SimpleNamespace(record=lambda *a, **k: None),
        )
        reverse = AutoTrader.reverse_phantom_exit.__get__(fake_bot)
        reverse(Decimal("0.30"))
        self.assertEqual(fake_bot.daily_realized_pnl, Decimal("0.20"))
        self.assertEqual(fake_bot.daily_realized_loss, Decimal("0"))

    def test_reverse_phantom_exit_undoes_a_recorded_loss(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            daily_realized_pnl=Decimal("-0.40"),
            daily_realized_loss=Decimal("0.40"),
            daily_pnl=SimpleNamespace(record=lambda *a, **k: None),
        )
        reverse = AutoTrader.reverse_phantom_exit.__get__(fake_bot)
        reverse(Decimal("-0.40"))
        self.assertEqual(fake_bot.daily_realized_pnl, Decimal("0"))
        self.assertEqual(fake_bot.daily_realized_loss, Decimal("0"))

    def test_reverse_phantom_exit_is_a_noop_for_none_or_zero(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            daily_realized_pnl=Decimal("0.10"),
            daily_realized_loss=Decimal("0"),
            daily_pnl=SimpleNamespace(record=lambda *a, **k: None),
        )
        reverse = AutoTrader.reverse_phantom_exit.__get__(fake_bot)
        reverse(None)
        reverse(Decimal("0"))
        self.assertEqual(fake_bot.daily_realized_pnl, Decimal("0.10"))

    def test_monitor_working_orders_reverses_pnl_for_a_confirmed_cancel(self):
        """The order dropped out of open_orders (broker-side cancel, not
        one we requested) - order_detail confirms CANCELLED, so the pnl
        recorded when the PROFIT order was originally submitted must be
        reversed, not left counted as a real gain.
        """
        from webull_bot.bot import AutoTrader

        class FakeApi:
            def open_orders(self):
                return []

            @staticmethod
            def open_order_ids(groups):
                return []

            def order_detail(self, order_id):
                return {"status": "CANCELLED"}

            @staticmethod
            def order_status(detail):
                return detail.get("status")

        discarded_trades = []
        fake_bot = SimpleNamespace(
            config=SimpleNamespace(order_monitor_seconds=Decimal("0")),
            api=FakeApi(),
            status=SimpleNamespace(discard_trade=discarded_trades.append),
            last_order_monitor=0.0,
            working_orders={
                "order-1": {
                    "submitted_at": 0.0,
                    "key": "STOCK:ASHR",
                    "action": "PROFIT",
                    "cancel_requested_at": None,
                    "pnl": Decimal("0.05"),
                }
            },
            pending_stock_exits={"ASHR"},
            pending_option_exits=set(),
            daily_realized_pnl=Decimal("0.05"),
            daily_realized_loss=Decimal("0"),
            daily_pnl=SimpleNamespace(record=lambda *a, **k: None),
            last_account_refresh=0.0,
            consecutive_exit_failures=defaultdict(int),
        )
        fake_bot._release_pending_order = AutoTrader._release_pending_order.__get__(fake_bot)
        fake_bot.reverse_phantom_exit = AutoTrader.reverse_phantom_exit.__get__(fake_bot)
        fake_bot._note_exit_failure = AutoTrader._note_exit_failure.__get__(fake_bot)
        fake_bot._reverse_if_never_filled = AutoTrader._reverse_if_never_filled.__get__(fake_bot)
        monitor = AutoTrader.monitor_working_orders.__get__(fake_bot)

        with unittest.mock.patch("time.monotonic", return_value=100.0):
            monitor()

        self.assertEqual(fake_bot.daily_realized_pnl, Decimal("0"))
        self.assertNotIn("order-1", fake_bot.working_orders)
        # Regression coverage for a live incident: a cancelled order's
        # trade-log entry stayed on the dashboard's Recent Trades list
        # forever, labeled as a completed profit that never happened.
        self.assertEqual(discarded_trades, ["order-1"])
        # And for the endless-retry loop: a confirmed never-filled exit
        # must count toward should_force_market_exit's threshold.
        self.assertEqual(fake_bot.consecutive_exit_failures["ASHR"], 1)

    def test_monitor_working_orders_leaves_pnl_alone_on_a_confirmed_fill(self):
        from webull_bot.bot import AutoTrader

        class FakeApi:
            def open_orders(self):
                return []

            @staticmethod
            def open_order_ids(groups):
                return []

            def order_detail(self, order_id):
                return {"status": "FILLED"}

            @staticmethod
            def order_status(detail):
                return detail.get("status")

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(order_monitor_seconds=Decimal("0")),
            api=FakeApi(),
            last_order_monitor=0.0,
            working_orders={
                "order-1": {
                    "submitted_at": 0.0,
                    "key": "STOCK:ASHR",
                    "action": "PROFIT",
                    "cancel_requested_at": None,
                    "pnl": Decimal("0.05"),
                }
            },
            pending_stock_exits={"ASHR"},
            pending_option_exits=set(),
            daily_realized_pnl=Decimal("0.05"),
            daily_realized_loss=Decimal("0"),
            daily_pnl=SimpleNamespace(record=lambda *a, **k: None),
            last_account_refresh=0.0,
        )
        fake_bot._release_pending_order = AutoTrader._release_pending_order.__get__(fake_bot)
        fake_bot.reverse_phantom_exit = AutoTrader.reverse_phantom_exit.__get__(fake_bot)
        fake_bot._reverse_if_never_filled = AutoTrader._reverse_if_never_filled.__get__(fake_bot)
        monitor = AutoTrader.monitor_working_orders.__get__(fake_bot)

        with unittest.mock.patch("time.monotonic", return_value=100.0):
            monitor()

        self.assertEqual(fake_bot.daily_realized_pnl, Decimal("0.05"))

    def test_monitor_working_orders_fails_open_on_unrecognized_status(self):
        """The status field name isn't confirmed against a live payload -
        an unrecognized/missing shape must never trigger a reversal (that
        would be worse than an occasional unconfirmed phantom).
        """
        from webull_bot.bot import AutoTrader

        class FakeApi:
            def open_orders(self):
                return []

            @staticmethod
            def open_order_ids(groups):
                return []

            def order_detail(self, order_id):
                return {}

            @staticmethod
            def order_status(detail):
                return None

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(order_monitor_seconds=Decimal("0")),
            api=FakeApi(),
            last_order_monitor=0.0,
            working_orders={
                "order-1": {
                    "submitted_at": 0.0,
                    "key": "STOCK:ASHR",
                    "action": "PROFIT",
                    "cancel_requested_at": None,
                    "pnl": Decimal("0.05"),
                }
            },
            pending_stock_exits={"ASHR"},
            pending_option_exits=set(),
            daily_realized_pnl=Decimal("0.05"),
            daily_realized_loss=Decimal("0"),
            daily_pnl=SimpleNamespace(record=lambda *a, **k: None),
            last_account_refresh=0.0,
        )
        fake_bot._release_pending_order = AutoTrader._release_pending_order.__get__(fake_bot)
        fake_bot.reverse_phantom_exit = AutoTrader.reverse_phantom_exit.__get__(fake_bot)
        fake_bot._reverse_if_never_filled = AutoTrader._reverse_if_never_filled.__get__(fake_bot)
        monitor = AutoTrader.monitor_working_orders.__get__(fake_bot)

        with unittest.mock.patch("time.monotonic", return_value=100.0):
            monitor()

        self.assertEqual(fake_bot.daily_realized_pnl, Decimal("0.05"))

    def test_escalation_reverses_pnl_of_the_abandoned_order(self):
        """escalate_stalled_stop_losses deliberately cancels the gentle
        order and lets a fresh one fire its own PROFIT/STOP decision (and
        its own pnl) next cycle - the pnl recorded at the abandoned
        order's original submission must be reversed here, or the daily
        total double-counts the same logical exit.
        """
        from webull_bot.bot import AutoTrader

        discarded_trades = []
        fake_bot = SimpleNamespace(
            config=SimpleNamespace(stop_loss_escalate_seconds=15),
            api=SimpleNamespace(cancel=lambda order_id: None),
            status=SimpleNamespace(discard_trade=discarded_trades.append),
            stop_exit_submitted={"ASHR": 0.0},
            pending_stock_exits={"ASHR"},
            stop_loss_escalated=set(),
            working_orders={
                "order-1": {
                    "submitted_at": 0.0,
                    "key": "STOCK:ASHR",
                    "action": "PROFIT",
                    "cancel_requested_at": None,
                    "pnl": Decimal("0.07"),
                }
            },
            daily_realized_pnl=Decimal("0.07"),
            daily_realized_loss=Decimal("0"),
            daily_pnl=SimpleNamespace(record=lambda *a, **k: None),
            consecutive_exit_failures=defaultdict(int),
        )
        fake_bot.reverse_phantom_exit = AutoTrader.reverse_phantom_exit.__get__(fake_bot)
        fake_bot._note_exit_failure = AutoTrader._note_exit_failure.__get__(fake_bot)
        escalate = AutoTrader.escalate_stalled_stop_losses.__get__(fake_bot)

        with unittest.mock.patch("time.monotonic", return_value=20.0):
            escalate()

        self.assertEqual(fake_bot.daily_realized_pnl, Decimal("0"))
        self.assertEqual(discarded_trades, ["order-1"])
        self.assertEqual(fake_bot.consecutive_exit_failures["ASHR"], 1)


class ManualOrderDiscoveryTests(unittest.TestCase):
    """By request: an order the bot never submitted itself (almost
    always a manual action taken directly in the Webull app) used to
    just log an opaque order_id - "just says monitoring." Now fetches
    the real symbol/side/quantity and runs it through the same
    record_trade tracking a bot-driven trade gets.
    """

    @staticmethod
    def _fake_bot(
        order_detail_response,
        open_order_ids,
        raise_on_detail=False,
        submitted_order_ids_today=None,
    ):
        from webull_bot.bot import AutoTrader

        class FakeApi:
            def open_orders(self):
                return []

            @staticmethod
            def open_order_ids(groups):
                return open_order_ids

            def order_detail(self, order_id):
                if raise_on_detail:
                    raise RuntimeError("boom")
                return order_detail_response

        recorded = []
        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                order_monitor_seconds=Decimal("0"),
                order_timeout_seconds=120,
            ),
            api=FakeApi(),
            last_order_monitor=0.0,
            working_orders={},
            submitted_order_ids_today=(
                submitted_order_ids_today
                if submitted_order_ids_today is not None
                else set()
            ),
        )
        fake_bot.record_trade = (
            lambda key, order_id, action, quantity=None: recorded.append(
                (key, order_id, action, quantity)
            )
        )
        fake_bot.monitor = AutoTrader.monitor_working_orders.__get__(fake_bot)
        return fake_bot, recorded

    def test_extracts_symbol_and_side_and_runs_it_through_record_trade(self):
        fake_bot, recorded = self._fake_bot(
            {
                "orders": [
                    {"symbol": "ashr", "side": "BUY", "total_quantity": "5"}
                ]
            },
            ["order-1"],
        )
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            fake_bot.monitor()
        self.assertEqual(
            recorded, [("STOCK:ASHR", "order-1", "MANUAL_BUY", Decimal("5"))]
        )

    def test_a_sell_side_manual_order_is_recorded_as_manual_sell(self):
        fake_bot, recorded = self._fake_bot(
            {"orders": [{"symbol": "ASHR", "side": "SELL", "total_quantity": "5"}]},
            ["order-1"],
        )
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            fake_bot.monitor()
        self.assertEqual(recorded[0][2], "MANUAL_SELL")

    def test_falls_back_to_the_opaque_broker_order_when_detail_fetch_fails(self):
        fake_bot, recorded = self._fake_bot(
            None, ["order-1"], raise_on_detail=True
        )
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            with self.assertLogs("webull-bot", level="WARNING"):
                fake_bot.monitor()
        self.assertEqual(recorded, [])
        self.assertIn("order-1", fake_bot.working_orders)
        self.assertEqual(fake_bot.working_orders["order-1"]["action"], "UNKNOWN")

    def test_falls_back_when_detail_has_no_symbol(self):
        fake_bot, recorded = self._fake_bot(
            {"orders": [{"status": "SUBMITTED"}]}, ["order-1"]
        )
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            fake_bot.monitor()
        self.assertEqual(recorded, [])
        self.assertIn("order-1", fake_bot.working_orders)
        self.assertEqual(fake_bot.working_orders["order-1"]["action"], "UNKNOWN")

    def test_a_bot_owned_order_missing_from_working_orders_is_not_treated_as_manual(
        self,
    ):
        """Live incident: the fast volatility-scalp entry/exit repricers
        cancel-and-replace roughly every second, and there's a real
        window right after cancel() where the OLD order_id can still
        show up in open_orders() (broker-side latency) even though
        working_orders already dropped it in favor of the replacement
        order_id. That window was getting misread as a manual order -
        the bot's own normal repricing showed up mislabeled as manual
        activity. submitted_order_ids_today (already populated for
        every order the bot has ever placed today, via record_trade)
        must short-circuit this before the "unrecognized -> manual"
        detection ever runs.
        """
        fake_bot, recorded = self._fake_bot(
            {"orders": [{"symbol": "GAUZ", "side": "SELL", "total_quantity": "100"}]},
            ["order-1"],
            submitted_order_ids_today={"order-1"},
        )
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            fake_bot.monitor()
        self.assertEqual(recorded, [])
        self.assertNotIn("order-1", fake_bot.working_orders)


class OrderHistoryThrottleGroupTests(unittest.TestCase):
    """Live incident: order_history shared the "order" _call throttle
    group with real order placement/cancellation - once volatility-scalp
    started cancel-and-replacing every ~1s, this read-only, once-per-30-
    minutes audit call started losing out to real trading traffic for
    that budget and getting a sustained 429. It must use the separate
    "account" group instead, so it never competes with live order flow.
    """

    def test_order_history_uses_the_account_throttle_group_not_order(self):
        api = WebullAPI.__new__(WebullAPI)
        api.config = SimpleNamespace(account_id="acct-1")
        groups_used = []

        def fake_call(callback, group):
            groups_used.append(group)
            return []

        api._call = fake_call
        api.trade = SimpleNamespace(
            order_v3=SimpleNamespace(get_order_history=lambda **k: [])
        )

        api.order_history("2026-08-01", "2026-08-21")

        self.assertEqual(groups_used, ["account"])


class OrderHistoryReconciliationTests(unittest.TestCase):
    """reconcile_order_history is a log-only audit - it must never touch
    pnl, positions, or gating state, only log once per unrecognized
    order per day.
    """

    def _fake_bot(self, history, **overrides):
        from webull_bot.bot import AutoTrader

        defaults = dict(
            config=SimpleNamespace(
                order_history_reconcile_enabled=True,
                order_history_reconcile_seconds=1800,
            ),
            api=SimpleNamespace(order_history=lambda start, end: history),
            submitted_order_ids_today=set(),
            reconciliation_flagged_order_ids=set(),
            last_order_history_reconcile=0.0,
            now=lambda: datetime(2026, 8, 19, tzinfo=timezone.utc),
        )
        defaults.update(overrides)
        fake_bot = SimpleNamespace(**defaults)
        fake_bot.reconcile_order_history = AutoTrader.reconcile_order_history.__get__(
            fake_bot
        )
        return fake_bot

    @staticmethod
    def _today_place_time():
        # reconcile_order_history filters to orders placed today by
        # matching place_time_at's (UTC) date prefix - match that here so
        # fixtures aren't filtered out by the day-boundary check itself.
        return datetime.now(timezone.utc).date().isoformat() + "T12:00:00.000Z"

    def test_a_bot_submitted_order_is_not_flagged(self):
        history = [
            {
                "client_order_id": "abc123",
                "orders": [
                    {
                        "symbol": "AAPL",
                        "side": "BUY",
                        "status": "FILLED",
                        "place_time_at": self._today_place_time(),
                    }
                ],
            }
        ]
        fake_bot = self._fake_bot(history, submitted_order_ids_today={"abc123"})
        # Real time.monotonic() is relative to an arbitrary reference
        # point (often host boot) - on a freshly-booted CI runner it can
        # read under ORDER_HISTORY_RECONCILE_SECONDS, making
        # last_order_history_reconcile=0.0 look "still within the
        # throttle window" and skip the call under test entirely. Pin it
        # well above the threshold so the throttle check behaves the
        # same regardless of host uptime.
        with unittest.mock.patch("time.monotonic", return_value=1_000_000.0):
            with self.assertNoLogs("webull-bot", level="WARNING"):
                fake_bot.reconcile_order_history()

    def test_an_unrecognized_order_is_logged_once(self):
        history = [
            {
                "client_order_id": "manual-order-1",
                "orders": [
                    {
                        "symbol": "TSLA",
                        "side": "SELL",
                        "status": "FILLED",
                        "total_quantity": "5",
                        "filled_quantity": "5",
                        "place_time_at": self._today_place_time(),
                    }
                ],
            }
        ]
        fake_bot = self._fake_bot(history)
        with unittest.mock.patch("time.monotonic", return_value=1_000_000.0):
            with self.assertLogs("webull-bot", level="WARNING") as logs:
                fake_bot.reconcile_order_history()
        self.assertIn("manual-order-1", logs.output[0])
        self.assertIn("manual-order-1", fake_bot.reconciliation_flagged_order_ids)

        # A second run within the throttle window must not re-fetch or
        # re-log the same order.
        with unittest.mock.patch("time.monotonic", return_value=1_000_010.0):
            with self.assertNoLogs("webull-bot", level="WARNING"):
                fake_bot.reconcile_order_history()

    def test_disabled_never_calls_the_api(self):
        fake_bot = self._fake_bot(
            [{"client_order_id": "x", "orders": []}],
            config=SimpleNamespace(
                order_history_reconcile_enabled=False,
                order_history_reconcile_seconds=1800,
            ),
        )
        fake_bot.api.order_history = lambda start, end: (_ for _ in ()).throw(
            AssertionError("must not fetch when disabled")
        )
        fake_bot.reconcile_order_history()

    def test_a_fetch_failure_is_swallowed_and_logged(self):
        fake_bot = self._fake_bot([])
        fake_bot.api = SimpleNamespace(
            order_history=lambda start, end: (_ for _ in ()).throw(
                RuntimeError("boom")
            )
        )
        with unittest.mock.patch("time.monotonic", return_value=1_000_000.0):
            with self.assertLogs("webull-bot", level="WARNING") as logs:
                fake_bot.reconcile_order_history()
        self.assertIn("boom", logs.output[0])

    def test_throttled_within_the_interval(self):
        history = [
            {
                "client_order_id": "manual-order-2",
                "orders": [{"symbol": "MSFT", "side": "BUY", "status": "FILLED"}],
            }
        ]
        fake_bot = self._fake_bot(history)
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            fake_bot.last_order_history_reconcile = 100.0
        with unittest.mock.patch("time.monotonic", return_value=200.0):
            with self.assertNoLogs("webull-bot", level="WARNING"):
                fake_bot.reconcile_order_history()


class StrategyTuningLeverTests(unittest.TestCase):
    """apply_lever_adjustment is pure - no file I/O, no config mutation
    itself (the caller is responsible for actually editing config.py and
    running the full verification suite before committing). See
    strategy_tuning.py's own module docstring/comments for why each
    lever maps to the field it does.
    """

    def test_safety_denylist_has_no_overlap_with_any_lever_field(self):
        lever_fields = {spec.field for spec in LEVER_SPECS.values()}
        lever_fields.update(
            spec.enabled_field
            for spec in LEVER_SPECS.values()
            if spec.enabled_field
        )
        lever_fields.update(
            {
                "stock_core_session_position_fraction",
                "stock_whole_share_core_session_fraction",
            }
        )
        self.assertEqual(lever_fields & SAFETY_DENYLIST, set())

    def test_increase_moves_toward_the_maximum_for_a_direct_lever(self):
        # profit-target distance: increase_raises=True.
        result = apply_lever_adjustment(
            "profit-target distance",
            "increase",
            {"stock_target_stop_multiple": Decimal("2.0")},
            Decimal("0.10"),
        )
        # span = 5 - 0.5 = 4.5; step = 0.45.
        self.assertEqual(result.field, "stock_target_stop_multiple")
        self.assertEqual(result.new_value, Decimal("2.45"))

    def test_increase_moves_toward_the_minimum_for_an_inverted_lever(self):
        # stop-loss tightness: increase_raises=False (tighter = smaller
        # multiplier), so "increase" (tightness) LOWERS the field.
        result = apply_lever_adjustment(
            "stop-loss tightness",
            "increase",
            {"stock_stop_loss_range_multiplier": Decimal("1.0")},
            Decimal("0.10"),
        )
        # span = 5 - 0 = 5; step = 0.5.
        self.assertEqual(result.new_value, Decimal("0.5"))

    def test_decrease_is_the_exact_opposite_of_increase(self):
        result = apply_lever_adjustment(
            "entry selectivity",
            "decrease",
            {"reenter_confirmation_polls": Decimal("10")},
            Decimal("0.10"),
        )
        # span = 20 - 1 = 19; step = 1.9.
        self.assertEqual(result.new_value, Decimal("8.1"))

    def test_clamps_at_the_maximum_instead_of_overshooting(self):
        result = apply_lever_adjustment(
            "profit-target distance",
            "increase",
            {"stock_target_stop_multiple": Decimal("4.9")},
            Decimal("0.10"),
        )
        self.assertEqual(result.new_value, Decimal("5"))

    def test_returns_none_when_already_at_the_bound(self):
        result = apply_lever_adjustment(
            "profit-target distance",
            "increase",
            {"stock_target_stop_multiple": Decimal("5")},
            Decimal("0.10"),
        )
        self.assertIsNone(result)

    def test_enable_disable_direction_never_produces_a_numeric_adjustment(self):
        result = apply_lever_adjustment(
            "time-aware-stop widen window",
            "enable",
            {"time_aware_stop_widen_seconds": Decimal("60")},
            Decimal("0.10"),
        )
        self.assertIsNone(result)

    def test_unknown_lever_is_a_safe_noop(self):
        result = apply_lever_adjustment(
            "made up lever", "increase", {}, Decimal("0.10")
        )
        self.assertIsNone(result)

    def test_fractional_whole_share_balance_shifts_both_fields_preserving_the_sum(self):
        result = apply_lever_adjustment(
            "fractional-vs-whole-share balance",
            "increase",
            {
                "stock_core_session_position_fraction": Decimal("0.30"),
                "stock_whole_share_core_session_fraction": Decimal("0.70"),
            },
            Decimal("0.10"),
        )
        self.assertEqual(result.new_value, Decimal("0.40"))
        self.assertEqual(result.paired_new_value, Decimal("0.60"))
        self.assertEqual(result.new_value + result.paired_new_value, Decimal("1"))

    def test_fractional_whole_share_balance_decrease_shifts_toward_whole_share(self):
        result = apply_lever_adjustment(
            "fractional-vs-whole-share balance",
            "decrease",
            {
                "stock_core_session_position_fraction": Decimal("0.30"),
                "stock_whole_share_core_session_fraction": Decimal("0.70"),
            },
            Decimal("0.10"),
        )
        self.assertEqual(result.new_value, Decimal("0.20"))
        self.assertEqual(result.paired_new_value, Decimal("0.80"))

    def test_fractional_whole_share_balance_clamps_and_still_sums_to_one(self):
        result = apply_lever_adjustment(
            "fractional-vs-whole-share balance",
            "increase",
            {
                "stock_core_session_position_fraction": Decimal("0.95"),
                "stock_whole_share_core_session_fraction": Decimal("0.05"),
            },
            Decimal("0.10"),
        )
        self.assertEqual(result.new_value, Decimal("1"))
        self.assertEqual(result.paired_new_value, Decimal("0"))


class StrategyTuningStateTests(unittest.TestCase):
    def test_a_never_adjusted_lever_is_ready_immediately(self):
        path = Path("tests/.generated_status/strategy_tuning.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            state = StrategyTuningState(str(path))
            self.assertTrue(state.ready("profit-target distance", 24))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_a_recently_adjusted_lever_is_not_ready_within_the_cooldown(self):
        path = Path("tests/.generated_status/strategy_tuning2.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            state = StrategyTuningState(str(path))
            state.record("profit-target distance")
            self.assertFalse(state.ready("profit-target distance", 24))
            # A different lever is unaffected.
            self.assertTrue(state.ready("entry selectivity", 24))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_ready_again_after_the_cooldown_elapses(self):
        path = Path("tests/.generated_status/strategy_tuning3.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            state = StrategyTuningState(str(path))
            state._last_applied["profit-target distance"] = (
                time.time() - 25 * 3600
            )
            state.path.parent.mkdir(parents=True, exist_ok=True)
            state.path.write_text(
                json.dumps(state._last_applied), encoding="utf-8"
            )
            reloaded = StrategyTuningState(str(path))
            self.assertTrue(reloaded.ready("profit-target distance", 24))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_state_persists_across_instances(self):
        path = Path("tests/.generated_status/strategy_tuning4.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            StrategyTuningState(str(path)).record("position size")
            reloaded = StrategyTuningState(str(path))
            self.assertFalse(reloaded.ready("position size", 24))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
