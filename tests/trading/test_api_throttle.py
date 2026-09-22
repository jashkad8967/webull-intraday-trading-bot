import threading
import time
import unittest

from webull_bot.webull_api import WebullAPI


def _api(intervals: dict[str, float]) -> WebullAPI:
    """A bare WebullAPI with only the throttle machinery wired up -
    no network, no credentials.
    """
    api = WebullAPI.__new__(WebullAPI)
    api._lock = threading.Lock()
    api._last_request = {}
    api._request_interval = lambda group: intervals.get(group, 0.0)
    return api


class ThrottleDoesNotBlockOtherThreadsTests(unittest.TestCase):
    """Live incident 2026-09-22.

    _throttle slept INSIDE `with self._lock`, and that lock is shared
    by every request group. The once-daily universe download runs on
    its own background thread (resolve_targets exists precisely so
    slow work cannot block trading), but its throttle delay was taken
    while holding the global lock - so the main trading loop, the fast
    position-protection thread, order placement and stop-loss checks
    all queued behind it.

    Measured live: no SCAN line for over 6 minutes while
    LOAD | US_LISTED paged through 7,500 symbols. An open position
    went unmonitored and a filled exit went unnoticed, because
    account_state() never got a cycle. Running on a separate thread
    meant nothing while the lock was held in sleep.
    """

    def test_a_slow_group_does_not_starve_a_fast_one(self):
        api = _api({"market": 3.0, "quote": 0.0})
        stop = threading.Event()

        def hog():
            while not stop.is_set():
                api._throttle("market")

        worker = threading.Thread(target=hog, daemon=True)
        worker.start()
        try:
            time.sleep(0.2)  # let the slow group take its slot first
            started = time.monotonic()
            for _ in range(5):
                api._throttle("quote")
            elapsed = time.monotonic() - started
        finally:
            stop.set()
        self.assertLess(
            elapsed,
            1.0,
            "the trading group was starved by an unrelated slow group - "
            "the throttle is sleeping while holding the shared lock",
        )

    def test_the_lock_is_not_held_while_waiting(self):
        """Directly: another thread must be able to take the lock while
        a throttle call is waiting out its spacing.
        """
        api = _api({"slow": 1.0})
        api._throttle("slow")  # claim the slot so the next call must wait

        waiting = threading.Thread(
            target=lambda: api._throttle("slow"), daemon=True
        )
        waiting.start()
        time.sleep(0.2)  # it is now inside its wait
        self.assertTrue(
            api._lock.acquire(timeout=0.5),
            "lock was held during the throttle wait",
        )
        api._lock.release()
        waiting.join(timeout=3)

    def test_spacing_is_still_enforced_within_one_group(self):
        """The fix must not turn the throttle into a no-op."""
        api = _api({"market": 0.3})
        started = time.monotonic()
        for _ in range(3):
            api._throttle("market")
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(
            elapsed, 0.6, "requests in one group must still be spaced"
        )

    def test_concurrent_callers_in_one_group_do_not_share_a_slot(self):
        """Two threads in the same group must not both claim the same
        slot - the reservation still happens under the lock.
        """
        api = _api({"market": 0.25})
        stamps: list[float] = []
        lock = threading.Lock()

        def call():
            api._throttle("market")
            with lock:
                stamps.append(time.monotonic())

        threads = [threading.Thread(target=call) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        stamps.sort()
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertEqual(len(stamps), 4)
        for gap in gaps:
            self.assertGreaterEqual(
                gap, 0.2, f"two callers claimed the same slot (gap {gap:.3f}s)"
            )


if __name__ == "__main__":
    unittest.main()
