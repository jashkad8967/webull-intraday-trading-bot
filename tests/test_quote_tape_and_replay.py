"""The quote tape and its replayer.

These exist because every strategy parameter changed on 2026-09-24/25
was validated by losing real money. The 5% stop shipped on the
reasoning "median win 5.3%, win rate 67% -> +1.9% expectancy per
trade" - arithmetic that held win rate CONSTANT while changing the one
thing that determines it. Five stop-outs in 37 minutes falsified it and
cost 48% of the account.

Trade history says what DID happen. It cannot say whether a different
stop would have fired, because that depends on the PRICE PATH between
entry and exit. The tape records the path; the replayer re-runs it
through the REAL option_decision.
"""

import importlib.util
import json
import logging
import shutil
import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from webull_bot.quote_tape import QuoteTape

REPO = Path(__file__).resolve().parent.parent


def _load_replay_module():
    """The replayer is a script, not a package module."""
    path = REPO / "scripts" / "replay_option_tape.py"
    spec = importlib.util.spec_from_file_location("replay_option_tape", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["replay_option_tape"] = module
    spec.loader.exec_module(module)
    return module


class QuoteTapeTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path("tests/.generated_tape")
        shutil.rmtree(self.dir, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _tape(self, interval=0.0, retention=5):
        return QuoteTape(
            str(self.dir),
            timezone.utc,
            logging.getLogger("test-tape"),
            interval_seconds=interval,
            retention_days=retention,
        )

    def _samples(self, symbol="GME261009C00024000"):
        return [(symbol, 0.80, 0.85, 0.82, 0.75, 3)]

    def test_it_writes_one_line_per_sample(self):
        tape = self._tape()
        self.addCleanup(tape.close)
        self.assertEqual(tape.record(self._samples()), 1)
        files = list(self.dir.glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        row = json.loads(files[0].read_text(encoding="utf-8").strip())
        self.assertEqual(row["s"], "GME261009C00024000")
        self.assertEqual(row["b"], 0.80)
        self.assertEqual(row["c"], 0.75)

    def test_it_records_delta_and_the_underlying_price(self):
        """Added 2026-10-01, after this tape recorded a perfect price
        path and still could not explain the session's worst trade.

        PFE261009C00028000 lost $7.07 in 150 seconds because a 10% stop
        on a 0.62-delta contract is a 0.29% move in PFE. An option is
        levered to its underlying by premium/(delta x spot), so a tape
        holding neither term cannot replay a correctly scaled stop - it
        can only replay the same mis-scaled percentage that caused the
        loss, and would have confirmed the broken design.
        """
        tape = self._tape()
        self.addCleanup(tape.close)
        tape.record(
            [("PFE261009C00028000", 0.47, 0.50, 0.49, 0.50, 1, 0.6187, 28.21)]
        )
        row = json.loads(
            next(self.dir.glob("*.jsonl")).read_text(encoding="utf-8").strip()
        )
        self.assertEqual(row["d"], 0.6187)
        self.assertEqual(row["u"], 28.21)

    def test_a_six_tuple_caller_still_works(self):
        """Backwards compatibility both ways: older callers and every
        tape file already on disk.
        """
        tape = self._tape()
        self.addCleanup(tape.close)
        self.assertEqual(tape.record(self._samples()), 1)
        row = json.loads(
            next(self.dir.glob("*.jsonl")).read_text(encoding="utf-8").strip()
        )
        self.assertIsNone(row["d"])
        self.assertIsNone(row["u"])

    def test_the_interval_throttles_repeat_samples(self):
        """The fast loop polls at 0.5s. Recording every poll would be
        four times the disk for no extra resolution on any stop or
        trail question, on a host whose disk has already filled once.
        """
        tape = self._tape(interval=60.0)
        self.addCleanup(tape.close)
        self.assertEqual(tape.record(self._samples()), 1)
        self.assertEqual(tape.record(self._samples()), 0)
        self.assertEqual(tape.record(self._samples()), 0)

    def test_forget_lets_a_re_entry_record_immediately(self):
        tape = self._tape(interval=60.0)
        self.addCleanup(tape.close)
        tape.record(self._samples())
        tape.forget("GME261009C00024000")
        self.assertEqual(tape.record(self._samples()), 1)

    def test_a_broken_sample_never_raises(self):
        """This runs inside the loop that submits stops. A recorder that
        can raise would trade a measurement tool for the thing it is
        meant to protect.
        """
        tape = self._tape()
        self.addCleanup(tape.close)
        tape.record([("SYM", "not-a-number", None, None, None, None)])
        tape.record([(None, 1, 1, 1, 1, 1)])
        tape.record("not-iterable-of-tuples")

    def test_a_bad_directory_never_raises(self):
        """The property that matters is that it cannot throw into the
        exit loop - NOT that it writes nothing. Asserting a write count
        here would only be asserting how the OS treats an odd path
        (POSIX refuses it, Windows happily creates it), which says
        nothing about the recorder.
        """
        tape = QuoteTape(
            "/nonexistent/path/that/cannot/exist",
            timezone.utc,
            logging.getLogger("test-tape"),
        )
        self.addCleanup(tape.close)
        try:
            tape.record(self._samples())
        except Exception as exc:  # pragma: no cover - the failure case
            self.fail(f"recorder raised into the caller: {exc!r}")


class ReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.replay = _load_replay_module()

    def setUp(self):
        self.dir = Path("tests/.generated_replay")
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _write(self, rows, name="tape.jsonl"):
        path = self.dir / name
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        return path

    def _path(self, symbol, cost, bids, start=1790000000.0, step=10, qty=1):
        return [
            {
                "t": start + i * step,
                "s": symbol,
                "b": bid,
                "a": round(bid + 0.05, 2),
                "l": bid,
                "c": cost,
                "q": qty,
            }
            for i, bid in enumerate(bids)
        ]

    def _config(self, **overrides):
        return self.replay.build_config(**overrides)

    def test_a_falling_position_stops_out(self):
        rows = self._path("X", 1.00, [1.00, 0.97, 0.94, 0.91, 0.88, 0.85])
        result = self.replay.replay_position(
            self._config(option_stop_loss_percent=Decimal("0.10")), rows
        )
        self.assertEqual(result["action"], "LOSS")
        self.assertLess(result["pnl"], 0)

    def test_a_tighter_stop_fires_sooner_on_the_same_path(self):
        """The whole point: the same real ticks, two configurations."""
        rows = self._path("X", 1.00, [1.00, 0.97, 0.94, 0.91, 0.88, 0.85])
        loose = self.replay.replay_position(
            self._config(option_stop_loss_percent=Decimal("0.20")), rows
        )
        tight = self.replay.replay_position(
            self._config(option_stop_loss_percent=Decimal("0.05")), rows
        )
        self.assertLess(tight["held_seconds"], loose["held_seconds"])

    def test_a_tighter_stop_can_convert_a_WINNER_into_a_loser(self):
        """The term the arithmetic that justified 5% left out.

        A path that dips before it works: a wide stop rides the dip and
        banks the gain, a tight stop is shaken out at the bottom. This
        is exactly what happened live - five stop-outs in 37 minutes on
        setups that had been winning the day before.
        """
        rows = self._path("X", 1.00, [1.00, 0.94, 0.92, 1.05, 1.12, 1.15])
        tight = self.replay.replay_position(
            self._config(option_stop_loss_percent=Decimal("0.05")), rows
        )
        loose = self.replay.replay_position(
            self._config(option_stop_loss_percent=Decimal("0.20")), rows
        )
        self.assertEqual(tight["action"], "LOSS")
        self.assertLess(tight["pnl"], 0)
        self.assertGreater(loose["pnl"], 0)

    def test_a_position_that_never_triggers_closes_at_the_bell(self):
        rows = self._path("X", 1.00, [1.00, 1.01, 1.02, 1.01, 1.02])
        result = self.replay.replay_position(
            self._config(option_stop_loss_percent=Decimal("0.20")), rows
        )
        self.assertEqual(result["action"], "EOD")

    def test_the_peak_is_tracked_on_the_bid(self):
        """The live fast loop rides the trail off sell_realizable_price
        (the bid). Tracking anything richer here would make the trail
        look better on paper than it can be in practice.
        """
        rows = self._path("X", 1.00, [1.00, 1.30, 1.10])
        result = self.replay.replay_position(
            self._config(
                option_stop_loss_percent=Decimal("0.50"),
                option_take_profit_percent=Decimal("0.90"),
            ),
            rows,
        )
        self.assertEqual(result["action"], "PROFIT")
        self.assertIn("trail", result["reason"].lower())

    def test_a_gap_starts_a_new_position(self):
        """One contract can be held twice in a day. Stitching two
        separate holdings into one path would invent a trade that never
        existed.
        """
        first = self._path("X", 1.00, [1.00, 1.02], start=1790000000.0)
        second = self._path("X", 1.50, [1.50, 1.52], start=1790000900.0)
        runs = self.replay.split_positions(first + second)
        self.assertEqual(len(runs), 2)

    def test_a_changed_cost_basis_starts_a_new_position(self):
        rows = self._path("X", 1.00, [1.00, 1.02])
        rows += self._path("X", 2.00, [2.00, 2.02], start=1790000030.0)
        runs = self.replay.split_positions(rows)
        self.assertEqual(len(runs), 2)

    def test_load_tape_skips_unparseable_and_costless_rows(self):
        path = self._write(
            self._path("X", 1.00, [1.00, 1.02])
            + [{"t": 1, "s": "Y", "b": 1.0, "c": 0}]
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        by_symbol = self.replay.load_tape(path)
        self.assertIn("X", by_symbol)
        self.assertNotIn("Y", by_symbol)

    def test_summarise_reports_win_rate_and_ratio(self):
        results = [
            {"pnl": Decimal("10")},
            {"pnl": Decimal("10")},
            {"pnl": Decimal("-5")},
        ]
        summary = self.replay.summarise(results)
        self.assertEqual(summary["wins"], 2)
        self.assertEqual(summary["losses"], 1)
        self.assertEqual(summary["total"], Decimal("15"))
        self.assertEqual(summary["ratio"], Decimal("2"))


if __name__ == "__main__":
    unittest.main()
