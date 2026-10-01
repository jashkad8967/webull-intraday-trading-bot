"""State writes must survive a concurrent reader on Windows.

os.replace is atomic on both platforms but they differ where it counts:
on POSIX it succeeds while another process holds the target open, and on
Windows it raises PermissionError (WinError 5) until that handle closes.

Live 2026-10-01, minutes after the dashboard started on the same Windows
machine as the trader:

    PROTECT| position-protection cycle failed | [WinError 5]
    Access is denied: 'status.tmp' -> 'status.json'

The bot rewrites status.json about every poll interval and the dashboard
reads it on every request. On the Linux host this could not happen, which
is why all seven state writers were built without a retry.

Two separate defects, fixed together:

  1. The collision was reported as "position-protection cycle failed",
     the most alarming line this loop can emit, when nothing protective
     had failed - the status write is last, after every exit path. The
     cosmetic write now sits outside that try block entirely, because a
     dashboard write must never be able to impersonate a broken stop.
  2. The replace itself now retries briefly. A reader holds the file for
     microseconds, so short sleeps clear essentially every collision,
     while a lock would put the trading loop behind a dashboard request.
"""

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from webull_bot.atomic_replace import ATTEMPTS, atomic_replace


class AtomicReplaceTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.target = self.dir / "status.json"
        self.target.write_text("old", encoding="utf-8")

    def _temp(self, content="new"):
        path = self.dir / "status.tmp"
        path.write_text(content, encoding="utf-8")
        return path

    def test_it_replaces_normally(self):
        atomic_replace(self._temp(), self.target)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "new")

    def test_it_retries_a_windows_permission_error_and_succeeds(self):
        """The live failure mode: the first attempt collides with a
        reader, a later one wins.
        """
        real = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError(13, "Access is denied")
            return real(src, dst)

        with mock.patch("webull_bot.atomic_replace.os.replace", flaky):
            atomic_replace(self._temp(), self.target)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "new")

    def test_it_raises_after_exhausting_retries(self):
        """A caller that treats a failed write as significant must still
        be told - this makes the write resilient, not silent.
        """
        def always(src, dst):
            raise PermissionError(13, "Access is denied")

        with mock.patch("webull_bot.atomic_replace.os.replace", always):
            with self.assertRaises(PermissionError):
                atomic_replace(self._temp(), self.target)

    def test_a_non_contention_error_is_not_retried(self):
        """Waiting cannot fix a missing temp file or a cross-device move,
        so those must fail immediately rather than sleeping first.
        """
        calls = {"n": 0}

        def wrong_device(src, dst):
            calls["n"] += 1
            raise OSError(18, "Invalid cross-device link")

        with mock.patch("webull_bot.atomic_replace.os.replace", wrong_device):
            with self.assertRaises(OSError):
                atomic_replace(self._temp(), self.target)
        self.assertEqual(calls["n"], 1, "retried an error waiting cannot fix")

    def test_the_retry_budget_is_bounded(self):
        """This runs inside a 0.5s trading loop; an unbounded retry would
        stall exit management behind a dashboard request.
        """
        calls = {"n": 0}

        def always(src, dst):
            calls["n"] += 1
            raise PermissionError(13, "Access is denied")

        with mock.patch("webull_bot.atomic_replace.os.replace", always):
            with self.assertRaises(PermissionError):
                atomic_replace(self._temp(), self.target)
        self.assertEqual(calls["n"], ATTEMPTS)


class EveryStateWriterUsesItTests(unittest.TestCase):
    """Seven writers do an atomic replace, and my own report scripts read
    trade_history.json and status.json while the bot runs - so every one
    of them can collide, and patching only some is the three-of-four
    mistake this session already made twice.
    """

    WRITERS = (
        "src/webull_bot/daily_pnl/__init__.py",
        "src/webull_bot/invalid_symbols/__init__.py",
        "src/webull_bot/option_contracts_state/__init__.py",
        "src/webull_bot/position_open_times/__init__.py",
        "src/webull_bot/status/__init__.py",
        "src/webull_bot/wash_sale/__init__.py",
    )

    def test_no_writer_calls_path_replace_directly(self):
        repo = Path(__file__).resolve().parent.parent
        for relative in self.WRITERS:
            text = (repo / relative).read_text(encoding="utf-8")
            self.assertNotIn(
                "temporary.replace(", text,
                f"{relative} still replaces directly and will raise "
                f"WinError 5 against a concurrent reader",
            )
            self.assertIn(
                "atomic_replace(", text,
                f"{relative} does not use the retrying replace",
            )

    def test_the_status_write_is_outside_the_protection_try(self):
        """It must not be able to report itself as a protection failure."""
        repo = Path(__file__).resolve().parent.parent
        text = (repo / "src/webull_bot/trading/orders/position_protection_loop.py"
                ).read_text(encoding="utf-8")
        self.assertIn("status snapshot write failed", text)
        # The cosmetic write comes AFTER the protective except block.
        self.assertLess(
            text.index("position-protection cycle failed"),
            text.index("self.write_status_snapshot"),
            "the status write is still inside the protection try block",
        )


if __name__ == "__main__":
    unittest.main()
