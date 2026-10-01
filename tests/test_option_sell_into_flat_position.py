"""The option-side stale-position race.

Live 2026-09-28..10-01: 15 ERROR lines reading "held-option exit
failed | <contract> | HTTP Status: 417 ..." across four sessions, plus
5 more from the stall sweep. Every single one landed 5-36 seconds after
a fill warning on the same contract:

  08:51:53 filled at 1.07 ... -> 08:52:06 held-option exit failed
  09:08:45 filled at 1.20 ... -> 09:08:53 held-option exit failed
  10:12:26 filled at 1.06 ... -> 10:12:36 held-option exit failed
  11:16:35 filled at 1.39 ... -> 11:16:48 held-option exit failed

evaluate_held_option_exits reads self.cached_positions, refreshed once
per SLOW scan. A sell that fills between refreshes leaves the contract
still listed as held while BOTH existing guards come up clear - the
order is gone because it FILLED, so has_pending_sell_order is False and
pending_option_exits was discarded - and the fast loop submits a second
sell into a flat position. Webull's rejection is what prevents the
double-sell.

The stock side has handled this since the INN/OIS/RDHL/SOAR incident
(is_sell_with_no_position + zeroing the stale entry). That detector
matches only "NO_POSITION", and none of the four option codes contain
that substring, so the option path never self-healed and every
occurrence surfaced as an unexplained ERROR - which is why this read
for days as another bare-underlying/OCC key mismatch.
"""

import logging
import unittest
from decimal import Decimal
from types import SimpleNamespace

from webull_bot.errors.option_sell_into_flat_position import (
    is_option_sell_into_flat_position,
)
from webull_bot.errors.sell_with_no_position import is_sell_with_no_position
from webull_bot.trading.orders.held_option_exit_evaluation import (
    evaluate_held_option_exits,
)

# Verbatim from the live log, so a broker wording change is caught here
# rather than by the race silently going unhandled again.
LIVE_REJECTIONS = (
    "HTTP Status: 417, Code: OPENAPI_POSITION_ORDER_INTENT_MISMATCH, "
    "Msg: Close intent mismatches position direction., "
    "RequestID: f04eb438-0d1d-468c-b5fd-44b5ab0f0857",
    "HTTP Status: 417, Code: OPENAPI_OPTION_CAVERED_CALL_STOCK_NO_ENOUGH, "
    "Msg: You hold an insufficient number of underlying shares to sell "
    "this covered call., RequestID: 4d57ed3c-886b-43cd-a138-77a86144a133",
    "HTTP Status: 417, Code: "
    "OPENAPI_OPTION_LONG_POSITION_MUST_BE_CLOSE_THAN_SELL_SHORT, "
    "Msg: You can not place order in excess of current holding quantity "
    "to create a position on the other side of the market. Please check "
    "your open orders and try again., "
    "RequestID: a6bc7808-2a47-444b-8725-727e02a91c7d",
    "HTTP Status: 417, Code: OPENAPI_ORDER_NOT_SUPPORT_REVERSE_OPTION, "
    "Msg: This order cannot be entered because it will reverse an "
    "existing position. You may need to close an open position, or "
    "cancel an open order, before you can submit this order., "
    "RequestID: 9e970674-f952-4fe0-9178-ab19e64b4883",
)


