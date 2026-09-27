"""Replay a recorded option quote tape through the REAL decision
function under a chosen configuration.

Answers the question that cost 48% of the account on 2026-09-25:
"what would a different stop have done to the trades we actually took?"

Trade history says what happened - entry, exit, P&L. It cannot answer
that, because the answer depends on the PRICE PATH between entry and
exit. QuoteTape records the path; this replays it.

The 5% stop was shipped on this reasoning: "median win 5.3%, win rate
67% -> +1.9% expectancy per trade". That arithmetic held win rate
CONSTANT while changing the very thing that determines it. Five
stop-outs in 37 minutes falsified it. This script would have falsified
it in seconds, because it re-runs each position's real ticks and
reports how many of the winners the tighter stop converts into losers -
the term the arithmetic omitted.

Usage:
  python scripts/replay_option_tape.py TAPE.jsonl
  python scripts/replay_option_tape.py TAPE.jsonl --stop 0.05
  python scripts/replay_option_tape.py TAPE.jsonl --sweep-stop 0.03,0.05,0.10,0.20

Reads nothing live and places no orders.
"""
import argparse
import json
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, "src")

from webull_bot.config import Settings  # noqa: E402
from webull_bot.strategy_logic.decision.stock_option_decision import (  # noqa: E402
    option_decision,
)


def load_tape(path: Path):
    """Group tape samples into per-position price paths.

    A single contract can be held more than once in a day, so a gap
    longer than `gap_seconds` starts a new position rather than
    stitching two separate trades into one impossible path.
    """
    by_symbol = defaultdict(list)
    bad = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                bad += 1
                continue
            if not row.get("s") or row.get("c") in (None, 0):
                continue
            by_symbol[row["s"]].append(row)
    if bad:
        print(f"note: skipped {bad} unparseable line(s)")
    return by_symbol


def split_positions(rows, gap_seconds=300):
    """One contract, possibly several separate holdings."""
    runs = []
    current = []
    for row in sorted(rows, key=lambda r: r.get("t", 0)):
        if current and row["t"] - current[-1]["t"] > gap_seconds:
            runs.append(current)
            current = []
        # A changed cost basis is a different position, not a drift.
        if current and abs(row.get("c", 0) - current[-1].get("c", 0)) > 1e-9:
            runs.append(current)
            current = []
        current.append(row)
    if current:
        runs.append(current)
    return runs


def replay_position(config, rows):
    """Walk one position's real ticks through option_decision.

    The peak is tracked on the BID, exactly as the live fast loop does
    (sell_realizable_price), so the profit-lock trail sees the same
    high-water mark it would have seen live. Anything else would make
    the trail look better on paper than it can be in practice.
    """
    strategy = SimpleNamespace(config=config)
    cost = Decimal(str(rows[0]["c"]))
    quantity = Decimal(str(rows[0].get("q") or 1))
    started = rows[0]["t"]
    peak = None
    for row in rows:
        bid = row.get("b")
        if bid is None or bid <= 0:
            continue
        price = Decimal(str(bid))
        peak = price if peak is None else max(peak, price)
        decision = option_decision(
            strategy,
            price,
            quantity,
            cost,
            30,  # dte: far enough that the expiry exit never fires
            seconds_since_entry=row["t"] - started,
            peak_price=peak,
        )
        if decision.action != "HOLD":
            exit_price = decision.target_price or price
            fee = config.option_sell_fee_per_contract * quantity
            pnl = (exit_price - cost) * quantity * 100 - fee
            return {
                "action": decision.action,
                "reason": decision.reason,
                "cost": cost,
                "exit": exit_price,
                "pnl": pnl,
                "held_seconds": row["t"] - started,
                "quantity": quantity,
            }
    # Never triggered - the position would still be open at the bell and
    # the EOD close would take it at whatever the last bid was.
    last_bid = next(
        (Decimal(str(r["b"])) for r in reversed(rows) if r.get("b")), cost
    )
    fee = config.option_sell_fee_per_contract * quantity
    return {
        "action": "EOD",
        "reason": "never triggered - closed at the bell",
        "cost": cost,
        "exit": last_bid,
        "pnl": (last_bid - cost) * quantity * 100 - fee,
        "held_seconds": rows[-1]["t"] - started,
        "quantity": quantity,
    }


