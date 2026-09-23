import time
import unittest
import unittest.mock
from collections import defaultdict, deque
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from webull_bot.webull_api import WebullAPI
from fake_open_times import FakeOpenTimes


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


class HasPendingSellOrderTests(unittest.TestCase):
    """SELL analog of HasPendingBuyOrderTests. Live incident: VZ hit
    OPENAPI_ORDER_NOT_SUPPORT_REVERSE_OPTION and OPENAPI_POSITION_
    ORDER_INTENT_MISMATCH when boost_stalled_positions placed its own
    SELL_TO_CLOSE while a STOP/PROFIT exit order for the same symbol
    was already resting - has_pending_sell_order checks working_orders
    directly so a second sweep can't double-book a close.
    """

    @staticmethod
    def _fake_bot(working_orders):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(working_orders=working_orders)
        return AutoTrader.has_pending_sell_order.__get__(fake_bot)

    def test_true_while_an_uncancelled_stop_order_is_resting(self):
        has_pending = self._fake_bot(
            {
                "order-1": {
                    "key": "OPTION:VZ260925P00050000",
                    "action": "STOP",
                    "cancel_requested_at": None,
                }
            }
        )
        self.assertTrue(has_pending("OPTION:VZ260925P00050000"))

    def test_true_while_an_uncancelled_profit_order_is_resting(self):
        has_pending = self._fake_bot(
            {
                "order-1": {
                    "key": "OPTION:VZ260925P00050000",
                    "action": "PROFIT",
                    "cancel_requested_at": None,
                }
            }
        )
        self.assertTrue(has_pending("OPTION:VZ260925P00050000"))

    def test_false_once_a_cancel_has_been_requested(self):
        has_pending = self._fake_bot(
            {
                "order-1": {
                    "key": "OPTION:VZ260925P00050000",
                    "action": "STOP",
                    "cancel_requested_at": time.monotonic(),
                }
            }
        )
        self.assertFalse(has_pending("OPTION:VZ260925P00050000"))

    def test_false_for_a_different_symbol(self):
        has_pending = self._fake_bot(
            {
                "order-1": {
                    "key": "OPTION:OTHER",
                    "action": "STOP",
                    "cancel_requested_at": None,
                }
            }
        )
        self.assertFalse(has_pending("OPTION:VZ260925P00050000"))

    def test_false_for_a_buy_order_on_the_same_symbol(self):
        has_pending = self._fake_bot(
            {
                "order-1": {
                    "key": "OPTION:VZ260925P00050000",
                    "action": "BUY",
                    "cancel_requested_at": None,
                }
            }
        )
        self.assertFalse(has_pending("OPTION:VZ260925P00050000"))

    def test_false_with_no_working_orders_at_all(self):
        has_pending = self._fake_bot({})
        self.assertFalse(has_pending("OPTION:VZ260925P00050000"))


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

            def order_filled_price(self, detail):
                # These fixtures exercise fill/cancel status only -
                # None means "no fill price known", so the
                # correction path fails open and leaves pnl alone.
                return None

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

            def order_filled_price(self, detail):
                # These fixtures exercise fill/cancel status only -
                # None means "no fill price known", so the
                # correction path fails open and leaves pnl alone.
                return None

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

            def order_filled_price(self, detail):
                # These fixtures exercise fill/cancel status only -
                # None means "no fill price known", so the
                # correction path fails open and leaves pnl alone.
                return None

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
        fake_bot.is_order_reverses_existing_position = (
            AutoTrader.is_order_reverses_existing_position
        )
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

            def order_filled_price(self, detail):
                # These fixtures exercise fill/cancel status only -
                # None means "no fill price known", so the
                # correction path fails open and leaves pnl alone.
                return None

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


class WorkingOrdersConcurrencyTests(unittest.TestCase):
    """By request: "held positions should be checked every 0.25s
    separately, the rest of the scan can take its own time" -
    position-protection (monitor_working_orders/the repricers/
    escalate_stalled_stop_losses) now runs on its own background
    thread (AutoTrader._position_protection_loop), concurrently with
    the main thread's record_trade calls for fresh entries. Both sides
    touch self.working_orders - this is a real regression test (two
    actual OS threads hammering the real record_trade/_rekey_working_
    order module-level functions against a real threading.Lock), not
    just a single-threaded unit test, since the whole point of this
    change is concurrent-safety that a single-threaded test can't
    exercise.
    """

    def test_concurrent_record_trade_and_rekey_never_corrupt_or_crash(self):
        import threading as _threading

        from webull_bot.bot import AutoTrader, _rekey_working_order

        fake_bot = SimpleNamespace(
            working_orders={},
            working_orders_lock=_threading.Lock(),
            last_trade={},
            submitted_order_ids_today=set(),
            last_exit_at={},
            position_opened_at={},
            position_open_times=FakeOpenTimes(),
            symbol_pnl_history=defaultdict(deque),
            recent_stop_losses=deque(),
            last_volatility_stop_loss_at={},
            last_capital_deployed_at=0.0,
            trade_times=defaultdict(deque),
            consecutive_exit_failures=defaultdict(int),
            status=SimpleNamespace(record_trade=lambda *a, **k: None),
        )
        record_trade = AutoTrader.record_trade.__get__(fake_bot)
        errors: list[Exception] = []

        def writer_thread(n):
            try:
                for i in range(200):
                    order_id = f"writer{n}-{i}"
                    record_trade(f"STOCK:SYM{n}", order_id, "BUY", quantity=1)
            except Exception as exc:  # pragma: no cover - test failure path
                errors.append(exc)

        def rekey_thread(n):
            try:
                for i in range(200):
                    old_id = f"writer{n}-{i}"
                    new_id = f"rekey{n}-{i}"
                    _rekey_working_order(
                        fake_bot, old_id, new_id, {"key": f"STOCK:SYM{n}"}
                    )
            except Exception as exc:  # pragma: no cover - test failure path
                errors.append(exc)

        threads = []
        for n in range(4):
            threads.append(_threading.Thread(target=writer_thread, args=(n,)))
            threads.append(_threading.Thread(target=rekey_thread, args=(n,)))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [])
        # No thread hung (all joined within the timeout).
        self.assertTrue(all(not t.is_alive() for t in threads))