class OptionSellIntoFlatPositionTests(unittest.TestCase):
    def test_every_live_rejection_is_recognised(self):
        for message in LIVE_REJECTIONS:
            with self.subTest(message=message[:60]):
                self.assertTrue(
                    is_option_sell_into_flat_position(Exception(message))
                )

    def test_the_stock_detector_misses_all_of_them(self):
        """The reason this went unhandled for four sessions. Kept as a
        test so nobody 'simplifies' the two detectors into one on the
        assumption the stock one already covered options.
        """
        for message in LIVE_REJECTIONS:
            with self.subTest(message=message[:60]):
                self.assertFalse(is_sell_with_no_position(Exception(message)))

    def test_a_genuine_failure_is_not_swallowed(self):
        for message in (
            "timeout",
            "HTTP Status: 403, Code: CLOCK_SKEW_EXCEEDED, Msg: The request "
            "timestamp is outside the allowed time window.",
            "HTTP Status: 417, Code: OPENAPI_DAY_BUYING_POWER_INSUFFICIENT, "
            "Msg: Buying power is insufficient.",
            "SDK.HttpError ('Connection aborted.', RemoteDisconnected())",
        ):
            with self.subTest(message=message[:50]):
                self.assertFalse(
                    is_option_sell_into_flat_position(Exception(message))
                )

    def test_buying_power_insufficient_stays_an_error(self):
        """Specifically guarded: it is also a 417 on an option order and
        it also appeared in the same logs, but it means the opposite -
        the position is real and we could not afford to act. Swallowing
        it would hide a genuine sizing problem.
        """
        self.assertFalse(
            is_option_sell_into_flat_position(
                Exception("OPENAPI_DAY_BUYING_POWER_INSUFFICIENT")
            )
        )

    def test_matching_is_case_insensitive(self):
        self.assertTrue(
            is_option_sell_into_flat_position(
                Exception("openapi_position_order_intent_mismatch")
            )
        )


class HeldOptionExitStaleCacheTests(unittest.TestCase):
    """The behaviour, not just the detector: the stale cached entry must
    actually be corrected, or the fast loop re-submits the identical
    doomed sell on every pass until the next slow refresh.
    """

    OCC = "UBER261009P00067000"

    def _bot(self, raises):
        # Webull reports an option position's `symbol` as the BARE
        # UNDERLYING, with the contract in `legs` - reproduced here
        # because it is exactly why this fix must not match by symbol.
        position = {
            "instrument_type": "OPTION",
            "symbol": "UBER",
            "quantity": "1",
            "cost_price": "1.20",
        }
        quote = {"symbol": self.OCC, "bid": 1.07, "ask": 1.16, "price": 1.10}

        def _raise(*args, **kwargs):
            raise Exception(raises)

        return SimpleNamespace(
            config=SimpleNamespace(
                held_option_exit_enabled=True,
                held_option_exit_seconds=Decimal("0"),
                quote_tape_enabled=False,
            ),
            last_held_option_exit_scan=0.0,
            cached_positions=[position],
            cached_option_buying_power=Decimal("85"),
            pending_option_exits=set(),
            has_pending_sell_order=lambda key: False,
            api=SimpleNamespace(
                contract_from_position=lambda item: {
                    "symbol": self.OCC,
                    "expiration_date": "2026-10-09",
                },
                option_quotes=lambda symbols: [quote],
                quote_bid=lambda q: Decimal("1.07"),
                quote_ask=lambda q: Decimal("1.16"),
                quote_price=lambda q: Decimal("1.10"),
            ),
            _evaluate_option_exit=_raise,
        )

    def test_the_stale_position_is_zeroed_and_logged_as_a_warning(self):
        bot = self._bot(LIVE_REJECTIONS[0])
        with self.assertLogs("webull-bot", level="WARNING") as captured:
            evaluate_held_option_exits(bot)
        self.assertEqual(bot.cached_positions[0]["quantity"], "0")
        joined = "\n".join(captured.output)
        self.assertIn("already closed", joined)
        self.assertNotIn("ERROR", joined)

    def test_a_second_pass_no_longer_retries_the_doomed_sell(self):
        """The point of zeroing. The quantity <= 0 filter at the top of
        the function must now exclude it, so _evaluate_option_exit is
        not reached at all.
        """
        bot = self._bot(LIVE_REJECTIONS[1])
        with self.assertLogs("webull-bot", level="WARNING"):
            evaluate_held_option_exits(bot)
        calls = []
        bot._evaluate_option_exit = lambda *a, **k: calls.append(a)
        bot.last_held_option_exit_scan = 0.0
        evaluate_held_option_exits(bot)
        self.assertEqual(calls, [], "a flat position must not be re-sold")

    def test_a_genuine_failure_still_logs_an_error_and_keeps_the_position(self):
        bot = self._bot("HTTP Status: 500, Msg: broker exploded")
        with self.assertLogs("webull-bot", level="ERROR") as captured:
            evaluate_held_option_exits(bot)
        self.assertEqual(bot.cached_positions[0]["quantity"], "1")
        self.assertIn("held-option exit failed", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
