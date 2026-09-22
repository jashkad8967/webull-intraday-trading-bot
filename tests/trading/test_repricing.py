import unittest
import unittest.mock
from collections import defaultdict, deque
from decimal import Decimal
from types import SimpleNamespace



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

    def test_a_buy_that_omits_limit_price_leaves_working_orders_unrepriceable(self):
        """Live incident ("nah there should be no constraint like
        that" investigation): EVERY BUY/SHORT record_trade call site
        across the whole bot (stocks, options, pairs, volatility-
        scalp, averaging-down) used to pass only entry_price=..., never
        the positional limit_price - so working_orders[order_id]
        ["limit_price"] was always None for a fresh entry. Both the
        stock-side reprice_resting_entries and the option-side
        reprice_resting_option_entries read exactly this field as
        their "current resting limit" baseline - with it always None,
        neither repricer could ever detect "the ask moved, chase it,"
        so a resting BUY just sat at its original price until the
        hard order-timeout cancelled it, unfilled, with zero visible
        error. This documents the bug this fixed: entry_price alone
        is NOT enough, limit_price must also be passed.
        """
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            last_trade={},
            manual_touch_at={},
            submitted_order_ids_today=set(),
            last_exit_at={},
            position_opened_at={},
            symbol_pnl_history=defaultdict(deque),
            consecutive_exit_failures=defaultdict(int),
            recent_stop_losses=deque(),
            last_volatility_stop_loss_at={},
            last_capital_deployed_at=0.0,
            trade_times=defaultdict(deque),
            working_orders={},
            status=SimpleNamespace(record_trade=lambda *a, **k: None),
            option_entry_occurred_today=False,
        )
        record_trade = AutoTrader.record_trade.__get__(fake_bot)

        # The buggy call shape (entry_price only) - limit_price stays
        # None, exactly the bug that made both entry repricers no-ops.
        record_trade(
            "STOCK:BUGGY", "order-buggy", "BUY", entry_price=Decimal("10.00")
        )
        self.assertIsNone(fake_bot.working_orders["order-buggy"]["limit_price"])

        # The fixed call shape (every real call site now uses this) -
        # limit_price passed positionally, matching entry_price.
        record_trade(
            "STOCK:FIXED",
            "order-fixed",
            "BUY",
            Decimal("10.00"),
            entry_price=Decimal("10.00"),
        )
        self.assertEqual(
            fake_bot.working_orders["order-fixed"]["limit_price"], Decimal("10.00")
        )

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
        fake_bot.is_order_reverses_existing_position = (
            AutoTrader.is_order_reverses_existing_position
        )
        escalate = AutoTrader.escalate_stalled_stop_losses.__get__(fake_bot)

        with unittest.mock.patch("time.monotonic", return_value=20.0):
            escalate()

        self.assertEqual(cancelled, ["order-1"])
        self.assertIn("ASHR", fake_bot.stop_loss_escalated)
        self.assertNotIn("ASHR", fake_bot.pending_stock_exits)
        self.assertNotIn("ASHR", fake_bot.stop_exit_submitted)
        self.assertEqual(fake_bot.consecutive_exit_failures["ASHR"], 1)


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


