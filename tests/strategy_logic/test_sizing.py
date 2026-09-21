import unittest
import unittest.mock
from decimal import ROUND_DOWN, Decimal
from types import SimpleNamespace

from webull_bot.config import Settings
from webull_bot.strategy import TradingStrategy
from webull_bot.webull_api import WebullAPI

from support.fixtures import StrategyConfigMixin


class RiskBasedShareCountTests(StrategyConfigMixin, unittest.TestCase):
    """risk_based_share_count - the professional 1-2% position-sizing
    rule: size so hitting the stop costs no more than risk_fraction of
    buying_power, not however many shares a fixed budget affords.
    """

    def test_computes_shares_from_risk_dollars_over_stop_distance(self):
        strategy = TradingStrategy(self.config())
        # risk_dollars = 1000 * 0.03 = 30; stop_distance = 10 - 9.91 = 0.09
        # -> 30 / 0.09 = 333.33 -> floor to 333.
        shares = strategy.risk_based_share_count(
            Decimal("10"), Decimal("9.91"), Decimal("1000"), Decimal("0.03")
        )
        self.assertEqual(shares, 333)

    def test_zero_with_no_stop_distance(self):
        strategy = TradingStrategy(self.config())
        shares = strategy.risk_based_share_count(
            Decimal("10"), Decimal("10"), Decimal("1000"), Decimal("0.03")
        )
        self.assertEqual(shares, 0)

    def test_zero_with_no_buying_power(self):
        strategy = TradingStrategy(self.config())
        shares = strategy.risk_based_share_count(
            Decimal("10"), Decimal("9.91"), Decimal("0"), Decimal("0.03")
        )
        self.assertEqual(shares, 0)

    def test_larger_risk_fraction_allows_more_shares(self):
        strategy = TradingStrategy(self.config())
        smaller = strategy.risk_based_share_count(
            Decimal("10"), Decimal("9.91"), Decimal("1000"), Decimal("0.01")
        )
        larger = strategy.risk_based_share_count(
            Decimal("10"), Decimal("9.91"), Decimal("1000"), Decimal("0.06")
        )
        self.assertGreater(larger, smaller)


class SizeStockEntryRiskCapTests(StrategyConfigMixin, unittest.TestCase):
    """size_stock_entry - regression for a live bug (live evidence:
    "CYCLE | failed | 'int' object has no attribute
    'to_integral_value'" recurring on BNGO/WNW-style whole-share
    fallback entries). risk_based_share_count returns a plain int; when
    it becomes the binding cap, quantity must still come back as a
    Decimal since every downstream caller (is_fractional_quantity,
    record_trade, etc.) expects one.
    """

    def test_returns_a_decimal_even_when_the_risk_cap_is_the_binding_constraint(
        self,
    ):
        from webull_bot.bot import AutoTrader

        # A large affordable budget but a tiny risk_fraction forces the
        # risk cap to bind below stock_order_quantity's own result.
        config = Settings(stock_risk_per_trade_fraction=Decimal("0.001"))
        strategy = TradingStrategy(config)
        fake_bot = SimpleNamespace(
            strategy=strategy,
            config=config,
            fractional_trading_enabled=False,
        )
        size_stock_entry = AutoTrader.size_stock_entry.__get__(fake_bot)
        quantity, _price, _fractional = size_stock_entry(
            Decimal("10"),
            Decimal("1000"),
            Decimal("0"),
            Decimal("1000"),
            True,
            False,
            True,
            symbol="BNGO",
            buying_power=Decimal("1000"),
        )
        self.assertIsInstance(quantity, Decimal)


