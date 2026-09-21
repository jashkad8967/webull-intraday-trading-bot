import statistics
import time
import unittest
import unittest.mock
from decimal import Decimal

from webull_bot.pairs import (
    PAIRS_ENTRY_Z,
    PAIRS_EXIT_Z,
    PAIRS_MAX_HOLD_MINUTES,
    PAIRS_MIN_SAMPLES,
    PAIRS_STOP_Z,
    PairsStrategy,
)


class PairsStrategyTests(unittest.TestCase):
    @staticmethod
    def _seeded(values):
        strat = PairsStrategy()
        pair = ("A", "B")
        strat._spread_history[pair].extend(values)
        return strat, pair

    @staticmethod
    def _expected_z(values):
        mean = statistics.mean(values)
        stdev = statistics.pstdev(values)
        return Decimal(str((values[-1] - mean) / stdev))

    def test_no_data_below_minimum_samples(self):
        strat, pair = self._seeded([0.001] * (PAIRS_MIN_SAMPLES - 1))
        decision = strat.decision(pair, is_open=False)
        self.assertEqual(decision.action, "NO_DATA")

    def test_no_data_when_history_is_perfectly_flat(self):
        # stdev == 0 must not raise a division error - just no signal.
        strat, pair = self._seeded([0.01] * (PAIRS_MIN_SAMPLES + 10))
        decision = strat.decision(pair, is_open=False)
        self.assertEqual(decision.action, "NO_DATA")
        self.assertIsNone(decision.z_score)

    def test_enters_long_b_short_a_when_a_rich(self):
        values = [0.0] * 40 + [0.05]
        strat, pair = self._seeded(values)
        expected_z = self._expected_z(values)
        self.assertGreaterEqual(expected_z, PAIRS_ENTRY_Z)
        decision = strat.decision(pair, is_open=False)
        self.assertEqual(decision.action, "ENTER_LONG_B_SHORT_A")
        self.assertAlmostEqual(
            float(decision.z_score), float(expected_z), places=9
        )

    def test_enters_long_a_short_b_when_b_rich(self):
        values = [0.0] * 40 + [-0.05]
        strat, pair = self._seeded(values)
        decision = strat.decision(pair, is_open=False)
        self.assertEqual(decision.action, "ENTER_LONG_A_SHORT_B")

    def test_no_entry_when_spread_within_normal_range(self):
        values = [0.001, -0.001] * 20 + [0.0005]
        strat, pair = self._seeded(values)
        expected_z = self._expected_z(values)
        self.assertLess(abs(expected_z), PAIRS_ENTRY_Z)
        decision = strat.decision(pair, is_open=False)
        self.assertEqual(decision.action, "NO_DATA")

    def test_stop_when_open_and_spread_keeps_diverging(self):
        values = [0.0] * 40 + [0.5]
        strat, pair = self._seeded(values)
        expected_z = self._expected_z(values)
        self.assertGreaterEqual(abs(expected_z), PAIRS_STOP_Z)
        strat.mark_entered(pair)
        decision = strat.decision(pair, is_open=True)
        self.assertEqual(decision.action, "STOP")

    def test_unwind_when_open_and_spread_reverted(self):
        # A volatile-then-flat spread: 60 samples oscillating +/-0.002 (a
        # real, nonzero stdev to revert from), then a final sample back at
        # the set's own mean - a genuine "it came back" shape, not just a
        # quiet history that never moved.
        values = [0.002, -0.002] * 30 + [0.0]
        strat, pair = self._seeded(values)
        expected_z = self._expected_z(values)
        self.assertLessEqual(abs(expected_z), PAIRS_EXIT_Z)
        strat.mark_entered(pair)
        decision = strat.decision(pair, is_open=True)
        self.assertEqual(decision.action, "UNWIND")

    def test_unwind_after_max_hold_time_regardless_of_z(self):
        # A moderate z (between PAIRS_EXIT_Z and PAIRS_STOP_Z) that would
        # otherwise just HOLD - only the max-hold override should move it.
        values = [0.002, -0.002] * 20 + [0.004]
        strat, pair = self._seeded(values)
        expected_z = self._expected_z(values)
        self.assertGreater(abs(expected_z), PAIRS_EXIT_Z)
        self.assertLess(abs(expected_z), PAIRS_STOP_Z)
        strat.mark_entered(pair)
        without_override = strat.decision(pair, is_open=True)
        self.assertEqual(without_override.action, "HOLD")
        strat._entered_at[pair] = (
            time.monotonic() - (PAIRS_MAX_HOLD_MINUTES + 1) * 60
        )
        decision = strat.decision(pair, is_open=True)
        self.assertEqual(decision.action, "UNWIND")
        self.assertIn("max hold", decision.reason)

    def test_mark_exited_clears_entry_time(self):
        strat, pair = self._seeded([0.0] * PAIRS_MIN_SAMPLES)
        strat.mark_entered(pair)
        strat.mark_exited(pair)
        self.assertNotIn(pair, strat._entered_at)
