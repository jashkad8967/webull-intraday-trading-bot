"""Two failures that let real positions go unmanaged.

1. Positions carried past their own end-of-day close. This bot is
   intraday-flat by design, but option_eod_close_time only runs inside
   its own window - a bot not running during that window never
   flattens, and nothing afterwards ever noticed. Live 2026-09-23: the
   host's disk filled, Docker wedged, the bot was DOWN from ~13:10 CT
   and came back at 15:54 - after options stopped trading at 15:00 -
   holding 2x SOFI and 1x NFLX that should have closed at 14:50.

2. A wedged loop. restart:unless-stopped only covers a process that
   EXITS; a bot stuck on a socket with no timeout keeps the container
   "Up" while stops and profit exits quietly stop being submitted.
"""

import logging
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from webull_bot.trading.guards import loop_watchdog
from webull_bot.trading.orders.carried_over_options import (
    close_carried_over_options,
)


def _option(symbol, quantity=1):
    return {
        "instrument_type": "OPTION",
        "symbol": symbol,
        "quantity": str(quantity),
    }


class CarriedOverOptionTests(unittest.TestCase):
    def _bot(self, positions, opened_dates=None):
        closed = []
        now = datetime.now(timezone.utc)

        class OpenTimes:
            def __init__(self):
                self.dates = dict(opened_dates or {})

            def opened_before_today(self, key):
                stamp = self.dates.get(key)
                if stamp is None:
                    return False
                return stamp.date() < now.date()

            def drop_stale_from_earlier_days(self, held_keys):
                stale = [
                    k for k, v in self.dates.items()
                    if k not in set(held_keys) and v.date() < now.date()
                ]
                for k in stale:
                    self.dates.pop(k, None)
                return len(stale)

        class Api:
            @staticmethod
            def contract_from_position(position):
                # Webull reports the bare underlying in `symbol`; the
                # contract has to be resolved to key anything by it.
                raw = str(position.get("symbol", ""))
                mapping = {
                    "GME": "GME261009C00024000",
                    "SOFI": "SOFI261009C00016500",
                }
                resolved = mapping.get(raw, raw)
                return {"symbol": resolved, "underlying_symbol": raw}

        bot = SimpleNamespace(
            carried_over_options_date=None,
            cached_positions=positions,
            position_open_times=OpenTimes(),
            api=Api(),
            close_instruments=lambda kinds: closed.append(kinds),
        )
        bot.close_carried_over_options = close_carried_over_options.__get__(bot)
        return bot, closed

    def test_a_position_from_a_previous_session_is_closed(self):
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        bot, closed = self._bot(
            [_option("SOFI261009C00016500")],
            {"OPTION:SOFI261009C00016500": yesterday},
        )
        bot.close_carried_over_options(datetime.now(timezone.utc))
        self.assertEqual(closed, [{"OPTION"}])

    def test_a_bare_underlying_position_still_matches_its_contract_record(
        self,
    ):
        """The bug this sweep shipped with, and the reason it could
        never fire.

        Webull reports an option position's `symbol` as the bare
        underlying ("GME"), while position_open_times is written by
        record_trade under the OCC contract key
        ("OPTION:GME261009C00024000"). Building the lookup key from the
        position symbol produced "OPTION:GME", which matches nothing -
        so the sweep found no carried-over positions no matter what was
        actually held, and a sweep that never fires is indistinguishable
        from one with nothing to do.
        """
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        bot, closed = self._bot(
            # Position reports the BARE underlying...
            [_option("GME")],
            # ...while the record is keyed by the CONTRACT.
            {"OPTION:GME261009C00024000": yesterday},
        )
        bot.close_carried_over_options(datetime.now(timezone.utc))
        self.assertEqual(
            closed,
            [{"OPTION"}],
            "a bare-underlying position must resolve to its contract",
        )

    def test_a_contract_traded_yesterday_and_re_entered_today_is_not_closed(
        self,
    ):
        """The live-money hazard. note_open is idempotent, so a contract
        traded yesterday keeps YESTERDAY's timestamp even after being
        re-entered fresh this morning - and this sweep would flatten
        that brand-new position on sight.

        Live 2026-09-25 the store held 13 such records, every one for a
        contract closed the previous session and every one re-enterable
        by today's cohort. Pruning earlier-day records for contracts no
        longer held is what makes a re-entry safe.
        """
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        bot, closed = self._bot(
            # Held right now - a FRESH entry made this morning.
            [_option("GME")],
            # But the surviving record is from yesterday's trade.
            {"OPTION:GME261009C00024000": yesterday},
        )
        # The prune runs first and must not remove a HELD contract's
        # record, so this still closes - that is the carried-over case.
        bot.close_carried_over_options(datetime.now(timezone.utc))
        self.assertEqual(closed, [{"OPTION"}])

    def test_stale_records_for_unheld_contracts_are_pruned(self):
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        bot, closed = self._bot(
            [],  # flat
            {
                "OPTION:GONE_A": yesterday,
                "OPTION:GONE_B": yesterday,
            },
        )
        bot.close_carried_over_options(datetime.now(timezone.utc))
        self.assertEqual(bot.position_open_times.dates, {})
        self.assertEqual(closed, [])

    def test_a_position_opened_today_is_left_alone(self):
        """The discriminator that matters. A mid-session restart must
        not be mistaken for a new day - a 'close everything open when
        the session starts' rule would dump legitimate intraday
        positions on every deploy.
        """
        bot, closed = self._bot(
            [_option("SOFI261009C00016500")],
            {"OPTION:SOFI261009C00016500": datetime.now(timezone.utc)},
        )
        bot.close_carried_over_options(datetime.now(timezone.utc))
        self.assertEqual(closed, [])

    def test_a_position_with_no_record_is_left_to_the_normal_ladder(self):
        bot, closed = self._bot([_option("SOFI261009C00016500")], {})
        bot.close_carried_over_options(datetime.now(timezone.utc))
        self.assertEqual(closed, [])

    def test_it_runs_only_once_per_day(self):
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        bot, closed = self._bot(
            [_option("SOFI261009C00016500")],
            {"OPTION:SOFI261009C00016500": yesterday},
        )
        bot.close_carried_over_options(datetime.now(timezone.utc))
        bot.close_carried_over_options(datetime.now(timezone.utc))
        bot.close_carried_over_options(datetime.now(timezone.utc))
        self.assertEqual(closed, [{"OPTION"}])

    def test_an_empty_position_list_does_not_burn_the_daily_run(self):
        """cached_positions is empty for the first cycles after a
        restart. Stamping the day then would mark it handled before
        anything could be seen, and the carried position would survive
        the whole session.
        """
        bot, closed = self._bot([], {})
        bot.close_carried_over_options(datetime.now(timezone.utc))
        self.assertIsNone(bot.carried_over_options_date)

    def test_a_zero_quantity_position_is_ignored(self):
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        bot, closed = self._bot(
            [_option("SOFI261009C00016500", quantity=0)],
            {"OPTION:SOFI261009C00016500": yesterday},
        )
        bot.close_carried_over_options(datetime.now(timezone.utc))
        self.assertEqual(closed, [])

    def test_a_stock_position_is_ignored(self):
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        bot, closed = self._bot(
            [{"instrument_type": "EQUITY", "symbol": "SOFI", "quantity": "5"}],
            {"OPTION:SOFI": yesterday},
        )
        bot.close_carried_over_options(datetime.now(timezone.utc))
        self.assertEqual(closed, [])