def summarise(results):
    wins = [r for r in results if r["pnl"] > 0]
    losses = [r for r in results if r["pnl"] < 0]
    total = sum(r["pnl"] for r in results)
    out = {
        "n": len(results),
        "wins": len(wins),
        "losses": len(losses),
        "total": total,
        "avg_win": (sum(r["pnl"] for r in wins) / len(wins)) if wins else None,
        "avg_loss": (
            sum(r["pnl"] for r in losses) / len(losses) if losses else None
        ),
    }
    if wins and losses:
        out["win_rate"] = Decimal(len(wins)) / Decimal(len(results))
        out["ratio"] = abs(out["avg_win"] / out["avg_loss"])
    return out


def build_config(**overrides):
    values = {}
    for key, value in overrides.items():
        if value is not None:
            values[key] = value
    return Settings(_env_file=None, **values)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tape", type=Path)
    parser.add_argument("--stop", type=Decimal, default=None)
    parser.add_argument("--take-profit", type=Decimal, default=None)
    parser.add_argument("--trail-arm", type=Decimal, default=None)
    parser.add_argument(
        "--sweep-stop",
        help="comma-separated stop values to compare, e.g. 0.03,0.05,0.10",
    )
    args = parser.parse_args()

    if not args.tape.exists():
        print(f"no such tape: {args.tape}")
        return 1

    by_symbol = load_tape(args.tape)
    positions = []
    for symbol, rows in by_symbol.items():
        for run in split_positions(rows):
            if len(run) >= 2:
                positions.append((symbol, run))
    if not positions:
        print("tape contains no usable positions")
        return 1
    print(f"tape: {args.tape.name} | {len(positions)} position(s) reconstructed")
    print()

    sweep = (
        [Decimal(v) for v in args.sweep_stop.split(",")]
        if args.sweep_stop
        else [args.stop]
    )

    rows_out = []
    for stop in sweep:
        config = build_config(
            option_stop_loss_percent=stop,
            option_take_profit_percent=args.take_profit,
            profit_lock_arm_percent=args.trail_arm,
        )
        results = [replay_position(config, run) for _, run in positions]
        summary = summarise(results)
        rows_out.append((config.option_stop_loss_percent, summary, results))

    header = (
        f"{'stop':>6} {'n':>4} {'W':>4} {'L':>4} {'total':>10} "
        f"{'avg win':>9} {'avg loss':>9} {'rate':>6}"
    )
    print(header)
    print("-" * len(header))
    for stop, summary, _ in rows_out:
        rate = summary.get("win_rate")
        print(
            f"{stop:>6} {summary['n']:>4} {summary['wins']:>4} "
            f"{summary['losses']:>4} ${summary['total']:>9.2f} "
            f"{('$%.2f' % summary['avg_win']) if summary['avg_win'] else '-':>9} "
            f"{('$%.2f' % summary['avg_loss']) if summary['avg_loss'] else '-':>9} "
            f"{(f'{rate*100:.0f}%') if rate else '-':>6}"
        )

    # Per-trade detail for the first (or only) configuration.
    print()
    stop, _, results = rows_out[0]
    print(f"per-position detail at stop={stop}:")
    print(
        f"  {'contract':26} {'action':7} {'cost':>6} {'exit':>6} "
        f"{'pnl':>9} {'held':>7}  why"
    )
    for (symbol, _), result in zip(positions, results):
        print(
            f"  {symbol:26} {result['action']:7} {result['cost']:>6} "
            f"{result['exit']:>6} ${result['pnl']:>8.2f} "
            f"{result['held_seconds']:>6.0f}s  {result['reason'][:38]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
