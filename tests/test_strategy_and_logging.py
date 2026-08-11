import json
import logging
import shutil
import statistics
import sys
import threading
import time
import unittest
import unittest.mock
from collections import deque
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
            micro_scalp_reference_window=20,
            micro_scalp_dip_cents=Decimal("0.05"),
            micro_scalp_target_cents=Decimal("0.06"),
            micro_scalp_stop_cents=Decimal("0.10"),
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

    def test_micro_scalp_reference_price_is_none_without_data(self):
        strategy = TradingStrategy(self.config())
        self.assertIsNone(strategy.micro_scalp_reference_price("TSLA"))

    def test_micro_scalp_decision_buys_a_small_dip_below_reference(self):
        strategy = TradingStrategy(self.config())
        for _ in range(5):
            strategy.update_stock_snapshot(
                {"symbol": "TSLA", "bid": "99.99", "ask": "100.01"},
                Decimal("100.00"),
            )
        strategy.update_stock_snapshot(
            {"symbol": "TSLA", "bid": "99.89", "ask": "99.91"},
            Decimal("99.90"),
        )
        decision = strategy.micro_scalp_decision(
            "STOCK:TSLA",
            Decimal("99.90"),
            0,
            Decimal("0"),
        )
        self.assertEqual(decision.action, "BUY")

    def test_micro_scalp_decision_holds_without_a_qualifying_dip(self):
        strategy = TradingStrategy(self.config())
        for _ in range(5):
            strategy.update_stock_snapshot(
                {"symbol": "TSLA", "bid": "99.99", "ask": "100.01"},
                Decimal("100.00"),
            )
        strategy.update_stock_snapshot(
            {"symbol": "TSLA", "bid": "99.97", "ask": "99.99"},
            Decimal("99.98"),
        )
        decision = strategy.micro_scalp_decision(
            "STOCK:TSLA",
            Decimal("99.98"),
            0,
            Decimal("0"),
        )
        self.assertEqual(decision.action, "HOLD")

    def test_micro_scalp_decision_takes_profit_on_the_bounce(self):
        strategy = TradingStrategy(self.config())
        decision = strategy.micro_scalp_decision(
            "STOCK:TSLA",
            Decimal("100.07"),
            10,
            Decimal("100.00"),
        )
        self.assertEqual(decision.action, "PROFIT")

    def test_micro_scalp_decision_cuts_loss_if_dip_keeps_falling(self):
        strategy = TradingStrategy(self.config())
        decision = strategy.micro_scalp_decision(
            "STOCK:TSLA",
            Decimal("99.85"),
            10,
            Decimal("100.00"),
        )
        self.assertEqual(decision.action, "LOSS")

    def test_clear_market_state_resets_micro_scalp_reference(self):
        strategy = TradingStrategy(self.config())
        strategy.update_stock_snapshot(
            {"symbol": "TSLA", "bid": "99.99", "ask": "100.01"},
            Decimal("100.00"),
        )
        self.assertIsNotNone(strategy.micro_scalp_reference_price("TSLA"))
        strategy.clear_market_state()
        self.assertIsNone(strategy.micro_scalp_reference_price("TSLA"))


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
            stop_exit_submitted={"ASHR": 0.0},
            pending_stock_exits={"ASHR"},
            stop_loss_escalated=set(),
            working_orders={
                "order-1": {
                    "submitted_at": 0.0,
                    "key": "STOCK:ASHR",
                    "action": "PROFIT",
                    "cancel_requested_at": None,
                }
            },
        )
        escalate = AutoTrader.escalate_stalled_stop_losses.__get__(fake_bot)

        with unittest.mock.patch("time.monotonic", return_value=20.0):
            escalate()

        self.assertEqual(cancelled, ["order-1"])
        self.assertIn("ASHR", fake_bot.stop_loss_escalated)
        self.assertNotIn("ASHR", fake_bot.pending_stock_exits)
        self.assertNotIn("ASHR", fake_bot.stop_exit_submitted)

    def test_escalated_profit_order_uses_aggressive_price_not_stale_target(self):
        """End-to-end via the real trade_micro_scalp() path: once a PROFIT
        exit is escalated, the resubmission must use the current aggressive
        crossing price, not the stale theoretical target - the whole point
        is to actually get a fill instead of re-quoting the exact price
        that already failed to fill.
        """
        from collections import defaultdict, deque

        from webull_bot.bot import AutoTrader

        config = Settings(
            micro_scalp_enabled=True,
            micro_scalp_symbols="ASHR",
            micro_scalp_target_cents=Decimal("0.06"),
            trade_cooldown_seconds=Decimal("0"),
        )
        strategy = TradingStrategy(config)
        quote = {"symbol": "ASHR", "bid": "34.09", "ask": "34.11", "price": "34.10"}

        placed = []

        class FakeApi:
            def stock_quotes_resilient(self, symbols, category):
                return [quote], set()

            @staticmethod
            def quote_price(q):
                return Decimal(str(q["price"]))

            @staticmethod
            def stock_position(symbol, positions):
                for item in positions:
                    if item.get("symbol") == symbol:
                        return (
                            Decimal(str(item.get("quantity", "0"))),
                            Decimal(str(item.get("cost_price", "0"))),
                        )
                return Decimal("0"), Decimal("0")

            def stock_limit_price(self, q, side):
                return Decimal("34.00")

            @staticmethod
            def quote_ask(q):
                return Decimal(str(q["ask"]))

            def place_stock(self, symbol, side, quantity, limit_price=None, fractional=False):
                placed.append((symbol, side, quantity, limit_price))
                return "order-1"

        fake_bot = SimpleNamespace(
            config=config,
            api=FakeApi(),
            strategy=strategy,
            wash_sales=SimpleNamespace(blocked_until=lambda symbol: None),
            status=SimpleNamespace(record_trade=lambda *a, **k: None),
            last_trade={},
            last_exit_at={},
            trade_times=defaultdict(deque),
            pending_stock_exits=set(),
            stop_exit_submitted={},
            stop_loss_escalated={"ASHR"},
            position_buckets={},
            working_orders={},
            broker_conflict_symbols=set(),
            daily_realized_pnl=Decimal("0"),
            daily_realized_loss=Decimal("0"),
            daily_pnl=SimpleNamespace(record=lambda *a, **k: None),
        )
        fake_bot.is_broker_position_conflict = AutoTrader.is_broker_position_conflict
        fake_bot.is_fractional_trading_not_enabled = (
            AutoTrader.is_fractional_trading_not_enabled
        )
        for name in (
            "cooldown_ready",
            "rate_capped",
            "reentry_cooldown_ready",
            "record_trade",
            "record_realized_exit",
            "stop_ready_to_submit",
            "trade_micro_scalp",
        ):
            setattr(fake_bot, name, getattr(AutoTrader, name).__get__(fake_bot))

        positions = [
            {
                "instrument_type": "EQUITY",
                "symbol": "ASHR",
                "quantity": "1",
                "cost_price": "34.00",
            }
        ]
        fake_bot.trade_micro_scalp(positions, Decimal("10000"))

        self.assertEqual(len(placed), 1)
        self.assertEqual(placed[0][:3], ("ASHR", "SELL", Decimal("1")))
        self.assertEqual(placed[0][3], Decimal("34.00"))
        self.assertIn("ASHR", fake_bot.stop_exit_submitted)

    def test_profit_exit_never_prices_below_target_even_if_ask_has_fallen(self):
        """A PROFIT decision can fire off a last-trade print (quote_price)
        that's already stale relative to the current book - if the ask has
        since dropped below the target, pricing the exit at that ask
        directly (the old `ask or target` logic) can execute at a real
        loss while still being logged as PROFIT. The floor must win.
        """
        from collections import defaultdict, deque

        from webull_bot.bot import AutoTrader

        config = Settings(
            micro_scalp_enabled=True,
            micro_scalp_symbols="ASHR",
            micro_scalp_target_cents=Decimal("0.06"),
            trade_cooldown_seconds=Decimal("0"),
        )
        strategy = TradingStrategy(config)
        # Last trade printed at 34.10 (above the 34.06 target, triggering
        # PROFIT), but the current ask has already fallen to 34.02 - below
        # the target and below the 34.00 entry cost.
        quote = {"symbol": "ASHR", "bid": "34.00", "ask": "34.02", "price": "34.10"}

        placed = []

        class FakeApi:
            def stock_quotes_resilient(self, symbols, category):
                return [quote], set()

            @staticmethod
            def quote_price(q):
                return Decimal(str(q["price"]))

            @staticmethod
            def stock_position(symbol, positions):
                for item in positions:
                    if item.get("symbol") == symbol:
                        return (
                            Decimal(str(item.get("quantity", "0"))),
                            Decimal(str(item.get("cost_price", "0"))),
                        )
                return Decimal("0"), Decimal("0")

            def stock_limit_price(self, q, side):
                return Decimal("33.90")

            @staticmethod
            def quote_ask(q):
                return Decimal(str(q["ask"]))

            def place_stock(self, symbol, side, quantity, limit_price=None, fractional=False):
                placed.append((symbol, side, quantity, limit_price))
                return "order-1"

        fake_bot = SimpleNamespace(
            config=config,
            api=FakeApi(),
            strategy=strategy,
            wash_sales=SimpleNamespace(blocked_until=lambda symbol: None),
            status=SimpleNamespace(record_trade=lambda *a, **k: None),
            last_trade={},
            last_exit_at={},
            trade_times=defaultdict(deque),
            pending_stock_exits=set(),
            stop_exit_submitted={},
            stop_loss_escalated=set(),
            position_buckets={},
            working_orders={},
            broker_conflict_symbols=set(),
            daily_realized_pnl=Decimal("0"),
            daily_realized_loss=Decimal("0"),
            daily_pnl=SimpleNamespace(record=lambda *a, **k: None),
        )
        fake_bot.is_broker_position_conflict = AutoTrader.is_broker_position_conflict
        fake_bot.is_fractional_trading_not_enabled = (
            AutoTrader.is_fractional_trading_not_enabled
        )
        for name in (
            "cooldown_ready",
            "rate_capped",
            "reentry_cooldown_ready",
            "record_trade",
            "record_realized_exit",
            "stop_ready_to_submit",
            "trade_micro_scalp",
        ):
            setattr(fake_bot, name, getattr(AutoTrader, name).__get__(fake_bot))

        positions = [
            {
                "instrument_type": "EQUITY",
                "symbol": "ASHR",
                "quantity": "1",
                "cost_price": "34.00",
            }
        ]
        fake_bot.trade_micro_scalp(positions, Decimal("10000"))

        self.assertEqual(len(placed), 1)
        # Must price at the 34.08 target (34.06 micro-scalp target plus the
        # $0.02 flat sell fee, spread over the 1-share quantity), not the
        # fallen 34.02 ask.
        self.assertEqual(placed[0][3], Decimal("34.08"))
        self.assertGreater(placed[0][3], Decimal("34.00"))  # never below cost


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

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(order_monitor_seconds=Decimal("5")),
            api=FakeApi(),
            last_reprice=0.0,
            stop_loss_escalated=set(),
            pending_stock_exits={"ASHR"},
            stop_exit_submitted={"ASHR": 12345.0},
            daily_realized_pnl=Decimal("0"),
            daily_realized_loss=Decimal("0"),
            is_fractional_quantity=AutoTrader.is_fractional_quantity,
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

    def test_reprice_skips_when_ask_unchanged(self):
        from webull_bot.bot import AutoTrader

        calls = []
        quote = {"symbol": "ASHR", "bid": "33.98", "ask": "34.00", "price": "33.99"}

        class FakeApi:
            @staticmethod
            def stock_quote(symbol):
                return quote

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
            stop_loss_escalated=set(),
            pending_stock_exits={"ASHR"},
            stop_exit_submitted={"ASHR": 5.0},
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
            stop_loss_escalated=set(),
            pending_stock_exits={"ASHR"},
            stop_exit_submitted={"ASHR": 12345.0},
            daily_realized_pnl=Decimal("0"),
            daily_realized_loss=Decimal("0"),
            is_fractional_quantity=AutoTrader.is_fractional_quantity,
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


