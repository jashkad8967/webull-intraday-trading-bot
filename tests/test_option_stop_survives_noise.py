"""An option stop must describe a real move, not the next tick.

Measured live 2026-10-01 at a cost of $7.07 in 150 seconds:

    12:52:29  BUY  PFE261009C00028000  limit 0.50
    12:54:58  STOP PFE261009C00028000  filled 0.43   pnl -$7.07
    12:55:08  BUY  PFE261009P00028500  limit 0.50    (flipped direction)

That contract passed every gate the bot had: liquidity, entry spread,
delta (0.6187, comfortably above the 0.20 floor) and the round-trip-cost
hurdle. It still could not survive, because nothing checked the stop
against the instrument it was applied to.

An option is levered to its underlying by premium/(delta x spot). For
PFE at 28.21 with a 0.50 premium and 0.62 delta that is ~35x, so:

    a 10% stop on the OPTION  =  a 0.29% move in PFE

PFE moves 0.29% many times an hour. The position was not stopped out by
an adverse trend; it was stopped out by noise, and 8.3% of the account
went with it.

This is the defect behind everything previously attributed to exit
logic:

  * positions exiting at held=0s
  * 9 of 10 recorded positions never trading above cost+fee on the bid
  * the 5% stop that cost $60.98 in 37 minutes - 5% of premium is a
    0.13% underlying move, so every position was dead on submission

Six exit configurations were swept across the recorded tape looking for
the problem, and the answer was never in the exit ladder at all. The
stop had been mis-scaled by a factor of ~35 the entire time.
"""

import unittest
from decimal import Decimal

from webull_bot.trading.guards.price_sanity import (
    option_stop_implied_underlying_move,
    option_stop_survives_noise,
)

STOP = Decimal("0.10")
FLOOR = Decimal("0.005")


class ImpliedUnderlyingMoveTests(unittest.TestCase):
    def test_the_live_pfe_contract(self):
        """premium 0.50, delta 0.6187, PFE 28.21 -> 0.29%."""
        implied = option_stop_implied_underlying_move(
            Decimal("0.50"), Decimal("0.6187"), Decimal("28.21"), STOP
        )
        self.assertAlmostEqual(
            float(implied * 100), 0.286, places=2
        )

    def test_a_tighter_stop_implies_an_even_smaller_move(self):
        """Why 5% was catastrophic rather than merely tight."""
        ten = option_stop_implied_underlying_move(
            Decimal("0.50"), Decimal("0.6187"), Decimal("28.21"), STOP
        )
        five = option_stop_implied_underlying_move(
            Decimal("0.50"), Decimal("0.6187"), Decimal("28.21"),
            Decimal("0.05"),
        )
        self.assertLess(five, ten)
        self.assertLess(five * 100, Decimal("0.15"))

    def test_lower_delta_implies_a_larger_move_at_equal_premium(self):
        """Counter-intuitive but correct, and worth pinning: at a FIXED
        premium a thinner delta needs a bigger underlying move to lose
        the same percentage. The reason far-OTM contracts are still bad
        is that their premium is tiny, not their delta alone.
        """
        thin = option_stop_implied_underlying_move(
            Decimal("0.50"), Decimal("0.08"), Decimal("764"), STOP
        )
        fat = option_stop_implied_underlying_move(
            Decimal("0.50"), Decimal("0.62"), Decimal("764"), STOP
        )
        self.assertGreater(thin, fat)

    def test_unusable_inputs_return_none(self):
        for args in (
            (None, Decimal("0.5"), Decimal("28"), STOP),
            (Decimal("0.5"), None, Decimal("28"), STOP),
            (Decimal("0.5"), Decimal("0.5"), None, STOP),
            (Decimal("0.5"), Decimal("0"), Decimal("28"), STOP),
            (Decimal("0"), Decimal("0.5"), Decimal("28"), STOP),
        ):
            with self.subTest(args=args):
                self.assertIsNone(option_stop_implied_underlying_move(*args))


class StopSurvivesNoiseTests(unittest.TestCase):
    def _ok(self, premium, delta, spot, stop=STOP, floor=FLOOR):
        return option_stop_survives_noise(
            Decimal(premium), Decimal(delta), Decimal(spot), stop, floor
        )

    def test_the_live_pfe_call_is_refused(self):
        """The trade that cost $7.07. 0.29% against a 0.5% floor."""
        self.assertFalse(self._ok("0.50", "0.6187", "28.21"))

    def test_the_five_percent_stop_is_refused_even_harder(self):
        self.assertFalse(
            self._ok("0.50", "0.6187", "28.21", stop=Decimal("0.05"))
        )

    def test_scaling_the_stop_to_the_instrument_makes_it_pass(self):
        """The gate is self-correcting rather than a permanent block. At
        ~35% of premium the same PFE contract represents a ~1% move in
        PFE, which is a real adverse move rather than noise.
        """
        self.assertTrue(
            self._ok("0.50", "0.6187", "28.21", stop=Decimal("0.35"))
        )

    def test_the_far_otm_spy_contracts_are_refused(self):
        """Measured the same day: delta 0.04-0.08 on SPY at 764."""
        self.assertFalse(self._ok("0.60", "0.0818", "764.275"))
        self.assertFalse(self._ok("0.57", "0.0424", "764.275"))

    def test_the_expensive_near_the_money_contracts_are_refused_too(self):
        """Important: raising capital alone does not fix this. The
        cheapest contracts clearing BOTH the delta floor and the hurdle
        gate still fail here at a 10% stop, which is why the conclusion
        is 'the stop is wrong', not 'the account is too small'.
        """
        # IWM261008P00277000  1.74 premium, delta 0.34, IWM 280.22
        self.assertFalse(self._ok("1.74", "0.34", "280.22"))
        # SPY261016P00755000  4.53 premium, delta 0.32, SPY 764.275
        self.assertFalse(self._ok("4.53", "0.32", "764.275"))

    def test_it_fails_open_on_missing_data(self):
        """Matches every other gate here. A gate that rejected on absent
        greeks would silently halt option trading whenever the snapshot
        omitted delta - which it does on some accounts.
        """
        self.assertTrue(
            option_stop_survives_noise(
                Decimal("0.50"), None, Decimal("28.21"), STOP, FLOOR
            )
        )
        self.assertTrue(
            option_stop_survives_noise(
                Decimal("0.50"), Decimal("0.62"), None, STOP, FLOOR
            )
        )

    def test_a_zero_floor_disables_the_gate(self):
        self.assertTrue(
            self._ok("0.50", "0.6187", "28.21", floor=Decimal("0"))
        )


if __name__ == "__main__":
    unittest.main()
