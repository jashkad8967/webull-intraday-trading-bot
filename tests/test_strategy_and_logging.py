import logging
import shutil
import unittest
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
                        "reason": "Current volume and catalyst",
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


if __name__ == "__main__":
    unittest.main()
