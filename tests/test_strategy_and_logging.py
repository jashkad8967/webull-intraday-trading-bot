import json
import logging
import shutil
import sys
import threading
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from types import SimpleNamespace

from webull_bot.commands import CommandQueue
from webull_bot.config import Settings
from webull_bot.daily_logging import DatedDailyFileHandler
from webull_bot.market_agent import MarketResearchAgent
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
            vwap_entry_band_percent=Decimal("0.001"),
            stock_min_net_profit_percent=Decimal("0.0001"),
            stock_estimated_round_trip_cost_percent=Decimal("0.002"),
            stock_stop_loss_min_percent=Decimal("0.0015"),
            stock_stop_loss_max_percent=Decimal("0.02"),
            stock_stop_loss_range_multiplier=Decimal("0.35"),
            stock_target_stop_multiple=Decimal("1.2"),
            stock_entry_max_spread_percent=Decimal("0.15"),
            stock_entry_max_extension_percent=Decimal("0.01"),
            stock_core_session_position_fraction=Decimal("0.10"),
            opening_grace_spread_multiplier=Decimal("2"),
            opening_grace_extension_multiplier=Decimal("2"),
            option_take_profit_price=Decimal("0.01"),
            option_stop_loss_percent=Decimal("0.50"),
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

        self.assertEqual(strategy.trend_signal(key, Decimal("9.6")), "HOLD")
        self.assertEqual(strategy.trend_signal(key, Decimal("9.7")), "BUY")
        # A fresh crossover fires instantly, but the very next poll of a
        # still-forming uptrend should not immediately re-fire.
        self.assertEqual(strategy.trend_signal(key, Decimal("9.9")), "HOLD")
        # Once the uptrend has held for the configured confirmation polls,
        # re-entry is allowed again.
        self.assertEqual(strategy.trend_signal(key, Decimal("10.2")), "BUY")

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
            "OPTION:TEST260101C00100000",
            Decimal("0.40"),
            5,
            Decimal("1.00"),
        )
        self.assertEqual(decision.action, "LOSS")

    def test_option_decision_holds_above_stop_and_below_target(self):
        strategy = TradingStrategy(self.config())
        decision = strategy.option_decision(
            "OPTION:TEST260101C00100000",
            Decimal("0.90"),
            5,
            Decimal("1.00"),
        )
        self.assertEqual(decision.action, "HOLD")

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
            trade_times=defaultdict(deque),
            pending_stock_exits=set(),
            stop_exit_submitted={},
            stop_loss_escalated={"ASHR"},
            position_buckets={},
            working_orders={},
            broker_conflict_symbols=set(),
            daily_realized_pnl=Decimal("0"),
            daily_realized_loss=Decimal("0"),
        )
        fake_bot.is_broker_position_conflict = AutoTrader.is_broker_position_conflict
        fake_bot.is_fractional_trading_not_enabled = (
            AutoTrader.is_fractional_trading_not_enabled
        )
        for name in (
            "cooldown_ready",
            "rate_capped",
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
            daily_realized_pnl=Decimal("0"),
            daily_realized_loss=Decimal("0"),
        )
        record = AutoTrader.record_realized_exit.__get__(fake_bot)
        record(Decimal("100"), Decimal("101"), 10)
        self.assertEqual(fake_bot.daily_realized_pnl, Decimal("10"))
        self.assertEqual(fake_bot.daily_realized_loss, Decimal("0"))
        record(Decimal("50"), Decimal("49"), 5)
        self.assertEqual(fake_bot.daily_realized_pnl, Decimal("5"))
        self.assertEqual(fake_bot.daily_realized_loss, Decimal("5"))

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

    def test_discovery_retry_skips_when_nothing_left_to_assess(self):
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(agent_daily_request_limit=250)
        agent._requests_today = 0
        # No client configured; a real call here would raise AttributeError,
        # proving the empty-state, no-discovery retry short-circuits first.
        agent._research({"positions": [], "candidates": []}, include_discovery=False)

    def test_empty_assessments_with_discovery_retries_assessment_only(self):
        """Regression test: an agentic model can spend its whole completion
        budget on the discovery web search and return {} for assessments -
        this must retry once without discovery instead of just falling back
        to conservative defaults on the first empty response.
        """
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(
            agent_daily_request_limit=250,
            agent_discovery_max_symbols=5,
            groq_model="groq/compound-mini",
        )
        agent.log = logging.getLogger("test-agent")
        agent._requests_today = 0
        agent._assessments = {}
        agent._discoveries = []
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
            prompt = kwargs["messages"][1]["content"]
            calls.append(prompt)
            if len(calls) == 1:
                return FakeResponse("{}")
            return FakeResponse(
                json.dumps(
                    {
                        "market_direction": 0.2,
                        "market_volatility": 0.5,
                        "assessments": [
                            {"symbol": "NVDA", "priority": 0.8, "confidence": 0.7}
                        ],
                    }
                )
            )

        agent.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=fake_create)
            )
        )

        agent._research({"positions": [{"symbol": "NVDA"}], "candidates": []})

        self.assertEqual(len(calls), 2)
        self.assertIn("TASK A discoveries", calls[0])
        self.assertNotIn("TASK A discoveries", calls[1])
        self.assertIn("NVDA", agent._assessments)
        self.assertEqual(agent._assessments["NVDA"]["priority"], 0.8)

    def test_discoveries_are_normalized_for_later_broker_validation(self):
        agent = MarketResearchAgent.__new__(MarketResearchAgent)
        agent.config = SimpleNamespace(agent_discovery_max_symbols=2)
        payload = agent._normalize(
            {
                "discoveries": [
                    {
                        "symbol": "nvda",
                        "popularity": 2,
                        "symbol_volatility": 0.8,
                        "confidence": 0.9,
                    },
                    {"symbol": "not a ticker"},
                ]
            },
            set(),
        )
        self.assertEqual([item["symbol"] for item in payload["discoveries"]], ["NVDA"])