class StockTotalExposureAtCapTests(unittest.TestCase):
    """stock_total_exposure_at_cap - by request: "do not allow more
    than 20% in stocks." A hard, checked-before-adding portfolio-level
    ceiling on total EQUITY position value as a fraction of account
    value - blocks fresh stock entries/averaging-down only, never any
    exit.
    """

    def test_false_when_well_under_the_cap(self):
        from webull_bot.bot import AutoTrader

        positions = [
            {"instrument_type": "EQUITY", "quantity": "10", "cost_price": "5"},
        ]
        self.assertFalse(
            AutoTrader.stock_total_exposure_at_cap(
                Decimal("1000"), positions, Decimal("0.20")
            )
        )

    def test_true_once_at_the_cap(self):
        from webull_bot.bot import AutoTrader

        positions = [
            {"instrument_type": "EQUITY", "quantity": "20", "cost_price": "10"},
        ]
        # 20 * 10 = $200 == 20% of $1000 - at the cap, not over it,
        # but "do not allow MORE than 20%" means no further room.
        self.assertTrue(
            AutoTrader.stock_total_exposure_at_cap(
                Decimal("1000"), positions, Decimal("0.20")
            )
        )

    def test_true_once_over_the_cap(self):
        from webull_bot.bot import AutoTrader

        positions = [
            {"instrument_type": "EQUITY", "quantity": "30", "cost_price": "10"},
        ]
        self.assertTrue(
            AutoTrader.stock_total_exposure_at_cap(
                Decimal("1000"), positions, Decimal("0.20")
            )
        )

    def test_ignores_option_positions(self):
        from webull_bot.bot import AutoTrader

        positions = [
            {"instrument_type": "OPTION", "quantity": "50", "cost_price": "10"},
        ]
        self.assertFalse(
            AutoTrader.stock_total_exposure_at_cap(
                Decimal("1000"), positions, Decimal("0.20")
            )
        )

    def test_false_when_account_value_is_not_cached_yet(self):
        from webull_bot.bot import AutoTrader

        positions = [
            {"instrument_type": "EQUITY", "quantity": "999", "cost_price": "999"},
        ]
        self.assertFalse(
            AutoTrader.stock_total_exposure_at_cap(None, positions, Decimal("0.20"))
        )


class DiversificationCappedEntryBudgetTests(unittest.TestCase):
    """diversification_capped_entry_budget - by request: "out of 7500
    stocks it should easily be able to find enough stocks to invest
    everything." Live evidence: a single FDX entry consumed ~43% of
    the whole account's buying power in one trade - the fraction cap
    fixes that, but live evidence ALSO showed the fraction alone can
    fall under fractional_shares_min_notional on a small remaining
    balance (buying_power=$45, 15% = $6.75 < $25 min), which would
    have stranded capital instead of deploying it - "100% of buying
    power should be used." The floor fixes that second issue.
    """

    def test_normal_case_uses_the_fraction(self):
        from webull_bot.bot import AutoTrader

        budget = AutoTrader.diversification_capped_entry_budget(
            Decimal("200"), Decimal("0.15"), Decimal("25")
        )
        self.assertEqual(budget, Decimal("30"))

    def test_matches_the_live_fdx_incident_shape(self):
        """A ~$186 account with a 15% cap should never again let one
        entry claim anywhere near $80 the way the live FDX buy did.
        """
        from webull_bot.bot import AutoTrader

        budget = AutoTrader.diversification_capped_entry_budget(
            Decimal("186"), Decimal("0.15"), Decimal("25")
        )
        self.assertLess(budget, Decimal("30"))

    def test_floor_kicks_in_when_the_fraction_falls_under_the_minimum(self):
        """Live evidence: buying_power=$45, 15% = $6.75, well under
        the $25 fractional minimum - the fraction alone would zero out
        every further entry for the rest of the day instead of using
        the remaining capital.
        """
        from webull_bot.bot import AutoTrader

        budget = AutoTrader.diversification_capped_entry_budget(
            Decimal("45"), Decimal("0.15"), Decimal("25")
        )
        self.assertEqual(budget, Decimal("25"))

    def test_floor_never_exceeds_what_is_actually_left(self):
        """A tiny remaining balance (below the floor itself) must
        still be usable up to everything that's left, not just
        zeroed out because it can't reach the full floor either.
        """
        from webull_bot.bot import AutoTrader

        budget = AutoTrader.diversification_capped_entry_budget(
            Decimal("10"), Decimal("0.15"), Decimal("25")
        )
        self.assertEqual(budget, Decimal("10"))

    def test_zero_buying_power_is_safe(self):
        from webull_bot.bot import AutoTrader

        budget = AutoTrader.diversification_capped_entry_budget(
            Decimal("0"), Decimal("0.15"), Decimal("25")
        )
        self.assertEqual(budget, Decimal("0"))