class RepriceRestingOptionExitsTests(unittest.TestCase):
    """Options analog of RepriceRestingExitsTests - by request: "use
    repricing to capture the trade when buying and selling options
    also." A resting option PROFIT sell should chase the current ask
    the same way a resting stock PROFIT sell does.
    """

    def test_reprice_cancels_and_replaces_option_exit_at_new_ask(self):
        from webull_bot.bot import AutoTrader

        cancelled = []
        placed = []
        contract = {
            "symbol": "XYZ260101C00100000",
            "underlying_symbol": "XYZ",
            "strike_price": "100",
            "expiration_date": "2026-01-01",
            "option_type": "CALL",
        }
        quote = {
            "symbol": "XYZ260101C00100000",
            "bid": "1.90",
            "ask": "2.00",
            "price": "1.95",
        }

        class FakeApi:
            @staticmethod
            def option_quotes(symbols):
                return [quote]

            @staticmethod
            def quote_ask(q):
                return Decimal(str(q["ask"]))

            @staticmethod
            def quote_bid(q):
                return Decimal(str(q["bid"]))

            @staticmethod
            def option_tick_from_quote(*prices):
                from webull_bot.webull_api import WebullAPI

                return WebullAPI.option_tick_from_quote(*prices)

            @staticmethod
            def _quantize_to_option_tick(price, rounding, tick=None):
                from webull_bot.webull_api import WebullAPI

                return WebullAPI._quantize_to_option_tick(price, rounding, tick)

            @staticmethod
            def quote_price(q):
                return Decimal(str(q["price"]))

            @staticmethod
            def option_position(contract, positions):
                for item in positions:
                    if item.get("symbol") == contract["symbol"]:
                        return (
                            Decimal(str(item.get("quantity", "0"))),
                            Decimal(str(item.get("cost_price", "0"))),
                        )
                return Decimal("0"), Decimal("0")

            @staticmethod
            def cancel(order_id):
                cancelled.append(order_id)

            @staticmethod
            def place_option(contract, side, quantity, limit_price, position_intent):
                placed.append((contract["symbol"], side, quantity, limit_price))
                return "order-2"

        rekeyed = []
        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                poll_seconds=Decimal("0.25"),
                price_sanity_cooldown_seconds=60,
                option_take_profit_percent=Decimal("0.02"),
                # A PROFIT reprice must clear cost PLUS the flat sell
                # fee, not just cost - see the guard in
                # reprice_resting_option_exits.
                sell_fee_dollars=Decimal("0.02"),
                option_sell_fee_per_contract=Decimal("0.02"),
            ),
            api=FakeApi(),
            status=SimpleNamespace(
                rekey_trade=lambda old, new: rekeyed.append((old, new))
            ),
            last_option_reprice=0.0,
            option_contracts=[contract],
            manual_touch_at={},
            price_sanity_rejected_at={},
            is_order_not_cancelable=lambda exc: False,
            working_orders={
                "order-1": {
                    "submitted_at": 0.0,
                    "key": "OPTION:XYZ260101C00100000",
                    "action": "PROFIT",
                    "cancel_requested_at": None,
                    "limit_price": Decimal("1.80"),
                }
            },
        )
        fake_bot.price_sanity_ok = AutoTrader.price_sanity_ok.__get__(fake_bot)
        fake_bot.price_sanity_cooldown_ready = AutoTrader.price_sanity_cooldown_ready.__get__(fake_bot)
        reprice = AutoTrader.reprice_resting_option_exits.__get__(fake_bot)

        positions = [
            {
                "instrument_type": "OPTION",
                "symbol": "XYZ260101C00100000",
                "quantity": "1",
                "cost_price": "1.50",
            }
        ]
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice(positions)

        # By explicit request ("the bot keeps requesting 50 even though
        # 49 reprice midpoint would bring significant profit as well"),
        # with the "only if the midpoint is a good profit though"
        # guard: bid 1.90 / ask 2.00 -> midpoint 1.95, which clears
        # cost (1.50) plus the 2% take-profit bar, so it re-quotes to
        # the fillable 1.95 rather than hanging at the 2.00 ask.
        self.assertEqual(cancelled, ["order-1"])
        self.assertEqual(len(placed), 1)
        self.assertEqual(
            placed[0], ("XYZ260101C00100000", "SELL", 1, Decimal("1.95"))
        )
        self.assertNotIn("order-1", fake_bot.working_orders)
        self.assertIn("order-2", fake_bot.working_orders)
        self.assertEqual(
            fake_bot.working_orders["order-2"]["limit_price"], Decimal("1.95")
        )
        self.assertEqual(rekeyed, [("order-1", "order-2")])

    def test_holds_at_the_ask_when_the_midpoint_is_not_a_good_profit(self):
        """By explicit request ("only if the midpoint is a good profit
        though"): conceding half the spread is only worth it when what
        remains still clears this position's own take-profit bar. Here
        cost 1.90 against a 1.90/2.00 quote leaves a 1.95 midpoint -
        only ~2.6% over cost, under the 15% bar - so it holds out at
        the 2.00 ask instead of giving up the spread for scraps.
        """
        from webull_bot.bot import AutoTrader

        cancelled = []
        placed = []
        contract = {
            "symbol": "XYZ260101C00100000",
            "underlying_symbol": "XYZ",
            "strike_price": "100",
            "expiration_date": "2026-01-01",
            "option_type": "CALL",
        }
        quote = {
            "symbol": "XYZ260101C00100000",
            "bid": "1.90",
            "ask": "2.00",
            "price": "1.95",
        }

        class FakeApi:
            @staticmethod
            def option_quotes(symbols):
                return [quote]

            @staticmethod
            def quote_ask(q):
                return Decimal(str(q["ask"]))

            @staticmethod
            def quote_bid(q):
                return Decimal(str(q["bid"]))

            @staticmethod
            def option_tick_from_quote(*prices):
                from webull_bot.webull_api import WebullAPI

                return WebullAPI.option_tick_from_quote(*prices)

            @staticmethod
            def _quantize_to_option_tick(price, rounding, tick=None):
                from webull_bot.webull_api import WebullAPI

                return WebullAPI._quantize_to_option_tick(price, rounding, tick)

            @staticmethod
            def quote_price(q):
                return Decimal(str(q["price"]))

            @staticmethod
            def option_position(contract, positions):
                return Decimal("1"), Decimal("1.90")

            @staticmethod
            def cancel(order_id):
                cancelled.append(order_id)

            @staticmethod
            def place_option(contract, side, quantity, limit_price, position_intent):
                placed.append((contract["symbol"], side, quantity, limit_price))
                return "order-2"

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                poll_seconds=Decimal("0.25"),
                price_sanity_cooldown_seconds=60,
                option_take_profit_percent=Decimal("0.15"),
                sell_fee_dollars=Decimal("0.02"),
                option_sell_fee_per_contract=Decimal("0.02"),
            ),
            api=FakeApi(),
            status=SimpleNamespace(rekey_trade=lambda old, new: None),
            last_option_reprice=0.0,
            option_contracts=[contract],
            manual_touch_at={},
            price_sanity_rejected_at={},
            is_order_not_cancelable=lambda exc: False,
            working_orders={
                "order-1": {
                    "submitted_at": 0.0,
                    "key": "OPTION:XYZ260101C00100000",
                    "action": "PROFIT",
                    "cancel_requested_at": None,
                    "limit_price": Decimal("1.80"),
                }
            },
        )
        fake_bot.price_sanity_ok = AutoTrader.price_sanity_ok.__get__(fake_bot)
        fake_bot.price_sanity_cooldown_ready = AutoTrader.price_sanity_cooldown_ready.__get__(fake_bot)
        reprice = AutoTrader.reprice_resting_option_exits.__get__(fake_bot)

        positions = [
            {
                "instrument_type": "OPTION",
                "symbol": "XYZ260101C00100000",
                "quantity": "1",
                "cost_price": "1.90",
            }
        ]
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice(positions)

        self.assertEqual(placed[0][3], Decimal("2.00"))

    def test_never_chases_the_ask_below_entry_cost(self):
        from webull_bot.bot import AutoTrader

        cancelled = []
        placed = []
        contract = {
            "symbol": "XYZ260101C00100000",
            "underlying_symbol": "XYZ",
            "strike_price": "100",
            "expiration_date": "2026-01-01",
            "option_type": "CALL",
        }
        quote = {
            "symbol": "XYZ260101C00100000",
            "bid": "1.20",
            "ask": "1.30",
            "price": "1.25",
        }

        class FakeApi:
            @staticmethod
            def option_quotes(symbols):
                return [quote]

            @staticmethod
            def quote_ask(q):
                return Decimal(str(q["ask"]))

            @staticmethod
            def quote_price(q):
                return Decimal(str(q["price"]))

            @staticmethod
            def option_position(contract, positions):
                return Decimal("1"), Decimal("1.50")

            @staticmethod
            def cancel(order_id):
                cancelled.append(order_id)

            @staticmethod
            def place_option(contract, side, quantity, limit_price, position_intent):
                placed.append((contract["symbol"], side, quantity, limit_price))
                return "order-2"

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                poll_seconds=Decimal("0.25"), price_sanity_cooldown_seconds=60
            ),
            api=FakeApi(),
            status=SimpleNamespace(rekey_trade=lambda old, new: None),
            last_option_reprice=0.0,
            option_contracts=[contract],
            manual_touch_at={},
            price_sanity_rejected_at={},
            is_order_not_cancelable=lambda exc: False,
            working_orders={
                "order-1": {
                    "submitted_at": 0.0,
                    "key": "OPTION:XYZ260101C00100000",
                    "action": "PROFIT",
                    "cancel_requested_at": None,
                    "limit_price": Decimal("1.60"),
                }
            },
        )
        fake_bot.price_sanity_ok = AutoTrader.price_sanity_ok.__get__(fake_bot)
        fake_bot.price_sanity_cooldown_ready = AutoTrader.price_sanity_cooldown_ready.__get__(fake_bot)
        reprice = AutoTrader.reprice_resting_option_exits.__get__(fake_bot)

        positions = [
            {
                "instrument_type": "OPTION",
                "symbol": "XYZ260101C00100000",
                "quantity": "1",
                "cost_price": "1.50",
            }
        ]
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice(positions)

        self.assertEqual(cancelled, [])
        self.assertEqual(placed, [])

    def test_reprice_also_actively_chases_a_resting_stop_order(self):
        """By explicit request ("profit is only realized when the
        order goes through, not just getting cancelled... same with
        exit"): a resting STOP now gets actively re-quoted too, not
        just PROFIT - tracking option_limit_price's aggressive bid-
        crossing formula (never the ask, unlike PROFIT) so it stays a
        loss-capping exit that keeps up with a moving market instead
        of sitting stale for up to 120s between attempts.
        """
        from webull_bot.bot import AutoTrader

        cancelled = []
        placed = []
        contract = {
            "symbol": "XYZ260101C00100000",
            "underlying_symbol": "XYZ",
            "strike_price": "100",
            "expiration_date": "2026-01-01",
            "option_type": "CALL",
        }
        quote = {
            "symbol": "XYZ260101C00100000",
            "bid": "1.00",
            "ask": "1.10",
            "price": "1.00",
        }

        class FakeApi:
            @staticmethod
            def option_quotes(symbols):
                return [quote]

            @staticmethod
            def option_limit_price(q, side):
                # Mirrors the real aggressive-crossing formula: 3%
                # below bid, quantized DOWN to the nearest nickel.
                return Decimal("0.95")

            @staticmethod
            def quote_price(q):
                return Decimal(str(q["price"]))

            @staticmethod
            def option_position(contract, positions):
                for item in positions:
                    if item.get("symbol") == contract["symbol"]:
                        return (
                            Decimal(str(item.get("quantity", "0"))),
                            Decimal(str(item.get("cost_price", "0"))),
                        )
                return Decimal("0"), Decimal("0")

            @staticmethod
            def cancel(order_id):
                cancelled.append(order_id)

            @staticmethod
            def place_option(contract, side, quantity, limit_price, position_intent):
                placed.append((contract["symbol"], side, quantity, limit_price))
                return "order-2"

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                poll_seconds=Decimal("0.25"), price_sanity_cooldown_seconds=60
            ),
            api=FakeApi(),
            status=SimpleNamespace(rekey_trade=lambda old, new: None),
            last_option_reprice=0.0,
            option_contracts=[contract],
            manual_touch_at={},
            price_sanity_rejected_at={},
            is_order_not_cancelable=lambda exc: False,
            working_orders={
                "order-1": {
                    "submitted_at": 0.0,
                    "key": "OPTION:XYZ260101C00100000",
                    "action": "STOP",
                    "cancel_requested_at": None,
                    "limit_price": Decimal("1.05"),
                }
            },
        )
        fake_bot.price_sanity_ok = AutoTrader.price_sanity_ok.__get__(fake_bot)
        fake_bot.price_sanity_cooldown_ready = AutoTrader.price_sanity_cooldown_ready.__get__(fake_bot)
        reprice = AutoTrader.reprice_resting_option_exits.__get__(fake_bot)

        positions = [
            {
                "instrument_type": "OPTION",
                "symbol": "XYZ260101C00100000",
                "quantity": "1",
                "cost_price": "1.50",
            }
        ]
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice(positions)

        self.assertEqual(cancelled, ["order-1"])
        self.assertEqual(
            placed[0], ("XYZ260101C00100000", "SELL", 1, Decimal("0.95"))
        )
        self.assertEqual(
            fake_bot.working_orders["order-2"]["action"], "STOP"
        )

    def test_reprice_skips_a_broker_conflict_flagged_symbol(self):
        """Live incident precedent (PETZ, stock side): every other
        repricer already skips a broker-conflict-flagged symbol -
        these two option repricers never did.
        """
        from webull_bot.bot import AutoTrader

        cancelled = []
        placed = []
        contract = {
            "symbol": "XYZ260101C00100000",
            "underlying_symbol": "XYZ",
            "strike_price": "100",
            "expiration_date": "2026-01-01",
            "option_type": "CALL",
        }
        quote = {
            "symbol": "XYZ260101C00100000",
            "bid": "1.90",
            "ask": "2.00",
            "price": "1.95",
        }

        class FakeApi:
            @staticmethod
            def option_quotes(symbols):
                return [quote]

            @staticmethod
            def quote_ask(q):
                return Decimal(str(q["ask"]))

            @staticmethod
            def quote_price(q):
                return Decimal(str(q["price"]))

            @staticmethod
            def option_position(contract, positions):
                return Decimal("1"), Decimal("1.50")

            @staticmethod
            def cancel(order_id):
                cancelled.append(order_id)

            @staticmethod
            def place_option(contract, side, quantity, limit_price, position_intent):
                placed.append((contract["symbol"], side, quantity, limit_price))
                return "order-2"

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                poll_seconds=Decimal("0.25"), price_sanity_cooldown_seconds=60
            ),
            api=FakeApi(),
            status=SimpleNamespace(rekey_trade=lambda old, new: None),
            last_option_reprice=0.0,
            option_contracts=[contract],
            manual_touch_at={},
            price_sanity_rejected_at={},
            is_order_not_cancelable=lambda exc: False,
            broker_conflict_symbols={"XYZ260101C00100000"},
            working_orders={
                "order-1": {
                    "submitted_at": 0.0,
                    "key": "OPTION:XYZ260101C00100000",
                    "action": "PROFIT",
                    "cancel_requested_at": None,
                    "limit_price": Decimal("1.80"),
                }
            },
        )
        fake_bot.price_sanity_ok = AutoTrader.price_sanity_ok.__get__(fake_bot)
        fake_bot.price_sanity_cooldown_ready = AutoTrader.price_sanity_cooldown_ready.__get__(fake_bot)
        reprice = AutoTrader.reprice_resting_option_exits.__get__(fake_bot)

        positions = [
            {
                "instrument_type": "OPTION",
                "symbol": "XYZ260101C00100000",
                "quantity": "1",
                "cost_price": "1.50",
            }
        ]
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice(positions)

        self.assertEqual(cancelled, [])
        self.assertEqual(placed, [])


