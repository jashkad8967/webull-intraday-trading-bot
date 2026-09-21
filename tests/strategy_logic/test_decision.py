import unittest
import unittest.mock
from collections import defaultdict
from decimal import Decimal
from types import SimpleNamespace

from webull_bot.strategy import TradingStrategy
from webull_bot.webull_api import QuoteUnavailableError, WebullAPI

from support.fixtures import StrategyConfigMixin


class ProfitTargetMultiplierTests(unittest.TestCase):
    """profit_target_multiplier - by request: "we basically just want
    to be able to stay in a significant profit until eod" -> "let
    winners run further before taking profit."
    """

    def test_no_widening_when_pnl_is_below_the_threshold(self):
        from webull_bot.bot import AutoTrader

        multiplier = AutoTrader.profit_target_multiplier(
            Decimal("2"), Decimal("200"), Decimal("0.03"), Decimal("1.5")
        )
        self.assertEqual(multiplier, Decimal("1"))

    def test_widens_once_pnl_crosses_the_threshold(self):
        from webull_bot.bot import AutoTrader

        multiplier = AutoTrader.profit_target_multiplier(
            Decimal("7"), Decimal("200"), Decimal("0.03"), Decimal("1.5")
        )
        self.assertEqual(multiplier, Decimal("1.5"))

    def test_widens_exactly_at_the_threshold(self):
        from webull_bot.bot import AutoTrader

        multiplier = AutoTrader.profit_target_multiplier(
            Decimal("6"), Decimal("200"), Decimal("0.03"), Decimal("1.5")
        )
        self.assertEqual(multiplier, Decimal("1.5"))

    def test_no_widening_with_a_loss(self):
        from webull_bot.bot import AutoTrader

        multiplier = AutoTrader.profit_target_multiplier(
            Decimal("-10"), Decimal("200"), Decimal("0.03"), Decimal("1.5")
        )
        self.assertEqual(multiplier, Decimal("1"))

    def test_fails_safe_with_unknown_account_value(self):
        from webull_bot.bot import AutoTrader

        multiplier = AutoTrader.profit_target_multiplier(
            Decimal("100"), None, Decimal("0.03"), Decimal("1.5")
        )
        self.assertEqual(multiplier, Decimal("1"))


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


class ProfitExitGiveUpThresholdTests(unittest.TestCase):
    """should_force_market_exit - by request, after live evidence: WNW's
    PROFIT exit escalated 5 times over 6+ minutes, every attempt at the
    exact same unfillable limit price, with no give-up threshold at
    all (unlike the STOP-loss path, which already had one). trade_
    stocks' PROFIT branches now fall back to the current bid once this
    returns True (see the "Live incident (WNW)" comment in bot.py) -
    trade_stocks itself is too large to harness cheaply, so this locks
    in the counter/threshold semantics the fix depends on: the same
    consecutive_exit_failures counter escalate_stalled_stop_losses
    already increments once per escalation (confirmed via
    PostStopReentryCooldownTests-style direct state, not the full
    wiring).
    """

    @staticmethod
    def _fake_bot(failures, threshold=3):
        return SimpleNamespace(
            consecutive_exit_failures=defaultdict(int, {"WNW": failures}),
            config=SimpleNamespace(
                consecutive_exit_failure_market_threshold=threshold
            ),
            fractional_trading_enabled=True,
        )

    def test_not_tripped_before_the_threshold(self):
        from webull_bot.bot import AutoTrader

        fake_bot = self._fake_bot(2)
        should_force = AutoTrader.should_force_market_exit.__get__(fake_bot)
        self.assertFalse(should_force("WNW", False, True))

    def test_tripped_at_wnw_s_actual_live_failure_count(self):
        from webull_bot.bot import AutoTrader

        fake_bot = self._fake_bot(5)
        should_force = AutoTrader.should_force_market_exit.__get__(fake_bot)
        self.assertTrue(should_force("WNW", False, True))

    def test_never_trips_for_a_fractional_position_outside_core_hours(self):
        from webull_bot.bot import AutoTrader

        fake_bot = self._fake_bot(5)
        should_force = AutoTrader.should_force_market_exit.__get__(fake_bot)
        self.assertFalse(should_force("WNW", True, True))
        self.assertFalse(should_force("WNW", False, False))


class EntryTimePremiumFloorTests(unittest.TestCase):
    """Live incident: option_min_premium_dollars ($0.50) shipped, yet
    SPY260930P00700000 was bought at $0.30 and QQQ260930P00655000 at
    $0.45 the same day. The floor was only enforced in select_atm_
    options at contract-SELECTION time, which isn't authoritative -
    selection has a bypass path (single candidate, or no
    max_contract_cost, skips the quoted-premium check entirely), and
    the discovery-time premium it checks can drift well below the
    floor before the entry actually prices. The floor now also gates
    limit_price in _evaluate_option_entry, which IS the price the
    contract gets bought at.
    """

    @staticmethod
    def _entry_limit(bid, ask):
        from webull_bot.config import Settings
        from webull_bot.webull_api import WebullAPI

        api = WebullAPI.__new__(WebullAPI)
        api.config = Settings(_env_file=None)
        return api.option_limit_price({"bid": bid, "ask": ask}, "BUY")

    def test_the_real_contracts_that_slipped_through_are_now_below_the_floor(self):
        from webull_bot.config import Settings

        floor = Settings(_env_file=None).option_min_premium_dollars
        # SPY P700 filled at 0.30, QQQ P655 filled at 0.45 - both must
        # price below the floor so the entry gate rejects them.
        self.assertLess(self._entry_limit("0.25", "0.35"), floor)
        self.assertLess(self._entry_limit("0.40", "0.50"), floor)

    def test_a_genuinely_priced_contract_still_clears_the_floor(self):
        from webull_bot.config import Settings

        floor = Settings(_env_file=None).option_min_premium_dollars
        self.assertGreaterEqual(self._entry_limit("0.60", "0.70"), floor)


class ProfitTargetTickQuantizationTests(unittest.TestCase):
    """Live incident (NKE, recurring even after the AMD fee-margin
    fix): _evaluate_option_exit's PROFIT branch used to quantize
    decision.target_price to the CENT (.quantize(Decimal("0.01"))),
    not the real $0.05 option tick - a 2% target on a $0.18 cost
    (0.18 * 1.02 = 0.1836) rounds to $0.18 at 2 decimal places, the
    SAME price as cost, so the order filled at cost exactly (0.18 ->
    0.18, three times, each a real -$0.02 loss labeled PROFIT).
    ROUND_UP to the real tick guarantees the placed price is never
    quantized back down below the intended target.
    """

    def test_a_small_profit_target_never_collapses_to_the_entry_price(self):
        from webull_bot.webull_api import WebullAPI
        from decimal import ROUND_UP

        api = WebullAPI.__new__(WebullAPI)
        # NKE's exact numbers: cost 0.18, a 2% target (0.1836) used to
        # collapse right back to 0.18 under cent-rounding.
        target = api._quantize_to_option_tick(Decimal("0.1836"), ROUND_UP)
        self.assertGreater(target, Decimal("0.18"))
        self.assertEqual(target, Decimal("0.20"))
        self.assertEqual(target % Decimal("0.05"), Decimal("0"))
