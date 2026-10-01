"""Recorded P&L must be reconciled against the account's own balance.

The balance is the only number that cannot lie. trade_history is written
when an order is SUBMITTED, not when it fills, so a duplicate or rejected
SELL still writes a PROFIT while a rejected order never writes a loss -
the error is therefore always in the flattering direction.

Live 2026-10-01:

    recorded P&L           -$1.20
    actual balance change  -$4.16   (84.70 -> 80.54)
    gap                    +$2.96

Two sell orders raced on one PFE contract (the stale-position race). The
first sold it, the second hit an already-flat position, and both wrote
+$2.93. The day looked $3 better than it was.

This matters beyond the $3: every measurement taken from trade_history is
optimistic by an unknown amount, including the -$44.84 four-session
figure used to reason about whether the strategy worked at all. The true
number was -$45.28.
"""

import importlib.util
import json
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent


def _load():
    path = REPO / "scripts" / "reconcile_pnl.py"
    spec = importlib.util.spec_from_file_location("reconcile_pnl", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["reconcile_pnl"] = module
    spec.loader.exec_module(module)
    return module


class ReconcilePnlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def _volume(self, trades, balances):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "conf").mkdir(parents=True)
        (root / "conf" / "trade_history.json").write_text(
            json.dumps({"trades": trades}), encoding="utf-8"
        )
        (root / "status.json").write_text(
            json.dumps({"balance_history": balances}), encoding="utf-8"
        )
        return root

    def _today_at(self, hour, minute):
        return datetime.combine(
            date.today(), datetime.min.time()
        ).replace(hour=hour, minute=minute).timestamp()

    def test_the_live_phantom_record_is_detected(self):
        """The real 2026-10-01 sequence: both PFE closes recorded."""
        trades = [
            {"time": self._today_at(10, 33), "symbol": "MEDS",
             "instrument_type": "STOCK", "action": "PROFIT", "pnl": "0.13"},
            {"time": self._today_at(12, 54), "symbol": "PFE...C00028000",
             "instrument_type": "OPTION", "action": "STOP", "pnl": "-7.07"},
            {"time": self._today_at(13, 1), "symbol": "PFE...P00028500",
             "instrument_type": "OPTION", "action": "PROFIT", "pnl": "2.93"},
            {"time": self._today_at(13, 10), "symbol": "PFE...P00028500",
             "instrument_type": "OPTION", "action": "PROFIT", "pnl": "2.93"},
        ]
        root = self._volume(
            trades,
            [{"time": 1, "balance": "84.70"}, {"time": 2, "balance": "80.54"}],
        )
        recorded, rows = self.mod._recorded_pnl(root, date.today())
        actual, opening, closing = self.mod._actual_change(root)
        self.assertEqual(len(rows), 4)
        self.assertEqual(recorded, Decimal("-1.08"))
        self.assertEqual(actual, Decimal("-4.16"))
        # The gap is positive: records flatter than reality.
        self.assertGreater(recorded - actual, self.mod.TOLERANCE)

    def test_a_clean_session_reconciles(self):
        trades = [
            {"time": self._today_at(10, 0), "symbol": "AAA",
             "instrument_type": "STOCK", "action": "PROFIT", "pnl": "1.00"},
            {"time": self._today_at(11, 0), "symbol": "BBB",
             "instrument_type": "STOCK", "action": "STOP", "pnl": "-0.40"},
        ]
        root = self._volume(
            trades,
            [{"time": 1, "balance": "100.00"}, {"time": 2, "balance": "100.60"}],
        )
        recorded, _ = self.mod._recorded_pnl(root, date.today())
        actual, _, _ = self.mod._actual_change(root)
        self.assertEqual(recorded, Decimal("0.60"))
        self.assertEqual(actual, Decimal("0.60"))
        self.assertLessEqual(abs(recorded - actual), self.mod.TOLERANCE)

    def test_open_records_without_pnl_are_ignored(self):
        """A BUY has no realized P&L and must not count as a round trip."""
        trades = [
            {"time": self._today_at(10, 0), "symbol": "AAA",
             "instrument_type": "STOCK", "action": "BUY", "pnl": None},
            {"time": self._today_at(10, 5), "symbol": "AAA",
             "instrument_type": "STOCK", "action": "PROFIT", "pnl": "0.25"},
        ]
        root = self._volume(
            trades,
            [{"time": 1, "balance": "10.00"}, {"time": 2, "balance": "10.25"}],
        )
        recorded, rows = self.mod._recorded_pnl(root, date.today())
        self.assertEqual(len(rows), 1)
        self.assertEqual(recorded, Decimal("0.25"))

    def test_too_few_balance_samples_refuses_to_reconcile(self):
        """Better to report 'cannot reconcile' than to silently trust
        records that are known to be written before fills.
        """
        root = self._volume([], [{"time": 1, "balance": "10.00"}])
        actual, opening, closing = self.mod._actual_change(root)
        self.assertIsNone(actual)

    def test_another_day_is_not_counted(self):
        trades = [
            {"time": datetime(2020, 1, 1, 10, 0).timestamp(), "symbol": "OLD",
             "instrument_type": "STOCK", "action": "PROFIT", "pnl": "99.00"},
        ]
        root = self._volume(
            trades,
            [{"time": 1, "balance": "10.00"}, {"time": 2, "balance": "10.00"}],
        )
        recorded, rows = self.mod._recorded_pnl(root, date.today())
        self.assertEqual(rows, [])
        self.assertEqual(recorded, 0)


if __name__ == "__main__":
    unittest.main()
