"""Liquid option underlyings are reachable regardless of the cohort.

Measured live 2026-10-01 11:14 by surveying real chains at a matched
~$0.55 premium (scripts/survey_option_hurdles.py):

    SPY261016P00700000   0.55/0.56   spread 0.01   hurdle  4.5%
    IWM261016P00258000   0.49/0.50   spread 0.01   hurdle  5.1%
    PLTR261009P00172500  0.59/0.65   spread 0.06   hurdle  8.5%
    ACN261016P00185000   0.55/0.75   spread 0.20   hurdle 21.8%

Identical premium, incompatible economics - and ACN was in that day's
LOCKED COHORT. A 21.8% round-trip cost against a 10% stop cannot be won
by any exit rule; SPY at 4.5% leaves 5.5% of usable room.

The cause is structural rather than a bad cohort pick: the cohort gates
screen on gap >= 2% and RVOL >= 2, which measure SHARE volatility.
Index ETFs essentially never clear them, so the only chains the bot
could ever reach were the wide single-name ones. Chain liquidity and
share volatility are different properties and selection measured only
the second.

Three separate places had to change, and leaving any one of them out
produces a silent dead end rather than an error:

  * entry gating   - or the contract is rejected as "not in today's
                     focus cohort"
  * discovery      - or no contract is ever fetched, and the gate looks
                     like it is refusing everything
  * the QUOTE set  - or option_direction_signal's EMA(3/8) is never fed
                     and the underlying returns HOLD forever

All three are indistinguishable from "no setup today" in the logs.
"""

import unittest
from collections import deque
from decimal import Decimal
from types import SimpleNamespace


class LiquidUnderlyingScanScopeTests(unittest.TestCase):
    """The quote set, which is the failure mode with no error message."""

    def _prepare(self, cohort=(), daily_batch=(),
                 liquid=("SPY", "QQQ", "IWM"), preset_prices=None):
        from webull_bot.bot import AutoTrader

        requested: list[list[str]] = []

        class FakeApi:
            @staticmethod
            def stock_quotes_resilient(symbols, category):
                requested.append(list(symbols))
                return (
                    [{"symbol": s, "price": "50.00"} for s in symbols],
                    set(),
                )

            @staticmethod
            def quote_price(quote):
                return Decimal(str(quote["price"]))

            @staticmethod
            def option_quotes(symbols):
                return []

        contracts = [
            {
                "symbol": f"{sym}261016C00075000",
                "underlying_symbol": sym,
                "strike_price": "75",
                "expiration_date": "2026-10-16",
                "option_type": "CALL",
            }
            for sym in ("ZZZ",)
        ]
        bot = SimpleNamespace(
            option_contracts=contracts,
            option_cursor=0,
            vixy_history=deque(maxlen=30),
            api=FakeApi(),
            strategy=SimpleNamespace(
                open_position_count=lambda positions: 0,
                option_direction_signal=lambda key, price: "HOLD",
                rotating_batch=lambda symbols, cursor, size: (
                    list(symbols)[:size], 0
                ),
                prices=dict(preset_prices or {}),
            ),
            stop_loss_guard_active=lambda: False,
            focus_cohort=list(cohort),
            daily_batch=list(daily_batch),
            config=SimpleNamespace(
                option_batch_size=20,
                focus_mode_enabled=True,
                option_liquid_underlyings=tuple(liquid),
            ),
        )
        AutoTrader._prepare_option_scan_batch.__get__(bot)([])
        self._prices = bot.strategy.prices
        return requested

    def test_liquid_names_are_quoted_alongside_a_locked_cohort(self):
        requested = self._prepare(cohort=["ACN", "MRNA"])
        self.assertTrue(requested, "no quote batch was requested at all")
        asked = set(requested[0])
        self.assertIn("ACN", asked)
        for symbol in ("SPY", "QQQ", "IWM"):
            self.assertIn(
                symbol, asked,
                f"{symbol} never quoted -> its EMA stays cold -> HOLD forever",
            )

    def test_liquid_names_are_quoted_with_only_a_daily_batch(self):
        requested = self._prepare(daily_batch=["ACN"])
        asked = set(requested[0])
        self.assertIn("ACN", asked)
        self.assertIn("SPY", asked)

    def test_a_cohort_name_is_never_duplicated_into_the_quote_set(self):
        """A duplicate would waste a slot in a batch capped at 20."""
        requested = self._prepare(cohort=["SPY", "ACN"])
        asked = requested[0]
        self.assertEqual(asked.count("SPY"), 1, f"SPY duplicated in {asked}")

    def test_an_empty_liquid_list_restores_the_old_scoping(self):
        """The exemption must be switchable off cleanly."""
        requested = self._prepare(cohort=["ACN"], liquid=())
        self.assertEqual(set(requested[0]), {"ACN"})

    def test_the_share_price_is_recorded_so_chain_discovery_can_run(self):
        """The fourth silent dead end.

        _ensure_one_focus_symbol_contracts returns early unless
        strategy.prices holds the underlying's share price, and under
        focus mode the STOCK scan batch is forced to the cohort - so a
        liquid underlying had no price, therefore no wide chain, and the
        entry gate just kept logging "no focus cohort locked yet".

        Live 2026-10-01 11:30: SPY and QQQ held 2 contracts each from the
        background rotation and IWM zero, against PLTR's 4328.
        """
        self._prepare(cohort=["ACN"])
        for symbol in ("SPY", "QQQ", "IWM"):
            self.assertIn(
                symbol, self._prices,
                f"{symbol} has no share price -> wide chain discovery "
                f"returns early -> no contract is ever tradeable",
            )

    def test_a_cohort_name_keeps_the_price_the_stock_scan_gave_it(self):
        """Only fills gaps - never overwrites a price the share scanner
        already measured, which carries volume/activity context this
        path does not.
        """
        from decimal import Decimal as D

        bot_prices = {"SPY": D("999")}
        requested = self._prepare(cohort=["ACN"], preset_prices=bot_prices)
        self.assertEqual(self._prices["SPY"], D("999"))
        self.assertTrue(requested)


