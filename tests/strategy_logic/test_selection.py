import unittest
import unittest.mock
from decimal import Decimal

from webull_bot.config import Settings
from webull_bot.strategy import TradingStrategy

from support.fixtures import StrategyConfigMixin


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


class StockScanConcurrentBatchesTests(unittest.TestCase):
    """stock_scan_concurrent_batches - by request: "scan through all
    [the universe]... split it up in parallel streams... as many as
    needed to scan everything and filter it down, then dynamically
    less as it is filtered down... does not need to be as intense in
    extended hours."
    """

    @staticmethod
    def _strategy(**overrides):
        config = Settings(
            stock_batch_size=100,
            stock_scan_target_full_coverage_cycles=20,
            stock_scan_max_concurrent_batches=8,
            stock_scan_extended_hours_concurrency_fraction=Decimal("0.5"),
            **overrides,
        )
        return TradingStrategy(config)

    def test_small_universe_needs_only_one_batch(self):
        strategy = self._strategy()
        self.assertEqual(
            strategy.stock_scan_concurrent_batches(300, core_session_active=True), 1
        )

    def test_scales_up_for_a_large_universe(self):
        # 5000 symbols / (100 * 20) = 2.5 -> ceil to 3.
        strategy = self._strategy()
        self.assertEqual(
            strategy.stock_scan_concurrent_batches(5000, core_session_active=True), 3
        )

    def test_capped_by_the_safety_max_even_for_a_huge_universe(self):
        strategy = self._strategy()
        self.assertEqual(
            strategy.stock_scan_concurrent_batches(100000, core_session_active=True),
            8,
        )

    def test_reduced_outside_core_hours(self):
        strategy = self._strategy()
        # 11000 / (100 * 20) = 5.5 -> ceil to 6, well under the max=8
        # safety cap, so the extended-hours halving is actually visible
        # instead of both sides just saturating at the cap.
        core = strategy.stock_scan_concurrent_batches(11000, core_session_active=True)
        extended = strategy.stock_scan_concurrent_batches(
            11000, core_session_active=False
        )
        self.assertEqual(core, 6)
        self.assertEqual(extended, 3)

    def test_never_goes_below_one_even_outside_core_hours(self):
        strategy = self._strategy()
        self.assertGreaterEqual(
            strategy.stock_scan_concurrent_batches(50, core_session_active=False), 1
        )

    def test_zero_universe_is_safe(self):
        strategy = self._strategy()
        self.assertEqual(
            strategy.stock_scan_concurrent_batches(0, core_session_active=True), 1
        )
