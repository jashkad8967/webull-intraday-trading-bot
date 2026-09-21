import unittest
import unittest.mock



class OrderNotCancelableTests(unittest.TestCase):
    """is_order_not_cancelable - by request, after live evidence: a
    repricer/escalator cancelling an order that Webull has already
    started filling gets OPENAPI_ORDER_CAN_NOT_CANCEL back - a benign
    race, not a fault, so it should log as a WARNING (skip, order
    already resolving) rather than an ERROR.
    """

    def test_true_for_the_webull_cancel_rejection_code(self):
        from webull_bot.bot import AutoTrader

        exc = Exception(
            "HTTP Status: 417, Code: OPENAPI_ORDER_CAN_NOT_CANCEL, "
            "Msg: Order cannot be cancelled!"
        )
        self.assertTrue(AutoTrader.is_order_not_cancelable(exc))

    def test_false_for_an_unrelated_error(self):
        from webull_bot.bot import AutoTrader

        self.assertFalse(AutoTrader.is_order_not_cancelable(Exception("timeout")))

    def test_true_for_the_can_not_be_cancel_variant(self):
        """Live incident ("why does it keep trying to sell RIVN for
        0.55"): Webull's REAL error code is OPENAPI_ORDER_CAN_NOT_BE_
        CANCEL (with "BE") - the original exact-substring check for
        CAN_NOT_CANCEL never matched it, so this benign race got
        misclassified as an unrecognized ERROR every time it fired.
        """
        from webull_bot.bot import AutoTrader

        exc = Exception(
            "HTTP Status: 417, Code: OPENAPI_ORDER_CAN_NOT_BE_CANCEL, "
            "Msg: Order can not be canceled"
        )
        self.assertTrue(AutoTrader.is_order_not_cancelable(exc))


class SellWithNoPositionTests(unittest.TestCase):
    """is_sell_with_no_position - by request ("check the logs and see
    what errors happened, and ensure they do not happen again"): live
    incident, AHMA - the fast held-exit evaluator's cached_positions
    snapshot showed a stale nonzero quantity for a symbol already
    closed, so it tried to sell it again and got rejected as an
    attempted short.
    """

    def test_true_for_the_webull_no_position_rejection_code(self):
        from webull_bot.bot import AutoTrader

        exc = Exception(
            "HTTP Status: 417, Code: "
            "OPENAPI_NEW_NO_POSITION_MARGIN_ACCOUNT_CAN_NOT_SELL_SHORT_FOR_LT_2K, "
            "Msg: You currently have no open positions in AHMA."
        )
        self.assertTrue(AutoTrader.is_sell_with_no_position(exc))

    def test_false_for_an_unrelated_error(self):
        from webull_bot.bot import AutoTrader

        self.assertFalse(AutoTrader.is_sell_with_no_position(Exception("timeout")))


class OtcExtendedHoursUnsupportedTests(unittest.TestCase):
    """is_otc_extended_hours_unsupported - by request ("check the logs
    and see what errors happened, and ensure they do not happen
    again"): live incident, NLST - an OTC-listed security has no
    extended-hours session at all, and this rejection was falling
    through to a raw, unclassified ERROR log.
    """

    def test_true_for_the_webull_otc_extended_hours_rejection_code(self):
        from webull_bot.bot import AutoTrader

        exc = Exception(
            "HTTP Status: 417, Code: OPENAPI_OTC_TICKER_NOT_SUPPORT_X_P, "
            "Msg: Extended hours trading is not available for OTC "
            "market stock."
        )
        self.assertTrue(AutoTrader.is_otc_extended_hours_unsupported(exc))

    def test_false_for_an_unrelated_error(self):
        from webull_bot.bot import AutoTrader

        self.assertFalse(
            AutoTrader.is_otc_extended_hours_unsupported(Exception("timeout"))
        )


class OrderReversesExistingPositionTests(unittest.TestCase):
    """is_order_reverses_existing_position - live incident:
    escalate_stalled_stop_losses cancels a stalled exit then
    immediately submits a fresh aggressive sell on the same cycle;
    Webull's cancel acknowledgement doesn't guarantee the old order
    has cleared its own book yet, so the new sell can get rejected
    with OPENAPI_ORDER_NOT_SUPPORT_REVERSE_OPTION - a benign timing
    race (despite "OPTION" in the code name, this fires for stock
    orders too), not a fault.
    """

    def test_true_for_the_webull_reverse_position_rejection_code(self):
        from webull_bot.bot import AutoTrader

        exc = Exception(
            "HTTP Status: 417, Code: OPENAPI_ORDER_NOT_SUPPORT_REVERSE_OPTION, "
            "Msg: This order cannot be entered because it will reverse an "
            "existing position."
        )
        self.assertTrue(AutoTrader.is_order_reverses_existing_position(exc))

    def test_false_for_an_unrelated_error(self):
        from webull_bot.bot import AutoTrader

        self.assertFalse(
            AutoTrader.is_order_reverses_existing_position(Exception("timeout"))
        )
