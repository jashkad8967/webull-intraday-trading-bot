"""Daily log files must not grow without bound.

Live outage 2026-09-23: the deploy host filled to 99% (124M free on an
8.7G root) and wedged Docker - the daemon reported "active" while
`docker ps` hung and `docker ps -a` returned nothing, so the trading
bot was DOWN mid-session with four open option positions and no stop,
no profit-lock and no EOD close. Stale Docker images were the bulk of
that (fixed in deploy/gcp/deploy.sh), but this handler wrote one log
file per day forever with no retention at all - the same unbounded
growth bug inside the bot's own house, on a box that also has to hold
the images, the release trees and the state files.
"""

import logging
import tempfile
import unittest
from datetime import date
from pathlib import Path

from webull_bot.daily_logging import DatedDailyFileHandler


def _seed(directory: Path, days: list[str]) -> None:
    for day in days:
        year, month, _ = day.split("-")
        path = directory / year / month / f"{day}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("old\n", encoding="utf-8")


class DailyLogRetentionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _handler(self, retention_days=3):
        handler = DatedDailyFileHandler(
            str(self.directory), "America/Chicago", retention_days
        )
        self.addCleanup(handler.close)
        return handler

    def _emit(self, handler):
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler.emit(
            logging.LogRecord(
                "t", logging.INFO, __file__, 1, "hello", None, None
            )
        )

    def _remaining(self):
        return sorted(p.name for p in self.directory.glob("*/*/*.log"))

    def test_old_day_files_are_deleted_down_to_the_retention_window(self):
        _seed(
            self.directory,
            [
                "2026-09-01", "2026-09-02", "2026-09-03",
                "2026-09-04", "2026-09-05",
            ],
        )
        self._emit(self._handler(retention_days=3))
        remaining = self._remaining()
        # Today's file plus the two newest seeded ones.
        self.assertEqual(len(remaining), 3)
        today = f"{date.today():%Y-%m-%d}.log"
        self.assertIn(today, remaining)
        self.assertNotIn("2026-09-01.log", remaining)
        self.assertNotIn("2026-09-02.log", remaining)

    def test_todays_log_is_still_written(self):
        handler = self._handler(retention_days=1)
        self._emit(handler)
        today = self.directory / f"{date.today():%Y}" / f"{date.today():%m}"
        written = today / f"{date.today():%Y-%m-%d}.log"
        self.assertTrue(written.exists())
        self.assertIn("hello", written.read_text(encoding="utf-8"))

    def test_retention_of_zero_disables_pruning(self):
        _seed(self.directory, ["2026-01-01", "2026-01-02"])
        self._emit(self._handler(retention_days=0))
        self.assertIn("2026-01-01.log", self._remaining())

    def test_pruning_never_raises_when_the_directory_is_unreadable(self):
        """A logging handler must never take the trading process down."""
        handler = self._handler(retention_days=1)
        handler.directory = Path("/nonexistent/path/that/cannot/exist")
        handler._prune()  # must not raise

    def test_empty_month_directories_are_cleaned_up(self):
        _seed(self.directory, ["2026-01-01", "2026-09-04", "2026-09-05"])
        self._emit(self._handler(retention_days=2))
        self.assertFalse((self.directory / "2026" / "01").exists())


if __name__ == "__main__":
    unittest.main()
