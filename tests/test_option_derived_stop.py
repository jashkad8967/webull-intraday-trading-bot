"""Scale the option stop to the UNDERLYING's move, not to the premium.

THE DEFECT, measured live 2026-10-01 at a cost of $7.07 in 150 seconds:

    PFE261009C00028000  premium 0.50  delta 0.6187  PFE spot 28.21
    option_stop_loss_percent 10%  ->  a 0.29% move in PFE

PFE moves 0.29% many times an hour, so that position was not stopped out
by an adverse trend - it was stopped out by the next tick. It had passed
every gate the bot had: liquidity, entry spread, delta far above the 0.20
floor, and the round-trip-cost hurdle. Nothing checked the stop against
the instrument it was applied to.

An option is levered to its underlying by premium/(delta x spot), roughly
35x for a cheap at-the-money contract, so the same percentage of premium
means something completely different on two contracts. Inverting it:

    stop_fraction = target_underlying_move x |delta| x spot / premium

One formula explains the whole loss history that was blamed on exit
logic: the held=0s exits, the 9-of-10 positions that never traded above
cost+fee on the bid, the NKE stop that fired six minutes after fill on a
$0.15 premium, and why 5% was catastrophic rather than merely tight (5%
of premium was a 0.13% move - dead on submission). Six exit-ladder
configurations were swept across the recorded tape hunting for this and
none could have found it, because the tape held neither delta nor spot.

The uncomfortable half is kept visible rather than tuned away: a
correctly scaled stop costs more in dollars, and when it costs more than
the account should risk the ENTRY is refused. A stop shrunk to be
affordable is the original defect with a new number.
"""

import unittest
from decimal import Decimal

from webull_bot.strategy_logic.options.derived_stop import (
    IMPLAUSIBLE_STOP_FRACTION,
    option_stop_fraction,
    option_stop_risk_dollars,
)
from webull_bot.trading.guards.price_sanity import (
    option_stop_implied_underlying_move,
)

TARGET = Decimal("0.005")


class DerivedStopTests(unittest.TestCase):
    def test_the_live_pfe_contract_gets_17_5_percent_not_10(self):
        stop = option_stop_fraction(
            Decimal("0.50"), Decimal("0.6187"), Decimal("28.21"), TARGET
        )
        self.assertAlmostEqual(float(stop) * 100, 17.45, places=1)

    def test_the_derived_stop_means_the_target_move_by_construction(self):
        """The whole point: whatever the contract, the stop corresponds to
        the same move in its own underlying. That is what makes
        option_min_stop_underlying_move_percent satisfiable by design
        rather than by luck.
        """
        for premium, delta, spot in (
            ("0.50", "0.6187", "28.21"),
            ("0.57", "0.0424", "764.28"),
            ("4.53", "0.32", "764.28"),
            ("1.74", "0.34", "280.22"),
        ):
            premium, delta, spot = Decimal(premium), Decimal(delta), Decimal(spot)
            stop = option_stop_fraction(premium, delta, spot, TARGET)
            implied = option_stop_implied_underlying_move(
                premium, delta, spot, stop
            )
            with self.subTest(premium=premium, delta=delta):
                self.assertAlmostEqual(float(implied), float(TARGET), places=6)

    def test_the_flat_ten_percent_was_noise_on_that_contract(self):
        """For contrast, the value being replaced."""
        implied = option_stop_implied_underlying_move(
            Decimal("0.50"), Decimal("0.6187"), Decimal("28.21"),
            Decimal("0.10"),
        )
        self.assertLess(float(implied) * 100, 0.30)

    def test_a_thinner_delta_needs_a_wider_stop(self):
        """A contract that barely responds to its underlying must tolerate
        a larger percentage move before the stop means anything.
        """
        fat = option_stop_fraction(
            Decimal("0.50"), Decimal("0.62"), Decimal("28.21"), TARGET
        )
        thin = option_stop_fraction(
            Decimal("0.50"), Decimal("0.08"), Decimal("28.21"), TARGET
        )
        self.assertGreater(fat, thin)

    def test_missing_greeks_fall_back_rather_than_leave_it_unprotected(self):
        """Some account snapshots omit delta. Returning None lets the
        caller keep the flat configured percentage - a mis-scaled stop is
        bad, no stop at all is worse.
        """
        for args in (
            (None, Decimal("0.5"), Decimal("28"), TARGET),
            (Decimal("0.5"), None, Decimal("28"), TARGET),
            (Decimal("0.5"), Decimal("0.5"), None, TARGET),
            (Decimal("0"), Decimal("0.5"), Decimal("28"), TARGET),
            (Decimal("0.5"), Decimal("0"), Decimal("28"), TARGET),
        ):
            with self.subTest(args=args):
                self.assertIsNone(option_stop_fraction(*args))

    def test_a_zero_target_disables_derivation(self):
        self.assertIsNone(
            option_stop_fraction(
                Decimal("0.50"), Decimal("0.62"), Decimal("28.21"),
                Decimal("0"),
            )
        )

    def test_an_implausible_stop_is_returned_not_clamped(self):
        """A deep-ITM contract can derive a stop above 100% of premium.
        It is returned as-is so the caller can SEE it and refuse, rather
        than being silently clamped into something that looks tradeable.
        """
        stop = option_stop_fraction(
            Decimal("0.05"), Decimal("0.95"), Decimal("500"), TARGET
        )
        self.assertGreater(stop, IMPLAUSIBLE_STOP_FRACTION)


