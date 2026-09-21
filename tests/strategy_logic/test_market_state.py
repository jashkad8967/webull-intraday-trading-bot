import unittest
import unittest.mock
from decimal import Decimal
from types import SimpleNamespace

from webull_bot.strategy import TradingStrategy
from webull_bot.webull_api import WebullAPI

from support.fixtures import StrategyConfigMixin


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
