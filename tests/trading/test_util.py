import threading
import time
import unittest
import unittest.mock



class ConcurrentDispatchTests(unittest.TestCase):
    """_dispatch_concurrently - by request: "do not wait for the
    response to fire another request," scoped to the position-
    protection loop's per-candidate repricers (reprice_resting_exits/
    entries, reprice_volatility_scalp_exits/entries), which previously
    ran one order at a time, each waiting out a full network round-
    trip before the next candidate's requests even started.
    """

    def test_runs_every_item_even_when_none_raise(self):
        from webull_bot.bot import _dispatch_concurrently

        seen = []
        lock = threading.Lock()

        def worker(item):
            with lock:
                seen.append(item)

        _dispatch_concurrently([1, 2, 3, 4, 5], worker)
        self.assertEqual(sorted(seen), [1, 2, 3, 4, 5])

    def test_one_bad_item_does_not_block_the_others(self):
        from webull_bot.bot import _dispatch_concurrently

        seen = []
        lock = threading.Lock()

        def worker(item):
            if item == 2:
                raise ValueError("boom")
            with lock:
                seen.append(item)

        _dispatch_concurrently([1, 2, 3], worker)
        self.assertEqual(sorted(seen), [1, 3])

    def test_empty_list_is_a_no_op(self):
        from webull_bot.bot import _dispatch_concurrently

        calls = []
        _dispatch_concurrently([], lambda item: calls.append(item))
        self.assertEqual(calls, [])

    def test_items_actually_overlap_instead_of_running_sequentially(self):
        """The whole point of this change - if items ran one at a
        time waiting out each other's full duration, N items each
        taking `delay` would take N*delay total. Concurrently, it
        should take close to just `delay` regardless of N.
        """
        from webull_bot.bot import _dispatch_concurrently

        delay = 0.2
        n = 4
        started = time.monotonic()
        _dispatch_concurrently(list(range(n)), lambda item: time.sleep(delay))
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, delay * n)


class RateLimitRetryTests(unittest.TestCase):
    """_retry_once_on_rate_limit - by request: "if there is any 429,
    make sure to refire that order asap." A single quick retry, not an
    unbounded loop (which would itself worsen the rate limit it's
    trying to recover from) - live evidence this session: CLOSE and
    RECON both hit HTTP 429 TOO_MANY_REQUESTS right after a restart.
    """

    def test_succeeds_immediately_when_no_error(self):
        from webull_bot.bot import _retry_once_on_rate_limit

        result = _retry_once_on_rate_limit(lambda x: x * 2, 21)
        self.assertEqual(result, 42)

    def test_retries_once_after_a_429_and_then_succeeds(self):
        from webull_bot.bot import _retry_once_on_rate_limit

        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception("HTTP Status: 429, Code: TOO_MANY_REQUESTS")
            return "ok"

        result = _retry_once_on_rate_limit(flaky, delay=0.01)
        self.assertEqual(result, "ok")
        self.assertEqual(calls["n"], 2)

    def test_does_not_retry_a_non_rate_limit_error(self):
        from webull_bot.bot import _retry_once_on_rate_limit

        calls = {"n": 0}

        def always_fails():
            calls["n"] += 1
            raise Exception("some other error")

        with self.assertRaises(Exception):
            _retry_once_on_rate_limit(always_fails, delay=0.01)
        self.assertEqual(calls["n"], 1)

    def test_reraises_if_still_rate_limited_after_the_one_retry(self):
        from webull_bot.bot import _retry_once_on_rate_limit

        calls = {"n": 0}

        def always_rate_limited():
            calls["n"] += 1
            raise Exception("HTTP Status: 429, Code: TOO_MANY_REQUESTS")

        with self.assertRaises(Exception):
            _retry_once_on_rate_limit(always_rate_limited, delay=0.01)
        self.assertEqual(calls["n"], 2)
