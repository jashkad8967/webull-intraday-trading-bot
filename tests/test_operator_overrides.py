"""A standing operator veto, read BEFORE the bot decides.

By explicit request: "I want you to be able to activate in case the bot
is making a wrong decision so you can override it, so it has to be
before the bot makes a decision not after."

The design note matters as much as the code, because the obvious version
is wrong and will be re-proposed. Having the bot publish a proposed trade
and wait for approval cannot work: entries are decided inside a loop
running at poll_seconds (0.5s) and a reply takes tens of seconds. Every
entry would stall for the timeout and then proceed anyway, because the
alternative - fail closed - stops the account trading the moment nobody
is watching. Pure latency on the fastest-moving setups, and no veto at
all outside a session.

So the veto is a FILE. Read on every entry evaluation, costs nothing,
applies whether or not anyone is present, takes effect on the next
evaluation rather than the next restart.

Distinct from the two stores it resembles:
  * wash_sale_blocks - a tax rule with its own expiry
  * CommandQueue      - one-off ACTIONS (sell/buy/cancel), drained
This is standing policy until the operator changes it.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from webull_bot.operator_overrides import OperatorOverrides
from webull_bot.trading.guards.focus_mode_guards import new_entries_blocked


class OperatorOverridesTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "conf" / "operator_overrides.json"

    def _store(self):
        return OperatorOverrides(str(self.path))

    def _write(self, payload):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def test_no_file_means_no_vetoes(self):
        """Absence is the normal state - overrides are the exception."""
        store = self._store()
        self.assertFalse(store.entries_halted())
        self.assertFalse(store.symbol_blocked("NVDA"))

    def test_a_blocked_symbol_is_refused(self):
        self._write({"blocked_symbols": ["ACN", "GOOGL"]})
        store = self._store()
        self.assertTrue(store.symbol_blocked("ACN"))
        self.assertTrue(store.symbol_blocked("acn"))
        self.assertFalse(store.symbol_blocked("NVDA"))

    def test_halt_entries_stops_everything(self):
        self._write({"halt_entries": True})
        self.assertTrue(self._store().entries_halted())

    def test_an_edit_takes_effect_without_a_restart(self):
        """The whole point of a file over a config value: a veto set
        mid-session must apply to the very next evaluation.
        """
        store = self._store()
        self.assertFalse(store.symbol_blocked("ACN"))
        self._write({"blocked_symbols": ["ACN"]})
        self.assertTrue(store.symbol_blocked("ACN"))
        self._write({"blocked_symbols": []})
        self.assertFalse(store.symbol_blocked("ACN"))

    def test_a_corrupt_file_keeps_the_last_valid_vetoes(self):
        """Fails open rather than halting, but does NOT silently drop a
        veto that is still meant to apply. A hand-edited JSON file is
        exactly where a syntax error comes from, and halting all trading
        over a typo would be a worse failure than the mistakes this
        prevents.
        """
        self._write({"blocked_symbols": ["ACN"], "halt_entries": True})
        store = self._store()
        self.assertTrue(store.symbol_blocked("ACN"))
        self.path.write_text("{not json", encoding="utf-8")
        self.assertTrue(store.symbol_blocked("ACN"))
        self.assertTrue(store.entries_halted())

    def test_a_non_object_file_is_rejected_safely(self):
        self._write(["ACN"])
        store = self._store()
        self.assertFalse(store.symbol_blocked("ACN"))
        self.assertFalse(store.entries_halted())

    def test_a_removed_file_clears_the_vetoes(self):
        self._write({"blocked_symbols": ["ACN"]})
        store = self._store()
        self.assertTrue(store.symbol_blocked("ACN"))
        self.path.unlink()
        self.assertFalse(store.symbol_blocked("ACN"))

    def test_set_writes_atomically_and_reads_back(self):
        store = self._store()
        payload = store.set(blocked_symbols=["acn", "googl"],
                            halt_entries=False, note="failing gaps")
        self.assertEqual(payload["blocked_symbols"], ["ACN", "GOOGL"])
        self.assertTrue(store.symbol_blocked("GOOGL"))
        # No temp files left behind by the atomic replace.
        leftovers = list(self.path.parent.glob(".overrides-*"))
        self.assertEqual(leftovers, [])

    def test_set_merges_rather_than_replacing(self):
        store = self._store()
        store.set(blocked_symbols=["ACN"], note="keep me")
        store.set(halt_entries=True)
        self.assertTrue(store.symbol_blocked("ACN"))
        self.assertTrue(store.entries_halted())
        self.assertEqual(
            json.loads(self.path.read_text())["note"], "keep me"
        )


class NewEntriesBlockedIntegrationTests(unittest.TestCase):
    """The halt must reach the gate the entry paths already consult, so
    there is no path that opens risk without seeing it.
    """

    def _bot(self, halted):
        return SimpleNamespace(
            config=SimpleNamespace(
                option_entries_halted=False, focus_mode_enabled=True
            ),
            profit_throttle_armed=False,
            profit_floor_breached=False,
            operator_overrides=SimpleNamespace(entries_halted=lambda: halted),
        )

    def test_an_operator_halt_blocks_new_entries(self):
        self.assertTrue(new_entries_blocked(self._bot(True)))

    def test_no_halt_leaves_entries_open(self):
        self.assertFalse(new_entries_blocked(self._bot(False)))

    def test_a_bot_without_the_store_still_works(self):
        """Fixtures and older call sites must not crash on the new
        attribute - the guard is read defensively.
        """
        bot = self._bot(False)
        del bot.operator_overrides
        self.assertFalse(new_entries_blocked(bot))


class EveryStockEntryPathIsVetoedTests(unittest.TestCase):
    PATHS = (
        "src/webull_bot/trading/stocks/stock_symbol_processing.py",
        "src/webull_bot/trading/stocks/stock_symbol_volatility_scalp.py",
    )

    def test_every_entry_site_checks_the_symbol_veto(self):
        repo = Path(__file__).resolve().parent.parent
        for relative in self.PATHS:
            text = (repo / relative).read_text(encoding="utf-8")
            sites = text.count("self.stock_entry_symbol_allowed(symbol)")
            vetoes = text.count("operator_overrides.symbol_blocked(symbol)")
            self.assertEqual(
                sites, vetoes,
                f"{relative}: {sites} entry site(s) but {vetoes} veto "
                f"check(s) - an unvetoed path can still buy a blocked name",
            )

    def test_the_option_entry_path_checks_it_too(self):
        repo = Path(__file__).resolve().parent.parent
        text = (repo / "src/webull_bot/trading/options/option_entry_exit.py"
                ).read_text(encoding="utf-8")
        self.assertIn("operator_overrides.symbol_blocked(underlying)", text)


if __name__ == "__main__":
    unittest.main()