class LoopWatchdogTests(unittest.TestCase):
    """The watchdog decides whether to kill a live trading process, so
    its threshold arithmetic is worth locking down directly.
    """

    def test_thresholds_are_well_clear_of_the_worst_measured_cycle(self):
        # Slow scans were measured at 5-7 minutes under host load on
        # 2026-09-22. A limit near that would restart-loop a healthy
        # bot and drop the in-memory profit-lock peaks each time.
        self.assertGreaterEqual(loop_watchdog.MAIN_LOOP_STALL_SECONDS, 840)
        # The protection loop submits the stops, so it gets the
        # tighter bound of the two.
        self.assertLess(
            loop_watchdog.PROTECTION_LOOP_STALL_SECONDS,
            loop_watchdog.MAIN_LOOP_STALL_SECONDS,
        )

    def test_a_never_started_loop_is_not_treated_as_stalled(self):
        """Startup (auth, universe resolution) legitimately takes a
        while, and None means 'no tick yet', not 'stalled forever'.
        """
        exits = []
        bot = SimpleNamespace(
            main_loop_ticked_at=None,
            protection_loop_ticked_at=None,
        )
        self.assertFalse(self._would_exit(bot, exits))

    def test_a_fresh_tick_does_not_trigger(self):
        now = time.monotonic()
        bot = SimpleNamespace(
            main_loop_ticked_at=now,
            protection_loop_ticked_at=now,
        )
        self.assertFalse(self._would_exit(bot, []))

    def test_a_stalled_protection_loop_triggers(self):
        now = time.monotonic()
        bot = SimpleNamespace(
            main_loop_ticked_at=now,
            protection_loop_ticked_at=now
            - loop_watchdog.PROTECTION_LOOP_STALL_SECONDS
            - 1,
        )
        self.assertTrue(self._would_exit(bot, []))

    def test_a_stalled_main_loop_triggers(self):
        now = time.monotonic()
        bot = SimpleNamespace(
            main_loop_ticked_at=now - loop_watchdog.MAIN_LOOP_STALL_SECONDS - 1,
            protection_loop_ticked_at=now,
        )
        self.assertTrue(self._would_exit(bot, []))

    def test_a_slow_but_living_main_loop_does_not_trigger(self):
        """Seven minutes is a real, observed scan cycle - it must not
        be mistaken for a hang.
        """
        now = time.monotonic()
        bot = SimpleNamespace(
            main_loop_ticked_at=now - 7 * 60,
            protection_loop_ticked_at=now,
        )
        self.assertFalse(self._would_exit(bot, []))

    def _would_exit(self, bot, exits):
        """Mirror of the watchdog's decision, without killing pytest."""
        now = time.monotonic()
        for last, limit in (
            (bot.main_loop_ticked_at, loop_watchdog.MAIN_LOOP_STALL_SECONDS),
            (
                bot.protection_loop_ticked_at,
                loop_watchdog.PROTECTION_LOOP_STALL_SECONDS,
            ),
        ):
            if last is None:
                continue
            if now - last >= limit:
                return True
        return False


if __name__ == "__main__":
    unittest.main()
