"""The fractional pre-close sweep must not hammer a flat account.

Live 2026-10-01 14:50, with the account FLAT and nothing to close:

    14:50:46  CLOSE | submitted=0 | remaining=0
    14:50:47  CLOSE | fractional pre-close sweep failed | HTTP 429
    14:51:02  CLOSE | submitted=0 | remaining=0
    14:51:15  CLOSE | submitted=0 | remaining=0
    14:51:16  CLOSE | fractional pre-close sweep failed | HTTP 429

Four failures in 75 seconds, and it would have run that way to the close.
Two separate defects:

  * the "no fractional positions" early return sat BELOW the live
    api.positions() call, so a flat account still spent a request every
    eod_retry_seconds just to learn it was still flat
  * a 429 returned to the NORMAL cadence, so the sweep kept retrying into
    its own rate limit

Same shape as the BMEA incident the price-sanity cooldown was written for
(~570 rejections over five hours with zero backoff). The cost is not just
noise: this account's API budget is shared with the live trading loop, so
a sweep burning requests on nothing competes with order placement.
"""

import time
import unittest
from decimal import Decimal
from types import SimpleNamespace

from webull_bot.trading.sweeps.fractional_pre_close_sweep import (
    close_fractional_positions_before_core_close,
)


class FractionalSweepTests(unittest.TestCase):
    def _bot(self, cached, raises=None):
        calls = []

        def _positions():
            calls.append(1)
            if raises:
                raise Exception(raises)
            return list(cached or [])

        bot = SimpleNamespace(
            last_fractional_sweep=0.0,
            config=SimpleNamespace(eod_retry_seconds=15),
            cached_positions=cached,
            is_fractional_quantity=lambda q: 0 < q < 1,
            api=SimpleNamespace(positions=_positions),
        )
        return bot, calls

    def _frac(self):
        return [{
            "instrument_type": "EQUITY", "symbol": "NVDA",
            "quantity": "0.1047", "cost_price": "231.78",
        }]

    def test_a_flat_account_spends_no_api_call(self):
        """The live bug: positions() was called every cycle on a flat
        account, and each call could 429.
        """
        bot, calls = self._bot(cached=[])
        close_fractional_positions_before_core_close(bot)
        self.assertEqual(calls, [], "spent a live positions() call while flat")

    def test_whole_share_only_also_spends_no_api_call(self):
        bot, calls = self._bot(cached=[{
            "instrument_type": "EQUITY", "symbol": "IBM", "quantity": "3",
            "cost_price": "200",
        }])
        close_fractional_positions_before_core_close(bot)
        self.assertEqual(calls, [])

    def test_a_fractional_position_still_triggers_the_live_call(self):
        """The pre-check must not disable the sweep it is protecting."""
        bot, calls = self._bot(cached=self._frac())
        try:
            close_fractional_positions_before_core_close(bot)
        except Exception:
            pass  # downstream of positions(); the call itself is the point
        self.assertEqual(len(calls), 1)

    def test_a_missing_cache_falls_through_to_the_live_call(self):
        """Degrade to the original behaviour rather than silently never
        sweeping - a bot or fixture without cached_positions must still
        work.
        """
        bot, calls = self._bot(cached=self._frac())
        del bot.cached_positions
        try:
            close_fractional_positions_before_core_close(bot)
        except Exception:
            pass
        self.assertEqual(len(calls), 1)

    def test_a_rate_limit_backs_off_beyond_the_normal_cadence(self):
        bot, calls = self._bot(
            cached=self._frac(),
            raises="HTTP Status: 429, Code: TOO_MANY_REQUESTS",
        )
        close_fractional_positions_before_core_close(bot)
        self.assertEqual(len(calls), 1)
        # A second attempt one normal interval later must NOT fire.
        bot.last_fractional_sweep -= float(bot.config.eod_retry_seconds)
        close_fractional_positions_before_core_close(bot)
        self.assertEqual(
            len(calls), 1,
            "retried into its own rate limit at the normal cadence",
        )

    def test_a_non_rate_limit_failure_keeps_the_normal_cadence(self):
        """Backoff is specific to 429. A transient network error should
        still be retried promptly - the position genuinely needs closing
        before core session ends.
        """
        bot, calls = self._bot(
            cached=self._frac(), raises="Connection reset by peer",
        )
        close_fractional_positions_before_core_close(bot)
        bot.last_fractional_sweep -= float(bot.config.eod_retry_seconds)
        close_fractional_positions_before_core_close(bot)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
