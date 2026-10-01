"""Where price sits in today's range - discretionary judgment, encoded.

On 2026-10-01 the bot's own gates produced one losing option trade while
two manual share entries both closed green (NVDA +$0.05, +$0.01). The
criterion used was not in the bot at all: position inside the day's
high/low range.

Real numbers from that session, run through this gate below:

    NVDA   +1.32%  range  87%  -> taken, closed green
    NVDA   +1.52%  range  89%  -> taken, closed green
    PLTR   +1.92%  range  78%  -> would take
    ACN   +17.95%  range  14%  -> REFUSED
    GOOGL  -1.43%  range  21%  -> REFUSED

ACN and GOOGL are the whole point. ACN gapped +18%, sat in the top four
by the bot's own priority_score, and had already faded 227.58 -> 216.28.
GOOGL scored 29.6, second highest, after opening at 351.44 and selling
down to 339.16. Gap and score - all the existing gates measure - said buy
both. Range position said they were failing, and it was right.

NOT the same as range_ratio, which already existed: that is
(high - low) / price, how WIDE the range is, used only to scale the stop.
A name can be extremely volatile and sitting on its low.

The evidence is one session and five names, so the default is 0.55 -
"upper half of the day's range", the weakest defensible form of "not
currently fading" rather than a number fitted to these trades. The
margin is what carries it: lowest accepted 78%, highest refused 21%.
"""

import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from webull_bot.strategy_logic.market_state.range_position import (
    range_position,
    range_position_supports_long,
)

REPO = Path(__file__).resolve().parent.parent
MIN = Decimal("0.55")


def _state(metrics, prices):
    return SimpleNamespace(metrics=metrics, prices=prices)


class RangePositionTests(unittest.TestCase):
    def test_the_live_session_numbers(self):
        cases = {
            # symbol: (high, low, price, expected_pct, should_allow_long)
            "NVDA": (231.91, 228.16, 231.48, 88.5, True),
            "ACN": (227.58, 214.50, 216.28, 13.6, False),
            "GOOGL": (353.22, 335.51, 339.16, 20.6, False),
            "PLTR": (191.80, 186.60, 190.63, 77.5, True),
        }
        for symbol, (high, low, price, pct, allow) in cases.items():
            state = _state({symbol: {"high": high, "low": low}}, {symbol: price})
            with self.subTest(symbol=symbol):
                position = range_position(state, symbol)
                self.assertAlmostEqual(float(position) * 100, pct, places=0)
                self.assertEqual(
                    range_position_supports_long(state, symbol, MIN), allow
                )

    def test_on_the_high_and_on_the_low(self):
        state = _state({"X": {"high": 10, "low": 5}}, {"X": 10})
        self.assertEqual(range_position(state, "X"), Decimal("1"))
        state = _state({"X": {"high": 10, "low": 5}}, {"X": 5})
        self.assertEqual(range_position(state, "X"), Decimal("0"))

    def test_a_print_outside_the_recorded_range_is_clamped(self):
        """A quote can print fractionally beyond the high/low captured in
        the same snapshot; that must not yield a position above 1.
        """
        state = _state({"X": {"high": 10, "low": 5}}, {"X": 10.5})
        self.assertEqual(range_position(state, "X"), Decimal("1"))
        state = _state({"X": {"high": 10, "low": 5}}, {"X": 4.5})
        self.assertEqual(range_position(state, "X"), Decimal("0"))

    def test_an_unusable_range_returns_none_and_falls_open(self):
        """No snapshot, or a high equal to the low (a symbol that has not
        moved). Falls open like every other best-effort gate here - a gate
        that rejected on missing data would silently stop all trading
        whenever the quote feed hiccuped.
        """
        for metrics, prices in (
            ({}, {"X": 10}),
            ({"X": {"high": 10, "low": 10}}, {"X": 10}),
            ({"X": {"high": 10, "low": 5}}, {}),
            ({"X": {"high": None, "low": 5}}, {"X": 10}),
        ):
            state = _state(metrics, prices)
            with self.subTest(metrics=metrics):
                self.assertIsNone(range_position(state, "X"))
                self.assertTrue(range_position_supports_long(state, "X", MIN))

    def test_a_zero_threshold_disables_the_gate(self):
        state = _state({"X": {"high": 10, "low": 5}}, {"X": 5})
        self.assertFalse(range_position_supports_long(state, "X", MIN))
        self.assertTrue(
            range_position_supports_long(state, "X", Decimal("0"))
        )


class EveryEntryPathIsGatedTests(unittest.TestCase):
    """Applied at three of four entry paths, this is the same gap with a
    smaller blast radius - the mistake already made once with
    stock_entry_symbol_allowed.
    """

    PATHS = (
        "src/webull_bot/trading/stocks/stock_symbol_processing.py",
        "src/webull_bot/trading/stocks/stock_symbol_volatility_scalp.py",
    )

    def test_every_symbol_gate_is_paired_with_the_range_gate(self):
        for relative in self.PATHS:
            text = (REPO / relative).read_text(encoding="utf-8")
            symbol_gates = text.count("self.stock_entry_symbol_allowed(symbol)")
            range_gates = text.count("range_position_supports_long(")
            self.assertEqual(
                symbol_gates, range_gates,
                f"{relative}: {symbol_gates} symbol gate(s) but "
                f"{range_gates} range gate(s) - an entry path missing the "
                f"range check can still buy a failing gap",
            )
            self.assertGreater(range_gates, 0, f"{relative}: no range gate")

    def test_the_shipped_default_is_the_upper_half(self):
        import os

        for key in ("WEBULL_APP_KEY", "WEBULL_APP_SECRET", "ACCOUNT_ID"):
            os.environ.setdefault(key, "x")
        from webull_bot.config import Settings

        self.assertEqual(
            Settings().stock_min_entry_range_position, Decimal("0.55")
        )


if __name__ == "__main__":
    unittest.main()