class MicroScalpIntegrationTests(unittest.TestCase):
    def test_trade_micro_scalp_buys_a_qualifying_dip_end_to_end(self):
        from collections import defaultdict, deque

        from webull_bot.bot import AutoTrader

        config = Settings(
            micro_scalp_enabled=True,
            micro_scalp_symbols="TSLA",
            micro_scalp_dip_cents=Decimal("0.05"),
            micro_scalp_capital_fraction=Decimal("1.0"),
            micro_scalp_max_positions=1,
            max_open_positions=5,
            trade_cooldown_seconds=Decimal("0"),
            stock_max_trades_per_hour=10,
        )
        strategy = TradingStrategy(config)
        for _ in range(5):
            strategy.update_stock_snapshot(
                {"symbol": "TSLA", "bid": "99.99", "ask": "100.01"},
                Decimal("100.00"),
            )

        quote = {"symbol": "TSLA", "bid": "99.89", "ask": "99.91", "price": "99.90"}

        class FakeApi:
            def __init__(self):
                self.placed = []

            def stock_quotes_resilient(self, symbols, category):
                return [quote], set()

            @staticmethod
            def quote_price(q):
                return Decimal(str(q["price"]))

            @staticmethod
            def stock_position(symbol, positions):
                for item in positions:
                    if item.get("symbol") == symbol:
                        return (
                            int(item.get("quantity", 0)),
                            Decimal(str(item.get("cost_price", "0"))),
                        )
                return 0, Decimal("0")

            def stock_limit_price(self, q, side):
                return Decimal(str(q["ask"] if side == "BUY" else q["bid"]))

            def place_stock(self, symbol, side, quantity, limit_price=None, fractional=False):
                self.placed.append((symbol, side, quantity, limit_price))
                return "order-1"

        api = FakeApi()
        fake_bot = SimpleNamespace(
            config=config,
            api=api,
            strategy=strategy,
            wash_sales=SimpleNamespace(blocked_until=lambda symbol: None),
            status=SimpleNamespace(record_trade=lambda *a, **k: None),
            last_trade={},
            last_exit_at={},
            trade_times=defaultdict(deque),
            pending_stock_exits=set(),
            stop_exit_submitted={},
            stop_loss_escalated=set(),
            position_buckets={},
            working_orders={},
            broker_conflict_symbols=set(),
        )
        for name in (
            "cooldown_ready",
            "rate_capped",
            "reentry_cooldown_ready",
            "record_trade",
            "record_realized_exit",
            "stop_ready_to_submit",
            "trade_micro_scalp",
        ):
            setattr(fake_bot, name, getattr(AutoTrader, name).__get__(fake_bot))

        positions: list[dict] = []
        buying_power = Decimal("10000")
        result = fake_bot.trade_micro_scalp(positions, buying_power)

        self.assertEqual(len(api.placed), 1)
        self.assertEqual(api.placed[0][:2], ("TSLA", "BUY"))
        self.assertTrue(any(p["symbol"] == "TSLA" for p in positions))
        self.assertLess(result, buying_power)
        self.assertEqual(fake_bot.position_buckets.get("TSLA"), "MICRO_SCALP")

    def test_trade_micro_scalp_disabled_returns_buying_power_unchanged(self):
        from webull_bot.bot import AutoTrader

        config = Settings(micro_scalp_enabled=False)
        fake_bot = SimpleNamespace(config=config)
        trade_micro_scalp = AutoTrader.trade_micro_scalp.__get__(fake_bot)

        result = trade_micro_scalp([], Decimal("5000"))

        self.assertEqual(result, Decimal("5000"))

    def test_trade_micro_scalp_skips_symbols_in_broker_conflict(self):
        """A symbol Webull has already rejected as a position conflict must
        not get any decision/order attempts - no quote snapshot, no BUY/
        SELL - until the conflict is manually resolved (or the day resets).
        Position bookkeeping (held_count) still sees it, which is fine.
        """
        from webull_bot.bot import AutoTrader

        config = Settings(micro_scalp_enabled=True, micro_scalp_symbols="TSLA")

        class UntouchableApi:
            def stock_quotes_resilient(self, symbols, category):
                return [{"symbol": "TSLA", "bid": "89.00", "ask": "89.20"}], set()

            @staticmethod
            def stock_position(symbol, positions):
                return Decimal("0"), Decimal("0")

        def forbidden(*args, **kwargs):
            raise AssertionError("must not evaluate a conflicted symbol")

        fake_bot = SimpleNamespace(
            config=config,
            api=UntouchableApi(),
            strategy=SimpleNamespace(
                update_stock_snapshot=forbidden,
                open_position_count=TradingStrategy.open_position_count,
            ),
            broker_conflict_symbols={"TSLA"},
        )
        trade_micro_scalp = AutoTrader.trade_micro_scalp.__get__(fake_bot)

        result = trade_micro_scalp([], Decimal("10000"))

        self.assertEqual(result, Decimal("10000"))

    def test_failed_stop_resubmission_clears_escalation_instead_of_retrying_forever(self):
        """Regression test: a stop-loss order that keeps failing (bad
        request, transient API error, whatever) must not stay in
        stop_loss_escalated forever - that flag bypasses the normal
        cooldown entirely, so a persistent failure would otherwise retry on
        every single poll cycle indefinitely instead of backing off.
        """
        from collections import defaultdict, deque

        from webull_bot.bot import AutoTrader

        config = Settings(micro_scalp_enabled=True, micro_scalp_symbols="TSLA")
        strategy = TradingStrategy(config)

        quote = {"symbol": "TSLA", "bid": "89.00", "ask": "89.20", "price": "89.10"}

        class FailingApi:
            def stock_quotes_resilient(self, symbols, category):
                return [quote], set()

            @staticmethod
            def quote_price(q):
                return Decimal(str(q["price"]))

            @staticmethod
            def stock_position(symbol, positions):
                for item in positions:
                    if item.get("symbol") == symbol:
                        return (
                            Decimal(str(item.get("quantity", "0"))),
                            Decimal(str(item.get("cost_price", "0"))),
                        )
                return Decimal("0"), Decimal("0")

            def stock_limit_price(self, q, side):
                return Decimal(str(q["bid"]))

            @staticmethod
            def quote_ask(q):
                return Decimal(str(q["ask"]))

            def place_stock(self, *args, **kwargs):
                raise RuntimeError("boom")

        wash_sale_calls = []
        fake_bot = SimpleNamespace(
            config=config,
            api=FailingApi(),
            strategy=strategy,
            wash_sales=SimpleNamespace(
                blocked_until=lambda symbol: None,
                block=lambda symbol, reason: wash_sale_calls.append((symbol, reason)),
            ),
            status=SimpleNamespace(record_trade=lambda *a, **k: None),
            last_trade={},
            last_exit_at={},
            trade_times=defaultdict(deque),
            pending_stock_exits=set(),
            pending_option_exits=set(),
            stop_exit_submitted={},
            stop_loss_escalated={"TSLA"},
            position_buckets={},
            working_orders={},
            daily_realized_pnl=Decimal("0"),
            daily_realized_loss=Decimal("0"),
            broker_conflict_symbols=set(),
        )
        fake_bot.is_broker_position_conflict = AutoTrader.is_broker_position_conflict
        fake_bot.is_fractional_trading_not_enabled = (
            AutoTrader.is_fractional_trading_not_enabled
        )
        for name in (
            "cooldown_ready",
            "rate_capped",
            "reentry_cooldown_ready",
            "record_trade",
            "record_realized_exit",
            "stop_ready_to_submit",
            "trade_micro_scalp",
            "handle_broker_conflict",
        ):
            setattr(fake_bot, name, getattr(AutoTrader, name).__get__(fake_bot))

        positions = [
            {
                "instrument_type": "EQUITY",
                "symbol": "TSLA",
                "quantity": "3",
                "cost_price": "100.00",
            }
        ]
        fake_bot.trade_micro_scalp(positions, Decimal("10000"))

        self.assertNotIn("TSLA", fake_bot.stop_loss_escalated)
        self.assertNotIn("TSLA", fake_bot.pending_stock_exits)
        self.assertEqual(wash_sale_calls, [])


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


