import threading
import time
import unittest
from types import SimpleNamespace

from webull_bot.trading.orders.option_exit_claim import (
    _claim_option_exit,
    _release_option_exit,
)


def _bot():
    bot = SimpleNamespace(pending_option_exits=set())
    bot._claim_option_exit = _claim_option_exit.__get__(bot)
    bot._release_option_exit = _release_option_exit.__get__(bot)
    return bot


class OptionExitClaimTests(unittest.TestCase):
    """Exits are reached from TWO threads - the 0.5s protection loop
    (evaluate_held_option_exits) and the slow scan (trade_options) -
    plus boost_stalled_positions and the dashboard's manual sell.

    Every one of those used a plain check-then-act:

        if option_symbol not in self.pending_option_exits:
            ...
            self.api.place_option(...)        # hundreds of ms
            self.pending_option_exits.add(option_symbol)

    Two threads could both pass the test before either added, and the
    window spans a whole broker round-trip. The result is two SELL
    orders against one position: at best the broker rejects the
    second, at worst it reverses the position into a short.
    """

    def test_only_one_of_two_racing_threads_wins_the_claim(self):
        bot = _bot()
        start = threading.Barrier(2)
        won: list[bool] = []
        lock = threading.Lock()

        def attempt():
            start.wait()
            got = bot._claim_option_exit("NVDAC")
            with lock:
                won.append(got)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(
            won.count(True), 1, "exactly one thread may place the exit"
        )
        self.assertEqual(won.count(False), 1)

    def test_a_crowd_of_threads_still_yields_exactly_one_winner(self):
        bot = _bot()
        start = threading.Barrier(12)
        won = []
        lock = threading.Lock()

        def attempt():
            start.wait()
            got = bot._claim_option_exit("NVDAC")
            with lock:
                won.append(got)

        threads = [threading.Thread(target=attempt) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        self.assertEqual(won.count(True), 1)

    def test_releasing_lets_a_later_attempt_through(self):
        """A placement that fails must hand the claim back, or the
        contract is locked out of every future exit this session.
        """
        bot = _bot()
        self.assertTrue(bot._claim_option_exit("NVDAC"))
        self.assertFalse(bot._claim_option_exit("NVDAC"))
        bot._release_option_exit("NVDAC")
        self.assertTrue(
            bot._claim_option_exit("NVDAC"),
            "after a release the contract must be claimable again",
        )

    def test_distinct_contracts_do_not_block_each_other(self):
        bot = _bot()
        self.assertTrue(bot._claim_option_exit("NVDAC"))
        self.assertTrue(
            bot._claim_option_exit("NVDAP"),
            "one lock table must not serialise unrelated contracts",
        )

    def test_the_lock_is_never_held_across_slow_work(self):
        """The claim must be a microsecond test-and-set. If it were
        ever held across an API call the fast loop would stall behind
        the scan thread - the exact class of bug the throttle fix
        already had to undo.
        """
        bot = _bot()
        bot._claim_option_exit("HELD")
        started = time.monotonic()
        for i in range(2000):
            bot._claim_option_exit(f"S{i}")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0, "claiming must be cheap")


if __name__ == "__main__":
    unittest.main()
