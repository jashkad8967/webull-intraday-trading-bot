"""A stop held by the broker, not by this process.

Every stop in this bot was software-only: the protection loop reads a
quote, decides the stop is breached, and submits a sell. Protection
therefore existed only while the process was alive - and on 2026-10-02
that assumption broke in production:

    08:16:48 ERROR WATCHDOG| main scan loop has not ticked for 16804s
             (limit 900s) - exiting so the container restarts

Modern Standby suspended the laptop and the main loop did not tick for
four hours forty. The account happened to be flat. A position held
through that window would have had no stop of any kind for the whole of
it, and nothing in the design would have noticed.

A broker-side stop survives what the software stop cannot: the machine
sleeping, power loss, a crash between restarts, and the absence of any
supervising session. The SDK exposed OrderType.STOP_LOSS the entire time;
this codebase had only ever sent LIMIT and MARKET.

Two properties carry real risk and are pinned here:

  * GTC, not DAY. The window being covered includes overnight, when
    nothing is running to re-place a DAY order. The price of that is an
    orphaned stop outliving its position, which is why
    resting_stop_orders exists to find them.
  * FRACTIONAL IS REFUSED. Webull supports fractional shares only as
    core-hours MARKET orders, so a fractional stop cannot rest. At $25 of
    notional against a $231 share this account's stock positions ARE
    fractional (0.1047 NVDA), so they cannot be protected this way -
    whole shares in cheaper names can be, and option contracts always
    can. Failing loudly keeps that visible instead of letting the broker
    reject it opaquely.
"""

import unittest
from decimal import Decimal
from types import SimpleNamespace

from webull_bot.api.orders import place_stock_stop_loss, resting_stop_orders


class _Api:
    """Captures the order dict instead of sending it."""

    def __init__(self):
        self.sent = []
        self.config = SimpleNamespace(account_id="acct-1")
        self.trade = SimpleNamespace(
            order_v3=SimpleNamespace(place_order=self._place)
        )

    def _place(self, account_id, orders):
        self.sent.append((account_id, orders))
        return {"ok": True}

    def _call(self, fn, bucket, retry=True):
        return fn()

    @staticmethod
    def price_tick_size(price):
        return Decimal("0.01")


class PlaceStockStopLossTests(unittest.TestCase):
    def setUp(self):
        self.api = _Api()

    def _place(self, quantity, stop="10.00", symbol="F"):
        return place_stock_stop_loss(
            self.api, symbol, quantity, Decimal(stop)
        )

    def test_the_order_is_a_resting_stop_loss(self):
        self._place(2)
        _, orders = self.api.sent[0]
        order = orders[0]
        self.assertEqual(order["order_type"], "STOP_LOSS")
        self.assertEqual(order["side"], "SELL")
        self.assertEqual(order["stop_price"], "10.00")
        self.assertEqual(order["quantity"], "2")
        self.assertNotIn(
            "limit_price", order,
            "a plain STOP_LOSS must not carry a limit price - that is "
            "STOP_LOSS_LIMIT, which can fail to fill in a fast move",
        )

    def test_it_rests_until_cancelled_not_just_for_the_day(self):
        """DAY would expire overnight, which is exactly the window this
        exists to cover - nothing is running then to re-place it.
        """
        self._place(2)
        self.assertEqual(self.api.sent[0][1][0]["time_in_force"], "GTC")

    def test_a_fractional_quantity_is_refused_with_the_reason(self):
        """The live constraint. 0.1047 NVDA cannot be protected this way,
        and the error has to say why rather than letting the broker
        reject it opaquely.
        """
        with self.assertRaises(ValueError) as caught:
            self._place(Decimal("0.1047"), symbol="NVDA")
        message = str(caught.exception)
        self.assertIn("FRACTIONAL", message)
        self.assertIn("whole shares", message)
        self.assertEqual(self.api.sent, [], "placed a doomed order anyway")

    def test_a_whole_decimal_quantity_is_accepted(self):
        """Decimal('2') is whole even though it is not an int - the
        positions feed returns strings, so this is the normal case."""
        self._place(Decimal("2"))
        self.assertEqual(self.api.sent[0][1][0]["quantity"], "2")

    def test_a_non_positive_quantity_is_refused(self):
        for bad in (0, Decimal("0"), Decimal("-1")):
            with self.subTest(quantity=bad):
                with self.assertRaises(ValueError):
                    self._place(bad)
        self.assertEqual(self.api.sent, [])

    def test_the_stop_price_is_quantized_to_the_tick(self):
        self._place(2, stop="10.004")
        self.assertEqual(self.api.sent[0][1][0]["stop_price"], "10.00")


class RestingStopOrdersTests(unittest.TestCase):
    """Finding orphans is not optional under GTC: a stop left behind
    because the process died between an exit filling and the cancel could
    sell a LATER position in the same symbol. That is the one way this
    feature loses money instead of saving it.
    """

    def _api(self, groups):
        return SimpleNamespace(open_orders=lambda: groups)

    def test_it_finds_a_resting_stop_inside_a_group(self):
        api = self._api([
            {"orders": [
                {"order_type": "LIMIT", "symbol": "AAA",
                 "client_order_id": "a"},
                {"order_type": "STOP_LOSS", "symbol": "F",
                 "client_order_id": "b"},
            ]},
        ])
        found = resting_stop_orders(api)
        self.assertEqual([o["client_order_id"] for o in found], ["b"])

    def test_it_finds_a_bare_order_not_wrapped_in_a_group(self):
        api = self._api([
            {"order_type": "STOP_LOSS", "symbol": "F", "client_order_id": "b"},
        ])
        self.assertEqual(len(resting_stop_orders(api)), 1)

    def test_it_ignores_non_stop_orders(self):
        api = self._api([
            {"order_type": "LIMIT", "client_order_id": "a"},
            {"order_type": "MARKET", "client_order_id": "b"},
        ])
        self.assertEqual(resting_stop_orders(api), [])

    def test_no_open_orders_is_not_an_error(self):
        for empty in ([], None):
            with self.subTest(value=empty):
                self.assertEqual(resting_stop_orders(self._api(empty)), [])

    def test_matching_is_case_insensitive(self):
        api = self._api([{"order_type": "stop_loss", "client_order_id": "b"}])
        self.assertEqual(len(resting_stop_orders(api)), 1)


if __name__ == "__main__":
    unittest.main()