class StopRiskDollarsTests(unittest.TestCase):
    """The number that makes this fix uncomfortable, and must therefore be
    reported rather than hidden.
    """

    def test_the_live_pfe_contract_risks_about_nine_dollars(self):
        risk = option_stop_risk_dollars(
            Decimal("0.50"), 1, Decimal("0.1745")
        )
        self.assertAlmostEqual(float(risk), 8.73, places=1)

    def test_that_is_eleven_percent_of_this_account(self):
        """$8.73 against $80.57 of buying power, with one contract the
        minimum size - so it cannot be reduced by trading smaller. Either
        the account can afford a correctly stopped position or it cannot.
        """
        risk = option_stop_risk_dollars(Decimal("0.50"), 1, Decimal("0.1745"))
        self.assertGreater(risk / Decimal("80.57"), Decimal("0.10"))
        self.assertLess(risk / Decimal("80.57"), Decimal("0.12"))

    def test_the_12_percent_cap_admits_pfe_and_refuses_the_rest(self):
        """Measured contracts from 2026-10-01 against a $80.57 account.
        Exactly one is tradeable, and it is the one on a CHEAP underlying -
        which is what makes an at-the-money contract affordable while
        still carrying real delta.
        """
        account = Decimal("80.57")
        cap = account * Decimal("0.12")
        cases = {
            "PFE 28C": ("0.50", "0.6187", "28.21", True),
            "SPY 710P": ("0.57", "0.0424", "764.28", False),
            "IWM 277P": ("1.74", "0.34", "280.22", False),
            "SPY 755P": ("4.53", "0.32", "764.28", False),
        }
        for name, (premium, delta, spot, expected) in cases.items():
            premium = Decimal(premium)
            stop = option_stop_fraction(
                premium, Decimal(delta), Decimal(spot), TARGET
            )
            risk = option_stop_risk_dollars(premium, 1, stop)
            affordable = premium * 100 <= account
            with self.subTest(contract=name):
                self.assertEqual(affordable and risk <= cap, expected)

    def test_risk_scales_with_contract_count(self):
        one = option_stop_risk_dollars(Decimal("0.50"), 1, Decimal("0.175"))
        two = option_stop_risk_dollars(Decimal("0.50"), 2, Decimal("0.175"))
        self.assertEqual(two, one * 2)


if __name__ == "__main__":
    unittest.main()
