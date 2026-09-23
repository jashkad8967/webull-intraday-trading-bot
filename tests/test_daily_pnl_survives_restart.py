import json
import logging
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from webull_bot.daily_pnl import DailyPnlTracker

TZ = ZoneInfo("America/Chicago")


class DailyPnlSurvivesRestartTests(unittest.TestCase):
    """universe_resolution_body clears the day's realized totals and
    re-arms the daily-loss circuit breaker, keyed off resolved_date -
    which is in-memory and None after every restart. So every boot
    looked like a new trading day.

    Live 2026-09-23: a -$52.14 realized loss was erased and the
    breaker re-armed roughly eight times across one session's deploys.
    The breaker could never have tripped however badly the day went,
    and persisting this file to disk was pointless while the reset ran
    on every start.
    """

    def _tracker(self, payload=None):
        d = Path(tempfile.mkdtemp())
        path = d / "daily_pnl.json"
        if payload is not None:
            path.write_text(json.dumps(payload), encoding="utf-8")
        return DailyPnlTracker(str(path), TZ, logging.getLogger("t"))

    def _today(self):
        return datetime.now(TZ).date().isoformat()

    def test_a_restart_the_same_day_is_not_treated_as_a_new_day(self):
        t = self._tracker(
            {"date": self._today(), "realized_pnl": "-52.14", "realized_loss": "52.14"}
        )
        self.assertTrue(
            t.belongs_to_today(),
            "a mid-session restart must NOT clear the day's totals",
        )
        self.assertEqual(t.realized_pnl, Decimal("-52.14"))
        self.assertEqual(t.realized_loss, Decimal("52.14"))

    def test_yesterdays_file_is_treated_as_a_new_day(self):
        t = self._tracker(
            {"date": "2020-01-01", "realized_pnl": "-99", "realized_loss": "99"}
        )
        self.assertFalse(t.belongs_to_today())
        self.assertEqual(t.realized_pnl, Decimal("0"))

    def test_no_file_at_all_is_a_new_day(self):
        t = self._tracker()
        self.assertFalse(t.belongs_to_today())

    def test_a_corrupt_file_is_a_new_day_not_a_crash(self):
        d = Path(tempfile.mkdtemp())
        path = d / "daily_pnl.json"
        path.write_text("{not json", encoding="utf-8")
        t = DailyPnlTracker(str(path), TZ, logging.getLogger("t"))
        self.assertFalse(t.belongs_to_today())
        self.assertEqual(t.realized_pnl, Decimal("0"))

    def test_the_reset_is_gated_on_a_real_new_day(self):
        """The guard the reset actually uses, asserted directly."""
        import inspect

        from webull_bot.trading.universe import universe_resolution_body as body

        src = inspect.getsource(body)
        self.assertIn(
            "if not self.daily_pnl.belongs_to_today():",
            src,
            "the daily reset must be gated, or every restart wipes the "
            "running loss and re-arms the circuit breaker",
        )


if __name__ == "__main__":
    unittest.main()
