import logging
import shutil
import sys
import unittest
import unittest.mock
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from webull_bot.config import Settings
from webull_bot.daily_logging import DatedDailyFileHandler
from webull_bot.market_agent import MarketResearchAgent
from webull_bot.strategy import TradingStrategy
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

            def place_stock(self, symbol, side, quantity, limit_price=None):
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


if __name__ == "__main__":
    unittest.main()
