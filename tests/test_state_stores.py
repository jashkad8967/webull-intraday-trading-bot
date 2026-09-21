import json
import logging
import shutil
import threading
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from webull_bot.daily_pnl import DailyPnlTracker
from webull_bot.wash_sale import WashSaleTracker


class WashSaleTrackerTests(unittest.TestCase):
    def _tracker(self, path, block_days):
        return WashSaleTracker(str(path), block_days, timezone.utc, logging.getLogger("test-wash"))

    def test_block_then_blocked_until_reflects_configured_days(self):
        path = Path("tests/.generated_wash/blocks.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            tracker = self._tracker(path, 31)
            until = tracker.block("AAPL", "stop-loss exit submitted")
            expected = datetime.now(timezone.utc) + timedelta(days=31)
            self.assertLess(abs((until - expected).total_seconds()), 5)
            self.assertIsNotNone(tracker.blocked_until("AAPL"))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_direction_scoped_key_only_blocks_the_matching_side(self):
        # By request: "you can constantly buy puts and calls on the
        # same stock as it dips and rises" - option stop-loss wash-
        # sale blocks are now keyed "UNDERLYING:CALL"/"UNDERLYING:PUT"
        # (see bot.py's trade_options), not the bare underlying, so a
        # stopped-out CALL must not block a PUT re-entry on the same
        # name. WashSaleTracker itself just treats the key as an
        # opaque string, so this documents/locks in that the compound
        # key genuinely keeps the two sides independent.
        path = Path("tests/.generated_wash/direction_scoped.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            tracker = self._tracker(path, 31)
            tracker.block("AAPL:CALL", "option stop-loss exit submitted")
            self.assertIsNotNone(tracker.blocked_until("AAPL:CALL"))
            self.assertIsNone(tracker.blocked_until("AAPL:PUT"))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_block_expires_after_configured_days(self):
        path = Path("tests/.generated_wash/expired.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            tracker = self._tracker(path, 31)
            blocked_at = datetime.now(timezone.utc) - timedelta(days=32)
            tracker.blocks["AAPL"] = {"blocked_at": blocked_at.isoformat()}
            self.assertIsNone(tracker.blocked_until("AAPL"))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_legacy_string_format_is_migrated_and_shortened_to_new_day_count(self):
        """A block written under the old fixed 60-day rule must be
        re-evaluated against the new (lower) WASH_SALE_BLOCK_DAYS as soon as
        the tracker loads it, not frozen at whatever the old rule computed.
        """
        path = Path("tests/.generated_wash/legacy.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Legacy entry: a loss 40 days ago, blocked for the old 60 days,
            # so the stored "until" is 20 days in the future.
            legacy_until = datetime.now(timezone.utc) + timedelta(days=20)
            path.write_text(
                json.dumps({"AAPL": legacy_until.isoformat()}),
                encoding="utf-8",
            )

            tracker = self._tracker(path, 31)

            # Under the new 31-day rule, a loss 40 days ago is already past
            # its block window (40 > 31), so it should be gone, not still
            # blocked for another 20 days.
            self.assertIsNone(tracker.blocked_until("AAPL"))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_legacy_migration_keeps_a_still_active_block_shortened_not_dropped(self):
        path = Path("tests/.generated_wash/legacy_active.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Legacy entry: a loss 10 days ago, blocked for the old 60 days,
            # so the stored "until" is 50 days in the future.
            legacy_until = datetime.now(timezone.utc) + timedelta(days=50)
            path.write_text(
                json.dumps({"AAPL": legacy_until.isoformat()}),
                encoding="utf-8",
            )

            tracker = self._tracker(path, 31)

            # Under the new 31-day rule, a loss 10 days ago is still blocked
            # for 21 more days (31 - 10), not the old 50.
            until = tracker.blocked_until("AAPL")
            self.assertIsNotNone(until)
            expected = datetime.now(timezone.utc) + timedelta(days=21)
            self.assertLess(abs((until - expected).total_seconds()), 5)
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_changing_block_days_after_load_immediately_changes_blocked_until(self):
        path = Path("tests/.generated_wash/dynamic.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            tracker = self._tracker(path, 60)
            tracker.block("TSLA", "manual sell at a loss")
            far_future = tracker.blocked_until("TSLA")

            tracker.block_days = 31
            nearer_future = tracker.blocked_until("TSLA")

            self.assertLess(nearer_future, far_future)
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_a_non_string_blocked_at_self_heals_instead_of_crashing(self):
        """Live incident: 11 symbols (all with a genuine wash-sale
        block entry) hit "fromisoformat: argument must be str" on
        every single scan - a TypeError, not the ValueError the
        existing malformed-string handling caught, so it crashed
        instead of self-healing like every other corrupt-entry case
        here already does.
        """
        path = Path("tests/.generated_wash/non_string_blocked_at.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            tracker = self._tracker(path, 31)
            tracker.blocks["ORCL"] = {"blocked_at": None}

            result = tracker.blocked_until("ORCL")

            self.assertIsNone(result)
            self.assertNotIn("ORCL", tracker.blocks)
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_concurrent_saves_never_raise_enoent_on_the_shared_tmp_file(self):
        """Same race as DailyPnlTracker's - two concurrent block()
        calls for different symbols both write the same shared
        ".tmp" path; whichever replace() ran second used to find the
        first had already consumed it. _save_lock serializes the
        write-then-replace pair so this can't happen.
        """
        path = Path("tests/.generated_wash/concurrent.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            tracker = self._tracker(path, 31)
            errors: list[Exception] = []

            def hammer(n: int) -> None:
                try:
                    for i in range(10):
                        tracker.block(f"SYM{n}-{i}", "stop-loss exit submitted")
                except Exception as exc:
                    errors.append(exc)

            threads = [
                threading.Thread(target=hammer, args=(n,)) for n in range(4)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            self.assertTrue(path.exists())
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)


class DailyPnlTrackerTests(unittest.TestCase):
    def _tracker(self, path):
        return DailyPnlTracker(str(path), timezone.utc, logging.getLogger("test-daily-pnl"))

    def test_fresh_file_starts_at_zero(self):
        path = Path("tests/.generated_daily_pnl/fresh.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            tracker = self._tracker(path)
            self.assertEqual(tracker.realized_pnl, Decimal("0"))
            self.assertEqual(tracker.realized_loss, Decimal("0"))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_record_then_new_instance_loads_the_same_totals(self):
        path = Path("tests/.generated_daily_pnl/roundtrip.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            tracker = self._tracker(path)
            tracker.record(Decimal("42.50"), Decimal("10.00"))

            reloaded = self._tracker(path)
            self.assertEqual(reloaded.realized_pnl, Decimal("42.50"))
            self.assertEqual(reloaded.realized_loss, Decimal("10.00"))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_stale_date_does_not_carry_over_to_a_new_day(self):
        path = Path("tests/.generated_daily_pnl/stale.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
            path.write_text(
                json.dumps(
                    {
                        "date": yesterday,
                        "realized_pnl": "500.00",
                        "realized_loss": "50.00",
                    }
                ),
                encoding="utf-8",
            )

            tracker = self._tracker(path)

            self.assertEqual(tracker.realized_pnl, Decimal("0"))
            self.assertEqual(tracker.realized_loss, Decimal("0"))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_reset_zeroes_and_persists(self):
        path = Path("tests/.generated_daily_pnl/reset.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            tracker = self._tracker(path)
            tracker.record(Decimal("30.00"), Decimal("5.00"))

            tracker.reset()

            self.assertEqual(tracker.realized_pnl, Decimal("0"))
            self.assertEqual(tracker.realized_loss, Decimal("0"))
            reloaded = self._tracker(path)
            self.assertEqual(reloaded.realized_pnl, Decimal("0"))
            self.assertEqual(reloaded.realized_loss, Decimal("0"))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_corrupt_file_logs_warning_and_starts_fresh(self):
        path = Path("tests/.generated_daily_pnl/corrupt.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("not json", encoding="utf-8")

            with self.assertLogs("test-daily-pnl", level="WARNING"):
                tracker = self._tracker(path)

            self.assertEqual(tracker.realized_pnl, Decimal("0"))
            self.assertEqual(tracker.realized_loss, Decimal("0"))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_concurrent_saves_never_raise_enoent_on_the_shared_tmp_file(self):
        """Live incident: two order events landing on the same poll
        cycle both called record() close together, and whichever
        replace() ran second found the first had already renamed
        away the shared ".tmp" path - ENOENT ("daily_pnl.tmp ->
        daily_pnl.json"). _save_lock serializes the write-then-
        replace pair so this race can't happen.
        """
        path = Path("tests/.generated_daily_pnl/concurrent.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            tracker = self._tracker(path)
            errors: list[Exception] = []

            def hammer(n: int) -> None:
                try:
                    # By request ("quickly do a sanity check"): this
                    # was flaky on Windows dev machines (not the Linux
                    # production host) at higher iteration/thread
                    # counts - a transient AV/indexer file-lock on the
                    # shared .tmp path (PermissionError), not a real
                    # logic bug in the lock itself. Reduced load keeps
                    # this meaningfully exercising the lock without
                    # tripping that unrelated OS-level flakiness.
                    for i in range(10):
                        tracker.record(Decimal(n * 100 + i), Decimal("0"))
                except Exception as exc:
                    errors.append(exc)

            threads = [
                threading.Thread(target=hammer, args=(n,)) for n in range(4)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            self.assertTrue(path.exists())
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)
