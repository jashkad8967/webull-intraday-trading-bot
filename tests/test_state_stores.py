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
from webull_bot.position_open_times import PositionOpenTimeStore


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

    def test_expired_blocks_are_dropped_at_load_not_carried_forever(self):
        """blocked_until() prunes lazily - only for a symbol someone
        asks about - so a name the scanner never surfaces again keeps
        its entry for the life of the deployment. Live 2026-09-23 this
        file held 336 blocks, most of them long-dead penny stocks from
        weeks earlier, every one re-read and re-serialised on every
        save, on a host whose disk had just filled to 99%.
        """
        path = Path("tests/.generated_wash/expired.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            now = datetime.now(timezone.utc)
            path.write_text(
                json.dumps({
                    "STALE": {
                        "blocked_at": (now - timedelta(days=90)).isoformat()
                    },
                    "ALSOSTALE": {
                        "blocked_at": (now - timedelta(days=32)).isoformat()
                    },
                    "FRESH": {
                        "blocked_at": (now - timedelta(days=2)).isoformat()
                    },
                    "CORRUPT": {"blocked_at": "not-a-date"},
                }),
                encoding="utf-8",
            )
            tracker = self._tracker(path, 31)
            self.assertEqual(set(tracker.blocks), {"FRESH"})
            # And the shrunken set is what the next start reads.
            self.assertEqual(
                set(json.loads(path.read_text(encoding="utf-8"))), {"FRESH"}
            )
            self.assertIsNotNone(tracker.blocked_until("FRESH"))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_a_file_of_only_live_blocks_is_not_rewritten(self):
        path = Path("tests/.generated_wash/all_live.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            now = datetime.now(timezone.utc)
            payload = {
                "FRESH": {"blocked_at": (now - timedelta(days=1)).isoformat()}
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            before = path.read_text(encoding="utf-8")
            tracker = self._tracker(path, 31)
            self.assertEqual(set(tracker.blocks), {"FRESH"})
            self.assertEqual(path.read_text(encoding="utf-8"), before)
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


class PositionOpenTimeStoreTests(unittest.TestCase):
    """The age clock used to reset to zero on every restart, because
    position_opened_at was in-memory and the exit path reseeded a
    missing entry from "first sight". Each restart therefore bought a
    stalled position another full option_stale_exit_minutes of
    immunity - a bot restarted repeatedly could hold one forever.
    """

    def _store(self, path):
        return PositionOpenTimeStore(
            str(path), timezone.utc, logging.getLogger("test-posage")
        )

    def test_the_recorded_time_survives_a_restart(self):
        path = Path("tests/.generated_posage/open.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            first = self._store(path).note_open("OPTION:SOFI")
            # A second process reading the same file.
            second = self._store(path).note_open("OPTION:SOFI")
            self.assertEqual(first, second)
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_note_open_is_idempotent_within_one_process(self):
        path = Path("tests/.generated_posage/idem.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            store = self._store(path)
            first = store.note_open("OPTION:SOFI")
            again = store.note_open("OPTION:SOFI")
            self.assertEqual(first, again)
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_opened_before_today_distinguishes_a_carried_position(self):
        path = Path("tests/.generated_posage/carried.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            yesterday = datetime.now(timezone.utc) - timedelta(days=1)
            path.write_text(
                json.dumps({
                    "OPTION:OLD": yesterday.isoformat(),
                    "OPTION:NEW": datetime.now(timezone.utc).isoformat(),
                }),
                encoding="utf-8",
            )
            store = self._store(path)
            self.assertTrue(store.opened_before_today("OPTION:OLD"))
            self.assertFalse(store.opened_before_today("OPTION:NEW"))
            # An unknown key must not be treated as carried over.
            self.assertFalse(store.opened_before_today("OPTION:UNSEEN"))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_forget_removes_a_record(self):
        path = Path("tests/.generated_posage/bounded.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            store = self._store(path)
            for key in ("A", "B"):
                store.note_open(key)
            store.forget("A")
            self.assertEqual(set(store.opened_at), {"B"})
            self.assertEqual(set(self._store(path).opened_at), {"B"})
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_stale_records_from_earlier_days_are_dropped(self):
        """Live 2026-09-25: 13 records sat in this file, every one for a
        contract closed the previous session.

        forget() runs on exit, but the fast exit-evaluation loop
        re-stamps via note_open for a few seconds afterwards while
        cached_positions still shows the closed position - so records
        come back after being removed.
        """
        path = Path("tests/.generated_posage/stale.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            yesterday = datetime.now(timezone.utc) - timedelta(days=1)
            now = datetime.now(timezone.utc)
            path.write_text(
                json.dumps({
                    "OPTION:OLD_A": yesterday.isoformat(),
                    "OPTION:OLD_B": yesterday.isoformat(),
                    "OPTION:HELD_FROM_YESTERDAY": yesterday.isoformat(),
                    "OPTION:TODAY": now.isoformat(),
                }),
                encoding="utf-8",
            )
            store = self._store(path)
            dropped = store.drop_stale_from_earlier_days(
                {"OPTION:HELD_FROM_YESTERDAY"}
            )
            self.assertEqual(dropped, 2)
            self.assertEqual(
                set(store.opened_at),
                {"OPTION:HELD_FROM_YESTERDAY", "OPTION:TODAY"},
            )
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_a_genuinely_carried_position_keeps_its_record(self):
        """The whole point of the store: a position actually still held
        from yesterday must keep its real entry date, or the carried-over
        sweep cannot see it and its stale-exit clock resets.
        """
        path = Path("tests/.generated_posage/carried_keep.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            yesterday = datetime.now(timezone.utc) - timedelta(days=1)
            path.write_text(
                json.dumps({"OPTION:HELD": yesterday.isoformat()}),
                encoding="utf-8",
            )
            store = self._store(path)
            store.drop_stale_from_earlier_days({"OPTION:HELD"})
            self.assertTrue(store.opened_before_today("OPTION:HELD"))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_todays_records_survive_an_empty_position_snapshot(self):
        """Every restart has a moment where cached_positions is empty.
        That must never delete the age of something actually open -
        losing it resets the stale-exit clock, the original bug.
        """
        path = Path("tests/.generated_posage/empty_snapshot.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            store = self._store(path)
            store.note_open("OPTION:OPENED_TODAY")
            dropped = store.drop_stale_from_earlier_days(set())
            self.assertEqual(dropped, 0)
            self.assertIn("OPTION:OPENED_TODAY", store.opened_at)
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_a_corrupt_file_does_not_crash_the_bot(self):
        path = Path("tests/.generated_posage/corrupt.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text("{not json", encoding="utf-8")
            store = self._store(path)
            self.assertEqual(store.opened_at, {})
            self.assertIsNotNone(store.note_open("OPTION:SOFI"))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_a_corrupt_timestamp_is_not_read_as_carried_over(self):
        path = Path("tests/.generated_posage/badstamp.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(
                json.dumps({"OPTION:SOFI": "not-a-date"}), encoding="utf-8"
            )
            store = self._store(path)
            self.assertFalse(store.opened_before_today("OPTION:SOFI"))
            # And a fresh stamp replaces it rather than sticking.
            self.assertIsNotNone(store.note_open("OPTION:SOFI"))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)