class AveragingDownCapacityTests(StrategyConfigMixin, unittest.TestCase):
    """averaging_down_capacity - by request: bound worst-case per-
    symbol exposure from averaging down. Research finding acted on
    directly: "doubling down three times can turn a 7% position into
    an 18% loss."
    """

    def test_caps_below_the_configured_max_on_a_small_account(self):
        strategy = TradingStrategy(self.config())
        # buying_power=$200, max_symbol_risk_fraction=0.12 ->
        # max_symbol_risk_dollars = $24. per_buy_risk_dollars=$10 ->
        # total_buys_affordable = 2 -> capacity = 2 - 1 = 1, well below
        # the configured ceiling of 5.
        capacity = strategy.averaging_down_capacity(
            Decimal("10"), Decimal("200"), Decimal("0.12"), 5
        )
        self.assertEqual(capacity, 1)

    def test_never_exceeds_the_configured_ceiling_on_a_large_account(self):
        strategy = TradingStrategy(self.config())
        # Plenty of room - the configured ceiling (5) binds instead of
        # the risk fraction.
        capacity = strategy.averaging_down_capacity(
            Decimal("1"), Decimal("1000000"), Decimal("0.12"), 5
        )
        self.assertEqual(capacity, 5)

    def test_zero_when_even_one_averaging_buy_would_breach_the_fraction(self):
        strategy = TradingStrategy(self.config())
        capacity = strategy.averaging_down_capacity(
            Decimal("100"), Decimal("200"), Decimal("0.12"), 5
        )
        self.assertEqual(capacity, 0)

    def test_falls_back_to_the_configured_max_with_no_risk_data(self):
        strategy = TradingStrategy(self.config())
        self.assertEqual(
            strategy.averaging_down_capacity(
                Decimal("0"), Decimal("200"), Decimal("0.12"), 5
            ),
            5,
        )
        self.assertEqual(
            strategy.averaging_down_capacity(
                Decimal("10"), Decimal("0"), Decimal("0.12"), 5
            ),
            5,
        )


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

    def test_risk_based_sizing_caps_quantity_below_the_affordability_limit(self):
        """By request: risk-based position sizing (the professional
        1-2% rule) - hitting the stop should cost no more than
        stock_risk_per_trade_fraction of buying_power, even when
        affordability alone would allow a much larger order.
        """
        config = Settings(
            stock_core_session_position_fraction=Decimal("0"),
            stock_whole_share_core_session_fraction=Decimal("1"),
            stock_quantity=100000,
            max_order_notional=Decimal("1000000"),
            stock_risk_per_trade_fraction=Decimal("0.03"),
        )
        size = self._size_fn(config)
        # price=$10, default stock_stop_loss_min_percent=0.009 -> stop
        # distance = $0.09/share. buying_power=$1000 (distinct from the
        # huge entry_budget/whole_share_remaining below, since the risk
        # cap sizes against total buying power, not the bucket budget) ->
        # risk_dollars = 1000*0.03 = $30 -> risk cap = floor(30/0.09) = 333.
        # Affordability alone (entry_budget=$1,000,000) would allow
        # ~97,000+ shares - the risk cap must be what actually binds.
        quantity, buffered_price, fractional = size(
            Decimal("10"),
            Decimal("1000000"),
            Decimal("0"),
            Decimal("1000000"),
            True,
            symbol="RISKY",
            buying_power=Decimal("1000"),
        )
        self.assertFalse(fractional)
        self.assertEqual(quantity, 333)

    def test_risk_based_sizing_is_a_noop_without_a_symbol_or_buying_power(self):
        """Backward-compatible: a caller that doesn't pass symbol/
        buying_power (or passes buying_power=None) gets the old,
        affordability-only sizing untouched.
        """
        config = Settings(
            stock_core_session_position_fraction=Decimal("0"),
            stock_whole_share_core_session_fraction=Decimal("1"),
            stock_quantity=1000,
            max_order_notional=Decimal("100000"),
        )
        size = self._size_fn(config)
        quantity, buffered_price, fractional = size(
            Decimal("50"), Decimal("10000"), Decimal("0"), Decimal("10000"), True
        )
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
