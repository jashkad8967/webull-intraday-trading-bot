"""Share entries are restricted to the names the day's screening chose.

Live 2026-10-01 10:50: with the cohort locked to [ACN, MRNA, PLTR] and
the daily batch [GOOGL, NVDA, AAPL, ACN, MRNA, RIVN, PLTR, GME], the bot
bought XOM. XOM is in neither list.

Two separate holes produced it, in the same change:

  1. 10:27 MEDS, a $4 name - below focus_min_price ($10), so it cannot
     be in the batch or the cohort at all. Fixed in deb15f4 by sitting
     out when NOTHING is screened.
  2. 10:50 XOM - fix (1) only decided WHETHER entries may happen, not
     WHICH names. The scan batch is built from prioritized_stock_batch
     over the whole universe UNION scan_watch_symbols (seed_popular |
     agent_popular | user_watchlist), and XOM is one of 116 watchlist
     names - none of which cleared a gate that day.

The XOM position was flat (-$0.03) and still did real damage: $24 of an
$85 account, in a mega-cap whose daily range is ~1% against a 0.9-1.5%
stop, capping option premium at $0.61 and putting the entire SPY CALL
side out of reach (0.46/0.47 = 5.4% hurdle vs the 5.0% ceiling) on a day
the account was meant to be trading options.

Screening on gap, RVOL, volume and spread is the only reason to believe
a name is worth risk. Buying something that cleared none of it is not a
smaller version of the strategy, it is the absence of one.
"""

import unittest
from types import SimpleNamespace

from webull_bot.bot import AutoTrader


def _bot(cohort=(), daily_batch=(), focus_mode_enabled=True):
    return SimpleNamespace(
        config=SimpleNamespace(focus_mode_enabled=focus_mode_enabled),
        focus_cohort=list(cohort),
        daily_batch=list(daily_batch),
    )


def _allowed(symbol, **kwargs):
    bot = _bot(**kwargs)
    return AutoTrader.stock_entry_symbol_allowed.__get__(bot)(symbol)


class StockEntrySymbolAllowedTests(unittest.TestCase):
    COHORT = ["ACN", "MRNA", "PLTR"]
    BATCH = ["GOOGL", "NVDA", "AAPL", "ACN", "MRNA", "RIVN", "PLTR", "GME"]

    def test_the_live_xom_entry_is_now_refused(self):
        self.assertFalse(
            _allowed("XOM", cohort=self.COHORT, daily_batch=self.BATCH)
        )

    def test_the_live_meds_entry_is_now_refused(self):
        """The $4 name from 10:27, for completeness - it fails both the
        price band and membership.
        """
        self.assertFalse(
            _allowed("MEDS", cohort=self.COHORT, daily_batch=self.BATCH)
        )

    def test_a_cohort_name_is_allowed(self):
        self.assertTrue(
            _allowed("ACN", cohort=self.COHORT, daily_batch=self.BATCH)
        )

    def test_a_daily_batch_name_is_allowed_before_the_cohort_locks(self):
        """The batch is the screened set pre-lock; it enforces the same
        gap/RVOL/volume/price gates.
        """
        self.assertTrue(_allowed("GOOGL", daily_batch=self.BATCH))

    def test_nothing_screened_refuses_everything(self):
        """Defence in depth. stock_entries_suspended already sits out in
        this state, but that guard has now been wrong twice, so this does
        not rely on it.
        """
        self.assertFalse(_allowed("GOOGL"))
        self.assertFalse(_allowed("XOM"))

    def test_matching_is_case_insensitive(self):
        self.assertTrue(_allowed("acn", cohort=self.COHORT))

    def test_focus_mode_off_allows_the_ordinary_scanner(self):
        """With focus mode off there is no cohort to respect and the
        multi-symbol scanner is the intended behaviour.
        """
        self.assertTrue(_allowed("XOM", focus_mode_enabled=False))


class EveryEntryPathIsGatedTests(unittest.TestCase):
    """A per-symbol gate applied at three of four entry paths is the
    same bug with a smaller blast radius. These are the four sites that
    check stock_entries_suspended, so each must check this too.
    """

    PATHS = (
        "src/webull_bot/trading/stocks/stock_symbol_processing.py",
        "src/webull_bot/trading/stocks/stock_symbol_volatility_scalp.py",
    )

    def test_every_suspension_check_is_paired_with_the_symbol_gate(self):
        from pathlib import Path

        repo = Path(__file__).resolve().parent.parent
        for relative in self.PATHS:
            text = (repo / relative).read_text(encoding="utf-8")
            suspended = text.count("not self.stock_entries_suspended()")
            allowed = text.count("self.stock_entry_symbol_allowed(symbol)")
            self.assertEqual(
                suspended, allowed,
                f"{relative}: {suspended} suspension check(s) but "
                f"{allowed} symbol gate(s) - an entry path that checks "
                f"only the first can still buy an unscreened name",
            )
            self.assertGreater(suspended, 0, f"{relative}: no gate found")


if __name__ == "__main__":
    unittest.main()
