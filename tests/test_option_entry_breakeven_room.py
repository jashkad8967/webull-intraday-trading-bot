"""The round-trip cost gate.

Measured 2026-10-01 from the recorded quote tape, after four straight
losing sessions in which every mechanical defect had already been found
and fixed (entry overpay, the max(target, marketable) exit, the quote
sanity bound, four key-mismatch guards). The strategy still lost, and
the tape said why:

  - mean breakeven hurdle 8.1% against a 10% stop -> 1.9% of room
  - 9 of 10 positions NEVER traded above cost+fee on the bid, so no
    exit rule could have won them
  - 3 of 10 had a hurdle at or BEYOND the stop: guaranteed losses the
    moment they filled
  - sweeping the trail arm/giveback and the take-profit across six
    configurations moved the total between -$44 and -$54 and never
    once into profit

The losing was not in the exit logic that was being tuned. It was that
buying at the mid and selling into the bid costs half a spread plus the
exit fee, and on the cheap contracts a small account can afford, the
fixed $0.05 option tick makes that cost most of the risk budget.

This gate is deliberately separate from option_entry_spread_ok: that
one asks "can this be liquidated?" and its bound is reused on the EXIT
path (stall_position_boost), so tightening it would break exits on
positions already held.
"""

import unittest
from decimal import Decimal

from webull_bot.trading.guards.price_sanity import (
    option_entry_breakeven_hurdle,
    option_entry_breakeven_room_ok,
)

FEE = Decimal("0.02")  # option_sell_fee_per_contract 2.00 / 100
STOP = Decimal("0.10")


def _room_ok(bid, ask, fraction="1.0", stop=STOP):
    return option_entry_breakeven_room_ok(
        Decimal(bid), Decimal(ask), stop, FEE, Decimal(fraction)
    )


class BreakevenHurdleTests(unittest.TestCase):
    def test_the_hurdle_is_half_the_spread_plus_the_fee(self):
        # $1.00/$1.10: mid 1.05, so the bid must gain 0.05 + 0.02 = 0.07
        # on a 1.00 base = 7%.
        hurdle = option_entry_breakeven_hurdle(
            Decimal("1.00"), Decimal("1.10"), FEE
        )
        self.assertEqual(hurdle.quantize(Decimal("0.0001")), Decimal("0.0700"))

    def test_a_tighter_spread_costs_less(self):
        wide = option_entry_breakeven_hurdle(
            Decimal("1.00"), Decimal("1.20"), FEE
        )
        tight = option_entry_breakeven_hurdle(
            Decimal("1.00"), Decimal("1.05"), FEE
        )
        self.assertLess(tight, wide)

    def test_the_same_tick_costs_more_on_a_cheaper_contract(self):
        """The core finding. The $0.05 option tick is fixed in CENTS,
        so as a share of premium it is brutal on cheap contracts - and
        cheap contracts are all a small account can afford. This is why
        the gate is on the hurdle and not on the spread percentage.
        """
        cheap = option_entry_breakeven_hurdle(
            Decimal("0.50"), Decimal("0.55"), FEE
        )
        rich = option_entry_breakeven_hurdle(
            Decimal("2.00"), Decimal("2.05"), FEE
        )
        self.assertGreater(cheap, rich)
        # One tick either side, yet the cheap one eats most of a 10%
        # stop budget while the rich one barely registers.
        self.assertGreater(cheap, Decimal("0.08"))
        self.assertLess(rich, Decimal("0.03"))

    def test_an_unusable_quote_returns_none(self):
        self.assertIsNone(option_entry_breakeven_hurdle(None, None, FEE))
        self.assertIsNone(
            option_entry_breakeven_hurdle(Decimal("0"), Decimal("1"), FEE)
        )
        # Crossed quote: ask below bid is corrupt, not tradeable.
        self.assertIsNone(
            option_entry_breakeven_hurdle(Decimal("1.10"), Decimal("1.00"), FEE)
        )