class ResearchDiscoveryTests(unittest.TestCase):
    def test_empty_or_truncated_completion_does_not_raise(self):
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        self.assertEqual(agent._parse_response(""), {})
        self.assertEqual(agent._parse_response(None), {})

    def test_salvage_assessments_extracts_complete_objects_before_a_cutoff(self):
        # Two complete assessment objects, then a third cut off mid-string -
        # exactly the "Unterminated string" shape a real truncation produces.
        truncated = (
            '{"market_direction":0.2,"market_volatility":0.5,"assessments":['
            '{"symbol":"NVDA","priority":0.8},'
            '{"symbol":"TSLA","priority":0.6},'
            '{"symbol":"AMD","priority":0.3,"catalyst_strength":"unterminat'
        )
        salvaged = MarketResearchAgent._salvage_assessments(truncated)
        self.assertEqual([item["symbol"] for item in salvaged], ["NVDA", "TSLA"])

    def test_salvage_assessments_ignores_objects_without_a_symbol_key(self):
        text = '{"market_direction":0.2,"nested":{"foo":"bar"}}'
        self.assertEqual(MarketResearchAgent._salvage_assessments(text), [])

    def test_parse_response_recovers_partial_assessments_from_truncation(self):
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.log = logging.getLogger("test-agent")
        truncated = (
            '{"market_direction":0.2,"market_volatility":0.5,"assessments":['
            '{"symbol":"NVDA","priority":0.8},'
            '{"symbol":"TSLA","priority":0.6},'
            '{"symbol":"AMD","catalyst_strength":"unterminat'
        )
        with self.assertLogs("test-agent", level="WARNING") as logs:
            parsed = agent._parse_response(truncated)
        self.assertEqual(
            [item["symbol"] for item in parsed["assessments"]], ["NVDA", "TSLA"]
        )
        self.assertIn("salvaged", logs.output[0])

    def test_parse_response_still_raises_when_nothing_is_salvageable(self):
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.log = logging.getLogger("test-agent")
        with self.assertRaises(json.JSONDecodeError):
            agent._parse_response('{"market_direction": "unterminat')

    def test_parse_response_salvages_a_balanced_but_internally_broken_object(self):
        """Regression test: a top-level object whose braces are perfectly
        balanced but has a syntax error INSIDE it (e.g. a missing comma
        between two assessment objects - "Expecting ',' delimiter" from a
        real production response) used to propagate straight out of
        _parse_response uncaught. _extract_json_object found a candidate
        (braces balance fine), but json.loads(candidate) itself raised and
        that call sat outside any try/except - the salvage path never even
        ran for this failure shape, only for a genuinely truncated one.
        """
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.log = logging.getLogger("test-agent")
        # Balanced overall, but missing the comma between the two
        # assessment objects in the array.
        broken = (
            '{"market_direction":0.2,"market_volatility":0.5,"assessments":['
            '{"symbol":"NVDA","priority":0.8}'
            '{"symbol":"TSLA","priority":0.6}'
            ']}'
        )
        with self.assertLogs("test-agent", level="WARNING") as logs:
            parsed = agent._parse_response(broken)
        self.assertEqual(
            [item["symbol"] for item in parsed["assessments"]], ["NVDA", "TSLA"]
        )
        self.assertIn("salvaged", logs.output[0])

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

    def test_submit_resets_budget_at_the_new_sessions_market_open(self):
        import queue as queue_module

        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(
            agent_daily_request_limit=250,
            agent_daily_token_budget=90000,
            market_open_time="04:00",
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
        agent._interval_seconds = lambda: 0
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
            agent.submit({"a": 1})
        # Before market open - still yesterday's session, budget untouched.
        self.assertEqual(agent._requests_today, 200)

        with unittest.mock.patch(
            "webull_bot.market_agent.datetime"
        ) as mock_datetime:
            mock_datetime.now.return_value = datetime(
                2026, 8, 6, 5, 0, tzinfo=timezone.utc
            )
            agent.submit({"a": 1})
        # Past market open - new session, budget resets to 0 (submit()
        # only resets/enqueues; _research() is what later advances the
        # count, on the worker thread this test doesn't run).
        self.assertEqual(agent._requests_today, 0)
        self.assertEqual(agent._request_date, datetime(2026, 8, 6).date())

    def test_submit_force_bypasses_the_interval_throttle(self):
        """force=True must actually skip the interval wait - it was
        previously accepted as a parameter but never read anywhere in
        submit(), so a forced post-liquidation reevaluation
        (submit_agent_research(..., force=True)) silently behaved
        identically to a routine submit and could sit rate-limited for
        minutes instead of firing immediately.
        """
        import queue as queue_module
        import time as time_module

        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(
            agent_daily_request_limit=250,
            agent_daily_token_budget=90000,
            market_open_time="00:00",
            session_time=lambda value: datetime_time(0, 0),
        )
        agent._request_date = datetime.now(timezone.utc).date()
        agent._requests_today = 0
        agent._limit_logged_date = None
        agent._last_submitted = time_module.monotonic()  # "just submitted"
        agent._timezone = timezone.utc
        agent._interval_seconds = lambda: 120
        agent._work = queue_module.Queue(maxsize=1)
        agent._rate_limit_blocked = False
        agent._token_usage_log = []
        agent._token_limit_logged_at = 0.0
        agent.log = logging.getLogger("test-agent")

        agent.submit({"a": 1}, force=False)
        self.assertTrue(agent._work.empty())  # still within the interval

        agent.submit({"a": 1}, force=True)
        self.assertFalse(agent._work.empty())  # force bypassed the wait

    def test_submit_respects_rolling_token_budget_and_rate_limit_block(self):
        """The interval throttle alone doesn't protect against Groq's real
        tokens-per-day cap - a request can be perfectly on-schedule and
        still 429 if the account's rolling 24h usage is near its limit.
        submit() must refuse to queue work in either case: usage already
        near budget, or a prior 429 having blocked the rest of the session.
        """
        import queue as queue_module

        # A fixed monotonic clock, not the real one - submit()'s interval
        # check compares elapsed-since-_last_submitted against this, and
        # the real clock's absolute value depends on how long the host has
        # been up, which is not something a test should depend on.
        now = 100_000.0

        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(
            agent_daily_request_limit=250,
            agent_daily_token_budget=1000,
            market_open_time="00:00",
            session_time=lambda value: datetime_time(0, 0),
        )
        agent._request_date = datetime.now(timezone.utc).date()
        agent._requests_today = 0
        agent._limit_logged_date = None
        agent._last_submitted = 0.0
        agent._timezone = timezone.utc
        agent._interval_seconds = lambda: 120
        agent._rate_limit_blocked = False
        agent._token_limit_logged_at = 0.0
        agent.log = logging.getLogger("test-agent")

        # Over the token budget, even though the interval has long elapsed.
        agent._work = queue_module.Queue(maxsize=1)
        agent._token_usage_log = [(now, 1500)]
        with unittest.mock.patch("time.monotonic", return_value=now):
            agent.submit({"a": 1})
        self.assertTrue(agent._work.empty())

        # Under budget and past the interval - goes through normally.
        agent._token_usage_log = [(now, 100)]
        with unittest.mock.patch("time.monotonic", return_value=now):
            agent.submit({"a": 1})
        self.assertFalse(agent._work.empty())

        # A prior 429 blocks submission for the rest of the session, even
        # with budget free and the interval elapsed - no backoff timer to
        # wait out, it just stays blocked until the next _session_date.
        agent._work = queue_module.Queue(maxsize=1)
        agent._last_submitted = 0.0
        agent._token_usage_log = []
        agent._rate_limit_blocked = True
        with unittest.mock.patch("time.monotonic", return_value=now):
            agent.submit({"a": 1})
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

    def test_rate_limit_error_blocks_research_for_the_rest_of_the_session(self):
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.log = logging.getLogger("test-agent")
        agent._rate_limit_blocked = False

        error = RuntimeError(
            "Error code: 429 - {'error': {'message': 'Rate limit reached "
            "... Please try again in 16m38.784s.', 'type': 'compound', "
            "'code': 'rate_limit_exceeded'}}"
        )
        with self.assertLogs("test-agent", level="WARNING") as logs:
            agent._handle_research_error(error)

        self.assertTrue(agent._rate_limit_blocked)
        self.assertIn("until the next session", logs.output[0])

    def test_rate_limit_block_clears_only_at_the_next_session(self):
        """A prior day's 429 must not silently linger and block research
        forever - it clears specifically when submit() rolls over to a new
        _session_date (start of the next extended trading day), same as
        the request-count budget.
        """
        import queue as queue_module

        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(
            agent_daily_request_limit=250,
            agent_daily_token_budget=90000,
            market_open_time="04:00",
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
        agent._interval_seconds = lambda: 0
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
            agent.submit({"a": 1})
        # Still the same session - the block must still be in effect.
        self.assertTrue(agent._rate_limit_blocked)
        self.assertTrue(agent._work.empty())

        with unittest.mock.patch(
            "webull_bot.market_agent.datetime"
        ) as mock_datetime:
            mock_datetime.now.return_value = datetime(
                2026, 8, 6, 5, 0, tzinfo=timezone.utc
            )
            agent.submit({"a": 1})
        # Past market open on a new day - the block clears and this submit
        # goes through.
        self.assertFalse(agent._rate_limit_blocked)
        self.assertFalse(agent._work.empty())

    def test_research_makes_exactly_one_call_per_cycle(self):
        """Regression test: a retry-with-different-params here used to
        count a second time against AGENT_DAILY_REQUEST_LIMIT and the
        rolling token budget, spending the day's budget faster than the
        core/extended interval pacing intends - _research must now place
        exactly one Groq call per invocation, no matter what comes back.
        """
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(
            agent_daily_request_limit=250,
            groq_model="groq/compound-mini",
        )
        agent.log = logging.getLogger("test-agent")
        agent._requests_today = 0
        agent._assessments = {}
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

        agent._research({"positions": [{"symbol": "NVDA"}], "candidates": []})

        # Exactly one call - an empty response falls back to conservative
        # defaults for this cycle rather than retrying with different
        # params, and the request budget only ever advances by one.
        self.assertEqual(len(calls), 1)
        self.assertEqual(agent._requests_today, 1)
        self.assertIn("NVDA", agent._assessments)
        self.assertEqual(agent._assessments["NVDA"]["priority"], 0)
        self.assertEqual(agent._assessments["NVDA"]["confidence"], 0)

    def test_research_disables_every_built_in_tool(self):
        """Regression test: raising max_completion_tokens and then telling
        the model to keep JSON compact both failed to reliably stop
        truncated/malformed responses in production - Groq's own tool-
        orchestration overhead before writing the JSON isn't something a
        prompt instruction can bound. TASK B never needed search (it's
        computed purely from STATE's numeric data), so every built-in tool
        must be disabled outright via compound_custom, not just
        discouraged in the prompt.
        """
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(
            agent_daily_request_limit=250,
            groq_model="groq/compound-mini",
        )
        agent.log = logging.getLogger("test-agent")
        agent._requests_today = 0
        agent._assessments = {}
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

        agent._research({"positions": [], "candidates": []})

        self.assertEqual(
            captured["compound_custom"], {"tools": {"enabled_tools": []}}
        )
        self.assertNotIn("search_settings", captured)

    def test_research_omits_compound_custom_for_a_plain_model(self):
        """A plain (non-Compound) model doesn't understand compound_custom -
        it must only be sent when groq_model is actually a Compound system,
        not unconditionally. The default model (see config.py) switched
        away from compound-mini entirely once search was disabled, since
        Compound's tool-orchestration layer was the actual source of the
        truncated/malformed/empty responses, not something worth paying
        for once it has no tools left to use.
        """
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(
            agent_daily_request_limit=250,
            groq_model="llama-3.3-70b-versatile",
        )
        agent.log = logging.getLogger("test-agent")
        agent._requests_today = 0
        agent._assessments = {}
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

        agent._research({"positions": [], "candidates": []})

        self.assertNotIn("compound_custom", captured)

    def test_normalize_no_longer_produces_a_discoveries_key(self):
        """Discovery of new symbols moved out of the model entirely (see
        AutoTrader.refresh_market_pulse) - _normalize's output shape
        shouldn't carry a vestigial discoveries field even if a stale
        prompt/response still mentions one.
        """
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace()
        payload = agent._normalize(
            {"discoveries": [{"symbol": "NVDA"}]},
            set(),
        )
        self.assertNotIn("discoveries", payload)


class AllocationAndLoggingTests(unittest.TestCase):
    def test_default_capital_and_position_allocations(self):
        config = Settings()
        self.assertEqual(
            sum(config.stock_capital_fractions().values()),
            Decimal("1.00"),
        )
        self.assertEqual(
            config.stock_bucket_slot_limits(),
            {"POPULAR": 14, "PENNY": 2, "DISCOVERY": 4},
        )
        self.assertEqual(config.stock_universe_page_size, 200)
        self.assertEqual(config.stocks(), ["ALL"])
        self.assertEqual(config.max_symbols, 500)
        self.assertEqual(config.stock_universe_limit(), 500)
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


class StopExitPricingTests(unittest.TestCase):
    def test_stop_exit_uses_bid_ask_midpoint_not_aggressive_crossing(self):
        api = WebullAPI.__new__(WebullAPI)
        quote = {"bid": "99.00", "ask": "99.20"}
        self.assertEqual(api.stock_stop_exit_price(quote), Decimal("99.10"))

    def test_stop_exit_requires_valid_spread(self):
        api = WebullAPI.__new__(WebullAPI)
        with self.assertRaises(QuoteUnavailableError):
            api.stock_stop_exit_price({"bid": "0", "ask": "99.20"})

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


class MarketCapAllocationTests(StrategyConfigMixin, unittest.TestCase):
    def test_default_market_cap_allocation_is_off_and_configurable(self):
        config = Settings()
        self.assertFalse(config.market_cap_allocation_enabled)
        self.assertEqual(
            config.stock_capital_fractions(),
            {
                "POPULAR": Decimal("0.70"),
                "PENNY": Decimal("0.10"),
                "DISCOVERY": Decimal("0.20"),
            },
        )

        cap_config = Settings(market_cap_allocation_enabled=True)
        self.assertEqual(
            cap_config.stock_capital_fractions(),
            {"LARGE_CAP": Decimal("0.30"), "SMALL_CAP": Decimal("0.70")},
        )
        self.assertEqual(
            sum(cap_config.stock_capital_fractions().values()),
            Decimal("1.00"),
        )
        self.assertEqual(
            cap_config.stock_bucket_slot_limits(),
            {"LARGE_CAP": 6, "SMALL_CAP": 14},
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

    def test_prioritized_stock_batch_by_market_cap_tiers_by_threshold(self):
        strategy = TradingStrategy(self.config())
        symbols = ["BIG1", "BIG2", "SMALL1", "SMALL2", "SMALL3"]
        strategy.activity.update(
            {"BIG1": 50, "BIG2": 40, "SMALL1": 30, "SMALL2": 20, "SMALL3": 10}
        )
        market_values = {
            "BIG1": 2e11,
            "BIG2": 1.5e11,
            "SMALL1": 5e8,
            "SMALL2": 3e8,
            "SMALL3": 1e8,
        }

        batch, _ = strategy.prioritized_stock_batch_by_market_cap(
            symbols,
            0,
            [],
            lambda symbol: None,
            market_values,
            Decimal("100000000000"),
            Decimal("0.80"),
        )

        self.assertIn("BIG1", batch)
        self.assertEqual(strategy.selection_bucket("BIG1"), "LARGE_CAP")
        self.assertEqual(strategy.selection_bucket("SMALL1"), "SMALL_CAP")

    def test_prioritized_stock_batch_by_market_cap_marks_held_positions(self):
        strategy = TradingStrategy(self.config())
        symbols = ["BIG1", "SMALL1", "SMALL2"]
        strategy.activity.update({"BIG1": 10, "SMALL1": 5, "SMALL2": 1})
        positions = [
            {"instrument_type": "EQUITY", "symbol": "SMALL1", "quantity": "10"}
        ]
        market_values = {"BIG1": 2e11, "SMALL1": 5e8, "SMALL2": 3e8}

        batch, _ = strategy.prioritized_stock_batch_by_market_cap(
            symbols,
            0,
            positions,
            lambda symbol: None,
            market_values,
            Decimal("100000000000"),
            Decimal("0.80"),
        )

        self.assertIn("SMALL1", batch)
        self.assertEqual(strategy.selection_bucket("SMALL1"), "HELD")

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

    def test_exclude_micro_scalp_symbols_removes_only_configured_names(self):
        from webull_bot.bot import AutoTrader

        fake_bot = AutoTrader.__new__(AutoTrader)
        fake_bot.config = SimpleNamespace(
            micro_scalp_enabled=True,
            micro_scalp_symbol_list=lambda: ["TSLA", "NVDA"],
        )
        exclude = AutoTrader.exclude_micro_scalp_symbols.__get__(fake_bot)

        remaining, excluded = exclude(["TSLA", "AAPL", "NVDA", "MSFT"])

        self.assertEqual(remaining, ["AAPL", "MSFT"])
        self.assertEqual(excluded, ["TSLA", "NVDA"])

    def test_exclude_micro_scalp_symbols_noop_when_disabled(self):
        from webull_bot.bot import AutoTrader

        fake_bot = AutoTrader.__new__(AutoTrader)
        fake_bot.config = SimpleNamespace(
            micro_scalp_enabled=False,
            micro_scalp_symbol_list=lambda: ["TSLA"],
        )
        exclude = AutoTrader.exclude_micro_scalp_symbols.__get__(fake_bot)

        remaining, excluded = exclude(["TSLA", "AAPL"])

        self.assertEqual(remaining, ["TSLA", "AAPL"])
        self.assertEqual(excluded, [])

    def test_safe_top_gainers_survives_screener_failure(self):
        from webull_bot.bot import AutoTrader

        fake_bot = AutoTrader.__new__(AutoTrader)

        def boom(*args, **kwargs):
            raise RuntimeError("Webull API error 500: boom")

        fake_bot.api = SimpleNamespace(top_gainers=boom)
        safe_call = AutoTrader.safe_top_gainers.__get__(fake_bot)

        self.assertEqual(safe_call(100, 50), {})

    def test_safe_top_active_stocks_falls_back_to_prior_universe_on_failure(self):
        from webull_bot.bot import AutoTrader

        fake_bot = AutoTrader.__new__(AutoTrader)

        def boom(*args, **kwargs):
            raise RuntimeError("Webull API error 503: boom")

        fake_bot.api = SimpleNamespace(top_active_stocks=boom)
        fake_bot.stock_symbols = ["OLD1", "OLD2"]
        fake_bot.stock_market_values = {"OLD1": 1e11, "OLD2": 2e8}
        safe_call = AutoTrader.safe_top_active_stocks.__get__(fake_bot)

        result = safe_call(500, 200)

        self.assertEqual(set(result), {"OLD1", "OLD2"})
        self.assertEqual(result["OLD1"]["market_value"], 1e11)

    def test_safe_top_losers_survives_screener_failure(self):
        from webull_bot.bot import AutoTrader

        fake_bot = AutoTrader.__new__(AutoTrader)

        def boom(*args, **kwargs):
            raise RuntimeError("Webull API error 500: boom")

        fake_bot.api = SimpleNamespace(top_losers=boom)
        safe_call = AutoTrader.safe_top_losers.__get__(fake_bot)

        self.assertEqual(safe_call(5, 5), {})

    def test_safe_market_pulse_active_falls_back_to_empty_not_prior_universe(self):
        """Distinct from safe_top_active_stocks: market_pulse must stay
        small on a screener failure, not balloon to the whole trading
        universe (that fallback is only correct for the once-daily
        universe rebuild).
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
        )
        handle = AutoTrader.handle_broker_conflict.__get__(fake_bot)

        handle("ASHR", RuntimeError("reverse position"))

        self.assertIn("ASHR", fake_bot.broker_conflict_symbols)
        self.assertNotIn("ASHR", fake_bot.pending_stock_exits)
        self.assertNotIn("ASHR", fake_bot.stop_exit_submitted)
        self.assertNotIn("ASHR", fake_bot.stop_loss_escalated)

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
        fake_bot.record_trade = lambda *a, **k: None
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
            api=SimpleNamespace(stock_categories=lambda symbols: {"TSLA": "US_STOCK"}),
        )
        add = AutoTrader.add_to_watchlist.__get__(fake_bot)

        add("tsla")

        self.assertIn("TSLA", fake_bot.user_watchlist)
        self.assertEqual(fake_bot.stock_categories.get("TSLA"), "US_STOCK")
        self.assertIn("TSLA", fake_bot.stock_symbols)


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


class ExecutionGuardrailTests(unittest.TestCase):
    def test_price_sanity_ok_within_tolerance(self):
        from webull_bot.bot import AutoTrader

        check = AutoTrader.price_sanity_ok.__get__(SimpleNamespace())
        self.assertTrue(check(Decimal("100.00"), Decimal("103.00")))

    def test_price_sanity_rejects_large_deviation(self):
        from webull_bot.bot import AutoTrader

        check = AutoTrader.price_sanity_ok.__get__(SimpleNamespace())
        with self.assertLogs("webull-bot", level="ERROR"):
            self.assertFalse(check(Decimal("100.00"), Decimal("110.00")))

    def test_record_order_error_trips_kill_switch_after_threshold(self):
        from webull_bot.bot import AutoTrader, CONSECUTIVE_ORDER_ERROR_LIMIT

        fake_bot = SimpleNamespace(
            order_error_times=deque(), order_kill_switch_tripped=False
        )
        record = AutoTrader.record_order_error.__get__(fake_bot)
        with self.assertLogs("webull-bot", level="CRITICAL"):
            for _ in range(CONSECUTIVE_ORDER_ERROR_LIMIT):
                record("TEST", RuntimeError("boom"))
        self.assertTrue(fake_bot.order_kill_switch_tripped)

    def test_record_order_error_does_not_trip_below_threshold(self):
        from webull_bot.bot import AutoTrader, CONSECUTIVE_ORDER_ERROR_LIMIT

        fake_bot = SimpleNamespace(
            order_error_times=deque(), order_kill_switch_tripped=False
        )
        record = AutoTrader.record_order_error.__get__(fake_bot)
        for _ in range(CONSECUTIVE_ORDER_ERROR_LIMIT - 1):
            record("TEST", RuntimeError("boom"))
        self.assertFalse(fake_bot.order_kill_switch_tripped)

    def test_record_order_error_prunes_entries_outside_window(self):
        from webull_bot.bot import AutoTrader, ORDER_ERROR_WINDOW_SECONDS

        fake_bot = SimpleNamespace(
            order_error_times=deque(), order_kill_switch_tripped=False
        )
        record = AutoTrader.record_order_error.__get__(fake_bot)
        fake_bot.order_error_times.append(
            time.monotonic() - ORDER_ERROR_WINDOW_SECONDS - 5
        )
        record("TEST", RuntimeError("boom"))
        self.assertEqual(len(fake_bot.order_error_times), 1)

    @staticmethod
    def _fake_bot_for_placement(placed, price="10.00"):
        from webull_bot.bot import AutoTrader

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

        fake_bot = SimpleNamespace(api=FakeApi(), iceberg_orders={})
        fake_bot.price_sanity_ok = AutoTrader.price_sanity_ok.__get__(fake_bot)
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

        fake_bot = SimpleNamespace(api=BadPriceApi(), iceberg_orders={})
        fake_bot.price_sanity_ok = AutoTrader.price_sanity_ok.__get__(fake_bot)
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
        )
        fake_bot.price_sanity_ok = AutoTrader.price_sanity_ok.__get__(fake_bot)
        fake_bot.record_order_error = AutoTrader.record_order_error.__get__(fake_bot)
        recorded = []
        fake_bot.record_trade = lambda key, order_id, action: recorded.append(
            (key, order_id, action)
        )
        process = AutoTrader.process_iceberg_orders.__get__(fake_bot)
        process()
        self.assertEqual(len(placed), 1)
        self.assertEqual(placed[0][2], Decimal(ICEBERG_SLICE_SHARES))
        self.assertEqual(
            fake_bot.iceberg_orders["AAA:BUY"]["remaining"],
            Decimal("15") - Decimal(ICEBERG_SLICE_SHARES),
        )
        self.assertEqual(recorded, [("STOCK:AAA", "order-2", "BUY")])

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
        )
        fake_bot.price_sanity_ok = AutoTrader.price_sanity_ok.__get__(fake_bot)
        fake_bot.record_order_error = AutoTrader.record_order_error.__get__(fake_bot)
        process = AutoTrader.process_iceberg_orders.__get__(fake_bot)
        process()
        self.assertNotIn("AAA:BUY", fake_bot.iceberg_orders)


class FractionalExitGuardTests(unittest.TestCase):
    def test_is_fractional_quantity(self):
        from webull_bot.bot import AutoTrader

        self.assertTrue(AutoTrader.is_fractional_quantity(Decimal("2.5847")))
        self.assertFalse(AutoTrader.is_fractional_quantity(Decimal("5")))
        self.assertFalse(AutoTrader.is_fractional_quantity(Decimal("0")))

    def test_boost_stalled_positions_skips_fractional_position_outside_core_hours(self):
        # Regression test: Webull rejects ANY order (buy or sell) on a
        # non-integer quantity outside core hours regardless of the
        # client-side fractional/order-type flags - previously this kept
        # retrying every stall-breaker interval and spamming the same
        # OAUTH_OPENAPI_FRACT_ONLT_CORE_TIME rejection.
        from webull_bot.bot import AutoTrader

        calls = []

        class FakeApi:
            def stock_quote(self, symbol):
                calls.append(symbol)
                return {"symbol": symbol, "bid": "100.00", "ask": "100.05"}

            @staticmethod
            def quote_bid(q):
                return Decimal(str(q["bid"]))

            def place_stock(self, *a, **k):
                raise AssertionError("must not place an order outside core hours")

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                stall_breaker_enabled=True,
                stall_breaker_seconds=1,
                stall_breaker_min_profit=Decimal("0.01"),
                sell_fee_dollars=Decimal("0.02"),
            ),
            api=FakeApi(),
            last_fill_time=0.0,
            last_stall_boost=0.0,
            pending_stock_exits=set(),
            pending_option_exits=set(),
        )
        fake_bot.cooldown_ready = lambda key: True
        fake_bot.is_fractional_quantity = AutoTrader.is_fractional_quantity
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
            def stock_quote(self, symbol):
                calls.append(symbol)
                return {"symbol": symbol, "bid": "0.50", "ask": "0.51"}

            @staticmethod
            def quote_bid(q):
                return Decimal(str(q["bid"]))

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
            ),
            api=FakeApi(),
            strategy=SimpleNamespace(minimum_lot_size=TradingStrategy.minimum_lot_size),
            last_fill_time=0.0,
            last_stall_boost=0.0,
            pending_stock_exits=set(),
            pending_option_exits=set(),
        )
        fake_bot.cooldown_ready = lambda key: True
        fake_bot.is_fractional_quantity = AutoTrader.is_fractional_quantity
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
                "TSLA": "MICRO_SCALP",
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


class ShortPricingTests(unittest.TestCase):
    def test_short_entry_uses_passive_mid_price(self):
        fake_api = SimpleNamespace(
            config=SimpleNamespace(stock_limit_offset=Decimal("0.005")),
            _quote_decimal=WebullAPI._quote_decimal,
        )
        price_fn = WebullAPI.stock_limit_price.__get__(fake_api)
        quote = {"bid": "10.00", "ask": "10.10"}
        self.assertEqual(price_fn(quote, "SHORT"), Decimal("10.05"))

    def test_cover_crosses_above_the_ask(self):
        fake_api = SimpleNamespace(
            config=SimpleNamespace(stock_limit_offset=Decimal("0.01")),
            _quote_decimal=WebullAPI._quote_decimal,
        )
        price_fn = WebullAPI.stock_limit_price.__get__(fake_api)
        quote = {"bid": "10.00", "ask": "10.10", "price": "10.05"}
        # 10.10 * 1.01 = 10.2010, quantized up to the next cent = 10.21.
        self.assertEqual(price_fn(quote, "COVER"), Decimal("10.21"))


if __name__ == "__main__":
    unittest.main()