class LiquidUnderlyingConfigTests(unittest.TestCase):
    def test_the_shipped_default_is_the_three_most_liquid_chains(self):
        import os

        for key in ("WEBULL_APP_KEY", "WEBULL_APP_SECRET", "ACCOUNT_ID"):
            os.environ.setdefault(key, "x")
        from webull_bot.config import Settings

        settings = Settings()
        self.assertEqual(
            tuple(settings.option_liquid_underlyings), ("SPY", "QQQ", "IWM")
        )

    def test_the_hurdle_gate_still_applies_to_them(self):
        """Membership exempts cohort selection ONLY. A liquid name whose
        contract is quoted too wide must still be refused - QQQ's own
        cheap strikes measured 8.8%-20.0% on 2026-10-01, well past the
        5% ceiling, and were correctly rejected.
        """
        from decimal import Decimal

        from webull_bot.trading.guards.price_sanity import (
            option_entry_breakeven_room_ok,
        )

        # QQQ261016P00630000, real quote: 0.34/0.36 -> 8.8% hurdle.
        self.assertFalse(
            option_entry_breakeven_room_ok(
                Decimal("0.34"), Decimal("0.36"), Decimal("0.10"),
                Decimal("0.02"), Decimal("0.5"),
            )
        )
        # SPY261016P00700000, real quote: 0.55/0.56 -> 4.5% hurdle.
        self.assertTrue(
            option_entry_breakeven_room_ok(
                Decimal("0.55"), Decimal("0.56"), Decimal("0.10"),
                Decimal("0.02"), Decimal("0.5"),
            )
        )


if __name__ == "__main__":
    unittest.main()