class BreakevenRoomGateTests(unittest.TestCase):
    def test_a_guaranteed_loser_is_refused_at_the_default(self):
        """RIOT261009C00024500, 2026-09-28 11:40, from the real tape:
        $0.47/$0.54. Hurdle 11.7% against a 10% stop - breakeven sits
        PAST the stop, so the trade cannot win at any point in its
        life. It was taken twice that session.
        """
        self.assertFalse(_room_ok("0.47", "0.54"))

    def test_the_default_admits_a_trade_with_room_left(self):
        """UBER261009C00070000, 2026-09-28 08:59: $0.95/$1.00, hurdle
        4.7%, 5.3% of room. Thin, but not arithmetically doomed.
        """
        self.assertTrue(_room_ok("0.95", "1.00"))

    def test_the_default_only_rejects_hurdles_at_or_past_the_stop(self):
        """Default 1.0 is the judgment-free setting: it refuses trades
        whose breakeven is at or beyond their own stop and nothing
        else. Anything stricter is a capital/strategy decision.
        """
        # Hurdle just under the stop: admitted.
        self.assertTrue(_room_ok("1.00", "1.14"))   # (0.07+0.02)/1 = 9%
        # Hurdle just over it: refused.
        self.assertFalse(_room_ok("1.00", "1.18"))  # (0.09+0.02)/1 = 11%

    def test_a_stricter_fraction_demands_real_room(self):
        """At 0.5 friction may consume at most half the risk budget.
        On the recorded tape this rejects 9 of 10 candidates - the gate
        reporting that a sub-$100 account cannot reach contracts whose
        spread is small relative to premium, not malfunctioning.

        UBER261009C00071000, 2026-09-28 09:05: $0.70/$0.78, hurdle
        8.6%. Admitted by the default, refused once friction is capped
        at half the budget.
        """
        self.assertTrue(_room_ok("0.70", "0.78"))
        self.assertFalse(_room_ok("0.70", "0.78", fraction="0.5"))

    def test_a_wider_stop_creates_room_on_the_same_quote(self):
        """The two numbers are only meaningful against each other. This
        is the arithmetic that made the 5% stop catastrophic: at a 5%
        stop every one of these quotes is already past its own stop at
        the moment of entry, which is exactly what the live log showed
        - four positions exiting at held=0s.
        """
        quote = ("0.95", "1.00")
        self.assertFalse(_room_ok(*quote, stop=Decimal("0.04")))
        self.assertTrue(_room_ok(*quote, stop=Decimal("0.10")))

    def test_a_five_percent_stop_refuses_nearly_the_whole_recorded_tape(self):
        """Regression for the single most expensive mistake in this
        project: a 5% stop shipped on expectancy arithmetic that held
        win rate constant while changing the thing that sets it. It
        cost $60.98 in 37 minutes.

        Nine of the ten entry quotes actually recorded that week are
        past their own stop at the moment of entry under a 5% stop -
        guaranteed instant losses, which is precisely what the live log
        showed (positions exiting at held=0s). The tenth clears it by
        0.3%, which is why the outcome was a near-total wipeout rather
        than a literal one. This gate would have refused all nine.
        """
        recorded = [
            ("1.07", "1.16"), ("0.61", "0.66"), ("0.70", "0.78"),
            ("1.09", "1.17"), ("0.48", "0.55"), ("0.47", "0.54"),
            ("0.97", "1.05"), ("0.71", "0.84"), ("0.63", "0.68"),
        ]
        for bid, ask in recorded:
            with self.subTest(bid=bid, ask=ask):
                self.assertFalse(
                    _room_ok(bid, ask, stop=Decimal("0.05")),
                    f"{bid}/{ask} should be refused under a 5% stop",
                )
        # The lone survivor, kept explicit so the margin is not
        # mistaken for comfort: 4.7% hurdle, 0.3% of room.
        self.assertTrue(_room_ok("0.95", "1.00", stop=Decimal("0.05")))

    def test_it_fails_open_on_a_missing_quote(self):
        """Matches the fail-open convention of every other gate here.
        A gate that rejects on absent data would silently halt trading
        whenever the quote feed hiccuped.
        """
        self.assertTrue(
            option_entry_breakeven_room_ok(None, None, STOP, FEE, Decimal("1"))
        )

    def test_it_fails_open_on_a_nonsensical_stop(self):
        self.assertTrue(
            option_entry_breakeven_room_ok(
                Decimal("0.47"), Decimal("0.54"), Decimal("0"), FEE,
                Decimal("1"),
            )
        )


if __name__ == "__main__":
    unittest.main()
