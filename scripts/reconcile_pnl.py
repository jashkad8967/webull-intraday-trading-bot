"""Reconcile recorded P&L against the account's actual balance change.

The account balance is the only number that cannot lie. Everything else -
trade_history, pnl_today, the PROFIT/STOP log lines - is written by the
bot about its own intentions, and the bot records a trade when an order is
SUBMITTED rather than when it fills.

Measured 2026-10-01:

    trade_history sum for the day   -$1.20
    actual balance change            -$4.20   (84.70 -> 80.50)
    overstatement                    +$3.00

Two sell orders raced on one PFE contract. The first sold it; the second
hit a position that was already flat. Both wrote a PROFIT record of
+$2.93, so the day looked $3 better than it was. Same class as the Ford
profit that never filled.

The bias is what makes this worth a script rather than a one-off check:
phantom records are always PROFITS, never losses, because a rejected
order never produces a loss to record. So every measurement taken from
trade_history is optimistic by an unknown amount - including the
-$44.84 four-session figure that was used to reason about whether the
strategy worked at all. The true figure was -$45.28.

Per the project's standing rule, a sanity check means reconciling real
numbers against the broker, not running local tests.

    python scripts/reconcile_pnl.py              # today
    python scripts/reconcile_pnl.py 2026-09-30   # a specific session

Exits non-zero when the gap exceeds the tolerance, so this can gate a
report or run from the EOD sweep.
"""

import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

# Fees, fractional-share dust and mid-cycle marks mean these never match
# to the cent. $0.50 is wide enough to absorb that and far narrower than
# a phantom option record, which is a whole premium.
TOLERANCE = Decimal("0.50")


def _data_dir() -> Path:
    for candidate in (
        "/var/data",
        "/var/lib/docker/volumes/webull-trading-data/_data",
    ):
        if Path(candidate).is_dir():
            return Path(candidate)
    return Path("/var/data")


def _recorded_pnl(data: Path, day: date):
    """Sum of every closing record the bot wrote for that day."""
    try:
        trades = json.loads(
            (data / "conf" / "trade_history.json").read_text(encoding="utf-8")
        ).get("trades", [])
    except Exception as exc:
        print(f"trade_history unreadable: {exc}")
        return None, []
    rows = []
    for trade in trades:
        stamp = trade.get("time")
        if not stamp:
            continue
        if datetime.fromtimestamp(stamp).date() != day:
            continue
        if trade.get("pnl") is None:
            continue
        rows.append(trade)
    total = sum(Decimal(str(t.get("pnl"))) for t in rows)
    return total, rows


def _actual_change(data: Path):
    """First and last balance the status snapshot recorded today.

    balance_history is written by the live account poll, so its endpoints
    are broker truth rather than the bot's own bookkeeping.
    """
    try:
        status = json.loads((data / "status.json").read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"status.json unreadable: {exc}")
        return None, None, None
    history = status.get("balance_history") or []
    points = [
        Decimal(str(entry["balance"]))
        for entry in history
        if entry.get("balance") not in (None, "")
    ]
    if len(points) < 2:
        return None, None, None
    return points[-1] - points[0], points[0], points[-1]


def main(argv) -> int:
    data = _data_dir()
    day = date.fromisoformat(argv[0]) if argv else date.today()

    recorded, rows = _recorded_pnl(data, day)
    actual, opening, closing = _actual_change(data)

    print(f"session {day}   (state volume {data})\n")
    if recorded is None:
        return 2
    print(f"{'closing records':<24} {len(rows)}")
    for trade in sorted(rows, key=lambda t: t.get("time", 0)):
        print(f"  {datetime.fromtimestamp(trade['time']).strftime('%H:%M:%S')} "
              f"{str(trade.get('instrument_type'))[:6]:<6} "
              f"{str(trade.get('symbol'))[:22]:<22} "
              f"{str(trade.get('action'))[:11]:<11} "
              f"{Decimal(str(trade.get('pnl'))):+.2f}")
    print(f"\n{'recorded P&L':<24} {recorded:+.2f}")

    if actual is None:
        print(f"{'actual balance change':<24} unavailable "
              f"(need 2+ balance samples)")
        print("\nCannot reconcile. Recorded P&L is NOT trustworthy on its "
              "own - it is written at order submission, not at fill.")
        return 2

    gap = recorded - actual
    print(f"{'actual balance change':<24} {actual:+.2f}   "
          f"({opening} -> {closing})")
    print(f"{'gap':<24} {gap:+.2f}")

    if abs(gap) <= TOLERANCE:
        print("\nOK - records agree with the account.")
        return 0

    direction = "OVERSTATES" if gap > 0 else "understates"
    print(f"\nMISMATCH: trade_history {direction} the day by "
          f"${abs(gap):.2f}.")
    if gap > 0:
        print(
            "An overstatement is the expected direction: a trade is "
            "recorded when its order is SUBMITTED, so a duplicate or "
            "rejected SELL still writes a PROFIT, while a rejected order "
            "never writes a loss. Look for two closing records on one "
            "contract with different order_ids - that is the stale-"
            "position race, and the second one is phantom.\n"
            "Trust the balance, not the records."
        )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