class AllocationAndLoggingTests(unittest.TestCase):
    def test_default_capital_and_position_allocations(self):
        config = Settings()
        self.assertEqual(
            sum(config.stock_capital_fractions().values()),
            Decimal("1.00"),
        )
        self.assertEqual(
            config.stock_bucket_slot_limits(),
            {"POPULAR": 3, "PENNY": 1, "DISCOVERY": 1},
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
            {"LARGE_CAP": Decimal("0.80"), "SMALL_CAP": Decimal("0.20")},
        )
        self.assertEqual(
            sum(cap_config.stock_capital_fractions().values()),
            Decimal("1.00"),
        )
        self.assertEqual(
            cap_config.stock_bucket_slot_limits(),
            {"LARGE_CAP": 4, "SMALL_CAP": 1},
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

        def boom(command, positions):
            raise RuntimeError("boom")

        fake_bot = SimpleNamespace(
            commands=SimpleNamespace(
                pop_all=lambda: [{"type": "unknown"}, {"type": "sell"}]
            ),
            _manual_sell=boom,
        )
        process = AutoTrader.process_ui_commands.__get__(fake_bot)
        process([])  # must not raise despite the unknown type and handler error

    def test_manual_sell_closes_equity_position_and_records_pnl(self):
        from webull_bot.bot import AutoTrader

        placed = []
        recorded_pnl = []

        fake_bot = SimpleNamespace(
            pending_stock_exits=set(),
            pending_option_exits=set(),
            api=SimpleNamespace(
                stock_quote=lambda symbol: {"bid": "99.00", "ask": "99.20"},
                stock_limit_price=lambda quote, side: Decimal("99.00"),
                place_stock=lambda symbol, side, quantity, limit_price=None, fractional=False: (
                    placed.append((symbol, side, quantity, fractional)) or "order-1"
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
        manual_sell({"symbol": "TSLA", "instrument_type": "EQUITY"}, positions)

        self.assertEqual(placed, [("TSLA", "SELL", Decimal("3"), False)])
        self.assertIn("TSLA", fake_bot.pending_stock_exits)
        self.assertEqual(recorded_pnl, [(Decimal("100.00"), Decimal("99.00"), Decimal("3"))])

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


if __name__ == "__main__":
    unittest.main()
