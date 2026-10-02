"""The watchdog must detect a hang, not a slow cycle.

Live 2026-10-02, with zero trades placed all morning on a healthy bot:

    SCAN cycles  09:08:45 -> 09:21:34 -> 09:42:08   (12m49s, 20m34s)
    09:32:54  OPTIONS | NVDA...C00235000 | REFUSED | a 10% stop is only
              a 0.48% move in NVDA
    09:36:39  WATCHDOG| main scan loop has not ticked for 905s
              (limit 900s) - exiting so the container restarts

Nothing was wedged. The bot had logged option refusals four minutes
before being shot. MAIN_LOOP_STALL_SECONDS was calibrated in September
against 5-7 minute cycles; by October a real pass took 13-20 minutes,
because 165 option contracts were being evaluated AND a warning formatted
for each one after the liquid-ETF chains were discovered.

Each kill then dropped the daily batch, the locked cohort and the
direction-signal EMA history - and option_direction_signal cannot emit
anything but HOLD without warm history. So: slow cycle -> killed ->
restart -> re-warm -> slow cycle. The monitor was not detecting a
livelock, it was causing one. Three cycles in 35 minutes, zero orders.

Two fixes, pinned here:

  * the watchdog reads main_loop_progress_at, updated INSIDE the cycle,
    so its bound means "no progress at all" rather than "no completed
    pass". Raising the number instead would have rotted again the next
    time the option universe grew.
  * gate rejections log once per gate per underlying per minute. The
    counters still count every rejection, so the totals are unchanged -
    only the repetition on the hot path is gone.
"""

import time
import unittest
from types import SimpleNamespace

from webull_bot.trading.guards.loop_watchdog import MAIN_LOOP_STALL_SECONDS
from webull_bot.trading.options.option_entry_exit import (
    _GATE_LOG_INTERVAL_SECONDS,
    _should_log_gate,
)


class GateLogThrottleTests(unittest.TestCase):
    def setUp(self):
        self.bot = SimpleNamespace()

    def test_the_first_rejection_for_an_underlying_logs(self):
        self.assertTrue(_should_log_gate(self.bot, "noise", "NVDA"))

    def test_a_flood_on_one_underlying_logs_once(self):
        """The live shape: 165 contracts on a handful of underlyings in a
        single pass.
        """
        logged = sum(
            1 for _ in range(165)
            if _should_log_gate(self.bot, "noise", "NVDA")
        )
        self.assertEqual(logged, 1)

    def test_each_underlying_still_gets_a_line(self):
        """Throttling must not hide that a DIFFERENT name is being
        refused - that is the diagnostic value of these lines.
        """
        for symbol in ("NVDA", "SPY", "QQQ", "PFE"):
            with self.subTest(symbol=symbol):
                self.assertTrue(_should_log_gate(self.bot, "noise", symbol))

    def test_each_gate_is_throttled_independently(self):
        """A contract refused for hurdle and another for noise are
        different findings and both deserve a line.
        """
        self.assertTrue(_should_log_gate(self.bot, "noise", "NVDA"))
        self.assertTrue(_should_log_gate(self.bot, "hurdle", "NVDA"))
        self.assertTrue(_should_log_gate(self.bot, "riskcap", "NVDA"))

    def test_it_logs_again_after_the_interval(self):
        _should_log_gate(self.bot, "noise", "NVDA")
        # Rewind the recorded time rather than sleeping a minute.
        self.bot._gate_log_last[("noise", "NVDA")] -= (
            _GATE_LOG_INTERVAL_SECONDS + 1
        )
        self.assertTrue(_should_log_gate(self.bot, "noise", "NVDA"))

    def test_a_missing_underlying_does_not_crash(self):
        self.assertTrue(_should_log_gate(self.bot, "noise", None))
        self.assertFalse(_should_log_gate(self.bot, "noise", None))


class WatchdogMeasuresProgressTests(unittest.TestCase):
    """A 20-minute cycle that is making progress must survive; a genuinely
    wedged loop must still die.
    """

    def _effective(self, bot):
        # Mirrors the expression the watchdog uses.
        return getattr(bot, "main_loop_progress_at", None) or bot.main_loop_ticked_at

    def test_a_slow_but_progressing_cycle_is_not_a_stall(self):
        now = time.monotonic()
        bot = SimpleNamespace(
            # cycle started 20 minutes ago - over the bound
            main_loop_ticked_at=now - 1200,
            # but it beat 5 seconds ago
            main_loop_progress_at=now - 5,
        )
        self.assertLess(now - self._effective(bot), MAIN_LOOP_STALL_SECONDS)

    def test_a_genuinely_wedged_loop_still_trips(self):
        now = time.monotonic()
        bot = SimpleNamespace(
            main_loop_ticked_at=now - 1200,
            main_loop_progress_at=now - 1200,
        )
        self.assertGreaterEqual(
            now - self._effective(bot), MAIN_LOOP_STALL_SECONDS
        )

    def test_it_falls_back_when_progress_was_never_recorded(self):
        """An older caller that only sets the cycle tick keeps the
        original behaviour rather than losing the watchdog entirely.
        """
        now = time.monotonic()
        bot = SimpleNamespace(main_loop_ticked_at=now - 1200)
        self.assertGreaterEqual(
            now - self._effective(bot), MAIN_LOOP_STALL_SECONDS
        )

    def test_the_option_loop_beats_the_heartbeat(self):
        """The slow loop must actually update it, or none of the above
        matters.
        """
        from pathlib import Path
        repo = Path(__file__).resolve().parent.parent
        text = (repo / "src/webull_bot/bot/__init__.py").read_text(
            encoding="utf-8"
        )
        head = text.index("for contract in batch:")
        window = text[head:head + 700]
        self.assertIn("main_loop_progress_at = time.monotonic()", window)


if __name__ == "__main__":
    unittest.main()