class ActualFillPriceCorrectionTests(unittest.TestCase):
    """Live incident (FIGR, by explicit request: "figr was sold at
    35.71 not 35.81 leading to a loss not a profit, so your number
    calculations are wrong, make sure you are getting the correct
    numbers from the openapi endpoints").

    record_realized_exit prices an exit at SUBMISSION time from the
    submitted limit, because that's all that's known then. That's
    exact for a limit fill - but Webull routes FRACTIONAL stock
    orders as MARKET orders, so FIGR (1.2067 shares, submitted 35.81)
    actually filled at 35.71 and a recorded +$0.05 PROFIT was really
    a loss. The fill-confirmation path already fetches order_detail,
    so the real executed price is read back from it and the estimate
    corrected.
    """

    def test_reads_the_real_fill_price_from_webulls_nested_shape(self):
        from webull_bot.webull_api import WebullAPI

        detail = {
            "client_order_id": "d26f4040",
            "orders": [
                {
                    "status": "FILLED",
                    "filled_quantity": "1.2067",
                    "avg_filled_price": "35.71",
                }
            ],
        }
        self.assertEqual(
            WebullAPI.order_filled_price(detail), Decimal("35.71")
        )

    def test_returns_none_on_an_unrecognized_shape_so_callers_fail_open(self):
        from webull_bot.webull_api import WebullAPI

        self.assertIsNone(WebullAPI.order_filled_price({}))
        self.assertIsNone(
            WebullAPI.order_filled_price({"orders": [{"status": "FILLED"}]})
        )
        # Unparseable / non-positive values must not be trusted either.
        self.assertIsNone(
            WebullAPI.order_filled_price(
                {"orders": [{"avg_filled_price": "not-a-number"}]}
            )
        )
        self.assertIsNone(
            WebullAPI.order_filled_price({"orders": [{"avg_filled_price": "0"}]})
        )

    def test_figr_profit_is_corrected_into_the_real_loss(self):
        from webull_bot.bot import AutoTrader

        amended = {}
        fake_bot = SimpleNamespace(
            daily_realized_pnl=Decimal("0.052402"),
            daily_realized_loss=Decimal("0"),
            daily_pnl=SimpleNamespace(record=lambda pnl, loss: None),
            status=SimpleNamespace(
                amend_trade_pnl=lambda oid, pnl, price=None: amended.update(
                    {oid: (pnl, price)}
                )
            ),
        )
        correct = AutoTrader.correct_realized_exit.__get__(fake_bot)

        # (35.71 actual - 35.81 submitted) * 1.2067 shares
        delta = (Decimal("35.71") - Decimal("35.81")) * Decimal("1.2067")
        correct("order-figr", Decimal("0.052402"), delta)

        corrected, shown_price = amended["order-figr"]
        self.assertLess(corrected, 0)
        self.assertAlmostEqual(float(corrected), -0.068268, places=6)
        # Live 2026-09-23: a BABA stop submitted at 1.10 filled at
        # 1.19. The pnl was corrected by $18 but the trade log kept
        # showing 1.10, so the dashboard reported an exit price the
        # account never traded at. The corrected pnl and the displayed
        # price must describe the SAME fill.
        self.assertIsNone(
            shown_price,
            "no fill price passed here - the displayed price is left alone",
        )
        # The running totals must follow the correction, including the
        # losing-side tracker the daily breaker reads.
        self.assertAlmostEqual(
            float(fake_bot.daily_realized_pnl), -0.068268, places=6
        )
        self.assertAlmostEqual(
            float(fake_bot.daily_realized_loss), 0.068268, places=6
        )

    def test_an_exact_limit_fill_needs_no_correction(self):
        from webull_bot.bot import AutoTrader

        touched = []
        fake_bot = SimpleNamespace(
            daily_realized_pnl=Decimal("5"),
            daily_realized_loss=Decimal("0"),
            daily_pnl=SimpleNamespace(record=lambda pnl, loss: None),
            status=SimpleNamespace(
                amend_trade_pnl=lambda oid, pnl, price=None: touched.append(oid)
            ),
        )
        correct = AutoTrader.correct_realized_exit.__get__(fake_bot)
        correct("order-1", Decimal("5"), Decimal("0"))
        self.assertEqual(touched, [])
        self.assertEqual(fake_bot.daily_realized_pnl, Decimal("5"))
