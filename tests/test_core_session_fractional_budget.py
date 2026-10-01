"""A small account must not silently stop being able to trade.

Live 2026-10-01 13:53. The account had drifted from $84.70 to $80.54 over
the session, and:

    $80.54 x stock_core_session_position_fraction (0.30) = $24.16
    fractional_shares_min_notional                        = $25.00

dollar_stock_quantity refuses any target below the minimum, so every
share entry died with "no affordable quantity". The identical NVDA setup
had been bought an hour earlier at $84.70, where 30% came to $25.41 and
cleared.

Nothing announced the transition. Below a balance of $83.33 the bot
simply stops being able to open a share position, and the logs look
exactly like a day with no setups - the same failure signature as the
four silent dead ends in the option path. Combined with the option side
being (correctly) blocked by option_min_stop_underlying_move_percent, the
account could place NO trade at all while appearing to run normally.

If the minimum is affordable, trade the minimum. The concentration is
real - $25 of an $80 account is 31% notional - but position RISK is set
by the stop, not the notional: at a 0.9-1.5% stock stop that is
$0.23-$0.38. Being unable to trade is the worse end of the trade-off.
"""

import unittest
from decimal import Decimal
from types import SimpleNamespace

from webull_bot.strategy_logic.sizing.order_quantity import (
    core_session_fractional_budget,
)


def _budget(buying_power, fraction="0.30", minimum="25"):
    strategy = SimpleNamespace(
        config=SimpleNamespace(
            stock_core_session_position_fraction=Decimal(fraction),
            fractional_shares_min_notional=Decimal(minimum),
        )
    )
    return core_session_fractional_budget(strategy, Decimal(buying_power))


class CoreSessionFractionalBudgetTests(unittest.TestCase):
    def test_the_live_cliff_is_gone(self):
        """$80.54 x 0.30 = $24.16, under the $25 minimum -> was 0."""
        self.assertEqual(_budget("80.54"), Decimal("25"))

    def test_an_account_above_the_cliff_is_unchanged(self):
        """$84.70 x 0.30 = $25.41, already clears the minimum, so this
        must not alter sizing for a healthy balance.
        """
        self.assertEqual(_budget("84.70"), Decimal("25.410"))

    def test_the_exact_boundary_is_unchanged(self):
        # 83.3333... x 0.30 == 25.00 exactly.
        self.assertEqual(_budget("83.34"), Decimal("25.002"))

    def test_an_account_that_cannot_cover_the_minimum_is_not_inflated(self):
        """The clamp must never exceed buying power. At $20 the minimum
        is unaffordable, so the budget stays the honest (unusable) number
        rather than inventing capital the account does not have.
        """
        self.assertEqual(_budget("20"), Decimal("6.00"))

    def test_buying_power_exactly_at_the_minimum_clamps_up(self):
        self.assertEqual(_budget("25"), Decimal("25"))

    def test_a_large_account_is_untouched(self):
        self.assertEqual(_budget("10000"), Decimal("3000.00"))


class EveryCoreSessionSizingSiteUsesTheHelperTests(unittest.TestCase):
    """Both the manual path and the automatic scan pool compute this
    budget. A floor applied to one of them is the same silent cliff with
    a smaller blast radius.
    """

    PATHS = (
        "src/webull_bot/trading/orders/manual_buy.py",
        "src/webull_bot/trading/screeners/stock_scan_batch.py",
    )

    def test_no_site_multiplies_the_fraction_by_hand(self):
        from pathlib import Path

        repo = Path(__file__).resolve().parent.parent
        for relative in self.PATHS:
            text = (repo / relative).read_text(encoding="utf-8")
            self.assertNotIn(
                "buying_power * self.config.stock_core_session_position_fraction",
                text,
                f"{relative} still computes the budget by hand, so it "
                f"keeps the sub-minimum cliff",
            )
            self.assertIn(
                "core_session_fractional_budget", text,
                f"{relative} does not use the floored budget helper",
            )


if __name__ == "__main__":
    unittest.main()