class RepriceRestingOptionEntriesTests(unittest.TestCase):
    """Options analog of reprice_resting_entries - by request: "why is
    the buy price not playing around in the spread, same with sell."
    Live incident: NKE and a UBER put both got cancelled unfilled
    after sitting at their original limit price for 120s, because
    only the option PROFIT-sell side chased the market - a resting
    option BUY never did.
    """

    def test_reprice_chases_the_ask_up_on_a_resting_option_buy(self):
        from webull_bot.bot import AutoTrader

        cancelled = []
        placed = []
        contract = {
            "symbol": "XYZ260101C00100000",
            "underlying_symbol": "XYZ",
            "strike_price": "100",
            "expiration_date": "2026-01-01",
            "option_type": "CALL",
        }
        quote = {
            "symbol": "XYZ260101C00100000",
            "bid": "1.90",
            "ask": "2.00",
            "price": "1.95",
        }

        class FakeApi:
            @staticmethod
            def option_quotes(symbols):
                return [quote]

            @staticmethod
            def option_limit_price(q, side):
                # Mirrors the real (bid+ask)/2 midpoint convention.
                return (Decimal(str(q["bid"])) + Decimal(str(q["ask"]))) / 2

            @staticmethod
            def quote_price(q):
                return Decimal(str(q["price"]))

            @staticmethod
            def cancel(order_id):
                cancelled.append(order_id)

            @staticmethod
            def place_option(contract, side, quantity, limit_price, position_intent):
                placed.append((contract["symbol"], side, quantity, limit_price))
                return "order-2"

        rekeyed = []
        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                poll_seconds=Decimal("0.25"), option_entry_escalate_seconds=30,
                price_sanity_cooldown_seconds=60
            ),
            api=FakeApi(),
            status=SimpleNamespace(
                rekey_trade=lambda old, new: rekeyed.append((old, new))
            ),
            last_option_entry_reprice=0.0,
            option_contracts=[contract],
            manual_touch_at={},
            price_sanity_rejected_at={},
            is_order_not_cancelable=lambda exc: False,
            working_orders={
                "order-1": {
                    # Resting 10s - under option_entry_escalate_seconds
                    # (30), so this should still chase mid, not ask.
                    "submitted_at": 90.0,
                    "key": "OPTION:XYZ260101C00100000",
                    "action": "BUY",
                    "cancel_requested_at": None,
                    "limit_price": Decimal("1.80"),
                    "quantity": 1,
                }
            },
        )
        fake_bot.price_sanity_ok = AutoTrader.price_sanity_ok.__get__(fake_bot)
        fake_bot.price_sanity_cooldown_ready = AutoTrader.price_sanity_cooldown_ready.__get__(fake_bot)
        reprice = AutoTrader.reprice_resting_option_entries.__get__(fake_bot)

        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice()

        # By request: "you don't have to buy at the edges of the
        # spread, the mid price is also fine" - chases the midpoint
        # (1.95), not the full ask (2.00).
        self.assertEqual(cancelled, ["order-1"])
        self.assertEqual(len(placed), 1)
        self.assertEqual(
            placed[0], ("XYZ260101C00100000", "BUY", 1, Decimal("1.95"))
        )
        self.assertNotIn("order-1", fake_bot.working_orders)
        self.assertIn("order-2", fake_bot.working_orders)
        self.assertEqual(
            fake_bot.working_orders["order-2"]["limit_price"], Decimal("1.95")
        )
        self.assertEqual(rekeyed, [("order-1", "order-2")])

    def test_never_reprices_to_a_worse_price_than_the_current_limit(self):
        from webull_bot.bot import AutoTrader

        cancelled = []
        placed = []
        contract = {
            "symbol": "XYZ260101C00100000",
            "underlying_symbol": "XYZ",
            "strike_price": "100",
            "expiration_date": "2026-01-01",
            "option_type": "CALL",
        }
        # Ask has fallen below the resting limit - nothing to chase.
        quote = {
            "symbol": "XYZ260101C00100000",
            "bid": "1.60",
            "ask": "1.70",
            "price": "1.65",
        }

        class FakeApi:
            @staticmethod
            def option_quotes(symbols):
                return [quote]

            @staticmethod
            def option_limit_price(q, side):
                return (Decimal(str(q["bid"])) + Decimal(str(q["ask"]))) / 2

            @staticmethod
            def quote_ask(q):
                return Decimal(str(q["ask"]))

            @staticmethod
            def quote_price(q):
                return Decimal(str(q["price"]))

            @staticmethod
            def _quantize_to_option_tick(price, rounding):
                tick = Decimal("0.05")
                steps = (price / tick).quantize(Decimal("1"), rounding=rounding)
                return (steps * tick).quantize(Decimal("0.01"))

            @staticmethod
            def cancel(order_id):
                cancelled.append(order_id)

            @staticmethod
            def place_option(contract, side, quantity, limit_price, position_intent):
                placed.append((contract["symbol"], side, quantity, limit_price))
                return "order-2"

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                poll_seconds=Decimal("0.25"), option_entry_escalate_seconds=30,
                price_sanity_cooldown_seconds=60
            ),
            api=FakeApi(),
            status=SimpleNamespace(rekey_trade=lambda old, new: None),
            last_option_entry_reprice=0.0,
            option_contracts=[contract],
            manual_touch_at={},
            price_sanity_rejected_at={},
            is_order_not_cancelable=lambda exc: False,
            working_orders={
                "order-1": {
                    "submitted_at": 0.0,
                    "key": "OPTION:XYZ260101C00100000",
                    "action": "BUY",
                    "cancel_requested_at": None,
                    "limit_price": Decimal("1.80"),
                    "quantity": 1,
                }
            },
        )
        fake_bot.price_sanity_ok = AutoTrader.price_sanity_ok.__get__(fake_bot)
        fake_bot.price_sanity_cooldown_ready = AutoTrader.price_sanity_cooldown_ready.__get__(fake_bot)
        reprice = AutoTrader.reprice_resting_option_entries.__get__(fake_bot)

        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice()

        self.assertEqual(cancelled, [])
        self.assertEqual(placed, [])

    def test_escalates_halfway_to_the_ask_once_the_entry_has_stalled(self):
        from webull_bot.bot import AutoTrader

        cancelled = []
        placed = []
        contract = {
            "symbol": "XYZ260101C00100000",
            "underlying_symbol": "XYZ",
            "strike_price": "100",
            "expiration_date": "2026-01-01",
            "option_type": "CALL",
        }
        # Wide spread so the halfway escalation target is clearly
        # distinct from both mid and the full ask after quantizing to
        # the real $0.05 option tick.
        quote = {
            "symbol": "XYZ260101C00100000",
            "bid": "1.70",
            "ask": "2.00",
            "price": "1.85",
        }

        class FakeApi:
            @staticmethod
            def option_quotes(symbols):
                return [quote]

            @staticmethod
            def option_limit_price(q, side):
                return (Decimal(str(q["bid"])) + Decimal(str(q["ask"]))) / 2

            @staticmethod
            def quote_ask(q):
                return Decimal(str(q["ask"]))

            @staticmethod
            def quote_price(q):
                return Decimal(str(q["price"]))

            @staticmethod
            def _quantize_to_option_tick(price, rounding):
                tick = Decimal("0.05")
                steps = (price / tick).quantize(Decimal("1"), rounding=rounding)
                return (steps * tick).quantize(Decimal("0.01"))

            @staticmethod
            def cancel(order_id):
                cancelled.append(order_id)

            @staticmethod
            def place_option(contract, side, quantity, limit_price, position_intent):
                placed.append((contract["symbol"], side, quantity, limit_price))
                return "order-2"

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                poll_seconds=Decimal("0.25"), option_entry_escalate_seconds=30,
                price_sanity_cooldown_seconds=60
            ),
            api=FakeApi(),
            status=SimpleNamespace(rekey_trade=lambda old, new: None),
            last_option_entry_reprice=0.0,
            option_contracts=[contract],
            manual_touch_at={},
            price_sanity_rejected_at={},
            is_order_not_cancelable=lambda exc: False,
            working_orders={
                "order-1": {
                    # Resting 40s - past option_entry_escalate_seconds
                    # (30), so this should escalate to the midpoint of
                    # mid and ask (rounded UP to a valid $0.05 tick),
                    # not track mid indefinitely and not jump straight
                    # to the full ask (a first version did that, and a
                    # VZ put dip-entry filled at the max-spread price
                    # as a result).
                    "submitted_at": 60.0,
                    "key": "OPTION:XYZ260101C00100000",
                    "action": "BUY",
                    "cancel_requested_at": None,
                    "limit_price": Decimal("1.80"),
                    "quantity": 1,
                }
            },
        )
        fake_bot.price_sanity_ok = AutoTrader.price_sanity_ok.__get__(fake_bot)
        fake_bot.price_sanity_cooldown_ready = AutoTrader.price_sanity_cooldown_ready.__get__(fake_bot)
        reprice = AutoTrader.reprice_resting_option_entries.__get__(fake_bot)

        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice()

        # mid=1.85, ask=2.00 -> raw halfway = 1.925, quantized UP to
        # the nearest $0.05 tick = 1.95 (distinct from both mid and
        # the full ask - proves this isn't silently degrading to
        # either extreme).
        self.assertEqual(cancelled, ["order-1"])
        self.assertEqual(
            placed[0], ("XYZ260101C00100000", "BUY", 1, Decimal("1.95"))
        )

    def test_a_sanity_rejected_escalation_backs_off_instead_of_retrying_every_cycle(self):
        """Live incident: NVDA's escalated (mid->ask) target sat
        durably past OPTION_PRICE_SANITY_TOLERANCE (a genuinely wide
        spread, not a bad quote) and got re-rejected on literally
        every poll cycle with zero backoff - 60 ERROR log lines in
        under 10 minutes for one contract. price_sanity_cooldown_ready
        must be checked before price_sanity_ok, not just recorded by
        it, or nothing ever actually backs off.
        """
        from webull_bot.bot import AutoTrader

        cancelled = []
        placed = []
        contract = {
            "symbol": "XYZ260101C00100000",
            "underlying_symbol": "XYZ",
            "strike_price": "100",
            "expiration_date": "2026-01-01",
            "option_type": "CALL",
        }
        # deviation from last=0.15 to ask=0.20 is 33.3%, past the 30%
        # option sanity tolerance - a real, durable wide-spread
        # rejection, not a transient blip.
        quote = {
            "symbol": "XYZ260101C00100000",
            "bid": "0.10",
            "ask": "0.20",
            "price": "0.15",
        }

        class FakeApi:
            @staticmethod
            def option_quotes(symbols):
                return [quote]

            @staticmethod
            def option_limit_price(q, side):
                return (Decimal(str(q["bid"])) + Decimal(str(q["ask"]))) / 2

            @staticmethod
            def quote_ask(q):
                return Decimal(str(q["ask"]))

            @staticmethod
            def quote_price(q):
                return Decimal(str(q["price"]))

            @staticmethod
            def _quantize_to_option_tick(price, rounding):
                tick = Decimal("0.05")
                steps = (price / tick).quantize(Decimal("1"), rounding=rounding)
                return (steps * tick).quantize(Decimal("0.01"))

            @staticmethod
            def cancel(order_id):
                cancelled.append(order_id)

            @staticmethod
            def place_option(contract, side, quantity, limit_price, position_intent):
                placed.append((contract["symbol"], side, quantity, limit_price))
                return "order-2"

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                poll_seconds=Decimal("0.25"), option_entry_escalate_seconds=30,
                price_sanity_cooldown_seconds=60
            ),
            api=FakeApi(),
            status=SimpleNamespace(rekey_trade=lambda old, new: None),
            last_option_entry_reprice=0.0,
            option_contracts=[contract],
            manual_touch_at={},
            price_sanity_rejected_at={},
            is_order_not_cancelable=lambda exc: False,
            working_orders={
                "order-1": {
                    "submitted_at": 60.0,
                    "key": "OPTION:XYZ260101C00100000",
                    "action": "BUY",
                    "cancel_requested_at": None,
                    "limit_price": Decimal("0.05"),
                    "quantity": 1,
                }
            },
        )
        fake_bot.price_sanity_ok = AutoTrader.price_sanity_ok.__get__(fake_bot)
        fake_bot.price_sanity_cooldown_ready = AutoTrader.price_sanity_cooldown_ready.__get__(fake_bot)
        reprice = AutoTrader.reprice_resting_option_entries.__get__(fake_bot)

        # First call: past the escalate threshold, hits the sanity
        # rejection, stamps price_sanity_rejected_at.
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice()
        self.assertEqual(placed, [])
        self.assertIn("XYZ260101C00100000", fake_bot.price_sanity_rejected_at)

        # Second call, moments later (well within the cooldown window):
        # must skip retrying entirely, not re-attempt and re-reject.
        fake_bot.last_option_entry_reprice = 0.0
        with unittest.mock.patch("time.monotonic", return_value=100.5):
            reprice()
        self.assertEqual(placed, [])
        self.assertEqual(cancelled, [])

    def test_reprice_skips_a_broker_conflict_flagged_symbol(self):
        """Live incident precedent (PETZ, stock side): every other
        repricer already skips a broker-conflict-flagged symbol -
        these two option repricers never did.
        """
        from webull_bot.bot import AutoTrader

        cancelled = []
        placed = []
        contract = {
            "symbol": "XYZ260101C00100000",
            "underlying_symbol": "XYZ",
            "strike_price": "100",
            "expiration_date": "2026-01-01",
            "option_type": "CALL",
        }
        quote = {
            "symbol": "XYZ260101C00100000",
            "bid": "1.90",
            "ask": "2.00",
            "price": "1.95",
        }

        class FakeApi:
            @staticmethod
            def option_quotes(symbols):
                return [quote]

            @staticmethod
            def option_limit_price(q, side):
                return (Decimal(str(q["bid"])) + Decimal(str(q["ask"]))) / 2

            @staticmethod
            def quote_price(q):
                return Decimal(str(q["price"]))

            @staticmethod
            def cancel(order_id):
                cancelled.append(order_id)

            @staticmethod
            def place_option(contract, side, quantity, limit_price, position_intent):
                placed.append((contract["symbol"], side, quantity, limit_price))
                return "order-2"

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                poll_seconds=Decimal("0.25"), option_entry_escalate_seconds=30,
                price_sanity_cooldown_seconds=60
            ),
            api=FakeApi(),
            status=SimpleNamespace(rekey_trade=lambda old, new: None),
            last_option_entry_reprice=0.0,
            option_contracts=[contract],
            manual_touch_at={},
            price_sanity_rejected_at={},
            is_order_not_cancelable=lambda exc: False,
            broker_conflict_symbols={"XYZ260101C00100000"},
            working_orders={
                "order-1": {
                    "submitted_at": 0.0,
                    "key": "OPTION:XYZ260101C00100000",
                    "action": "BUY",
                    "cancel_requested_at": None,
                    "limit_price": Decimal("1.80"),
                    "quantity": 1,
                }
            },
        )
        fake_bot.price_sanity_ok = AutoTrader.price_sanity_ok.__get__(fake_bot)
        fake_bot.price_sanity_cooldown_ready = AutoTrader.price_sanity_cooldown_ready.__get__(fake_bot)
        reprice = AutoTrader.reprice_resting_option_entries.__get__(fake_bot)

        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice()

        self.assertEqual(cancelled, [])
        self.assertEqual(placed, [])


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
                option_sell_fee_per_contract=Decimal("0"),
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
        # Throttle is now poll_seconds (0.25s, lowered by request - "bring
        # repricing to the 0.25 lane as well"), not order_monitor_seconds -
        # 0.1s elapsed still needs to be inside that smaller window.
        fake_bot.last_entry_reprice = 99.9
        reprice = AutoTrader.reprice_resting_entries.__get__(fake_bot)
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            reprice(True)
        self.assertEqual(cancelled, [])
        self.assertEqual(placed, [])
