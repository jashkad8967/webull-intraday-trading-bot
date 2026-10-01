"""Print everything about the running bot's current state, in one pass.

Written 2026-10-01 after spending a session reasoning about a live
account from log fragments. Four commits shipped that day against a bot
that could not be observed, and one of them (resuming stock entries
unconditionally) was caught only because a single MEDS order line
happened to arrive through a log monitor. A guess about live state is
how the expensive mistakes on this project start.

Run it on the host, where the volume is readable:

    sudo -n docker exec webull-trading-bot \\
        python /app/scripts/dump_live_state.py

Read-only by construction: it opens state files and prints. It places no
orders, writes nothing, and calls no broker endpoint that could change
anything. Safe to run mid-session.
"""

import json
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path


def _load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        return {"__error__": f"{type(exc).__name__}: {exc}"}


def _rule(title):
    print(f"\n{'=' * 64}\n{title}\n{'=' * 64}")


def _data_dir():
    """The volume, whether we are inside the container or on the host."""
    for candidate in (
        "/var/data",
        "/var/lib/docker/volumes/webull-trading-data/_data",
    ):
        if Path(candidate).is_dir():
            return Path(candidate)
    return Path("/var/data")


def main() -> int:
    data = _data_dir()
    print(f"state volume: {data}")
    print(f"dumped at   : {datetime.now().isoformat(timespec='seconds')}")

    status = _load(data / "status.json")
    if "__error__" in status:
        print(f"\nstatus.json unreadable: {status['__error__']}")
        return 1

    _rule("ACCOUNT")
    for key in (
        "account_value", "total_equity", "buying_power", "cash",
        "day_start_equity", "pnl_today", "daily_realized_pnl",
    ):
        if key in status:
            print(f"  {key:<22} {status[key]}")
    updated = status.get("updated_at")
    if isinstance(updated, (int, float)):
        age = datetime.now().timestamp() - updated
        print(f"  {'status age':<22} {age:.0f}s "
              f"{'(STALE - is the bot alive?)' if age > 300 else ''}")

    _rule("POSITIONS")
    positions = status.get("positions") or []
    if not positions:
        print("  (flat)")
    for item in positions:
        shown = {
            k: item.get(k)
            for k in (
                "symbol", "instrument_type", "quantity", "cost_price",
                "last_price", "unrealized_pnl", "market_value",
            )
            if item.get(k) not in (None, "")
        }
        print(f"  {shown}")

    _rule("SELECTION")
    for key in ("focus_cohort", "daily_batch", "focus_symbol"):
        if key in status:
            print(f"  {key:<22} {status[key]}")

    _rule("WHY IT IS NOT TRADING")
    # The single most-asked question of this project, and the answer is
    # always in these counters - which are otherwise only visible as one
    # transient log line per scan.
    for key in ("option_gate_rejections", "stock_gate_rejections", "gates"):
        bucket = status.get(key)
        if isinstance(bucket, dict) and bucket:
            print(f"  {key}:")
            for reason, count in sorted(
                bucket.items(), key=lambda kv: kv[1], reverse=True
            ):
                print(f"    {count:>5}  {reason}")

    _rule("TODAY'S TRADES")
    history = _load(data / "conf" / "trade_history.json")
    trades = history.get("trades", []) if isinstance(history, dict) else []
    today = date.today()
    rows = [
        t for t in trades
        if datetime.fromtimestamp(t.get("time", 0)).date() == today
    ]
    if not rows:
        print("  (none)")
    realized = 0.0
    for t in sorted(rows, key=lambda t: t.get("time", 0)):
        stamp = datetime.fromtimestamp(t.get("time", 0)).strftime("%H:%M:%S")
        pnl = t.get("pnl")
        if pnl is not None:
            try:
                realized += float(pnl)
            except (TypeError, ValueError):
                pass
        print(f"  {stamp}  {str(t.get('instrument_type'))[:6]:<6} "
              f"{str(t.get('symbol'))[:22]:<22} "
              f"{str(t.get('action'))[:12]:<12} "
              f"qty={t.get('quantity')} pnl={pnl}")
    print(f"\n  realized today (summed from records): {realized:+.2f}")
    print(f"  closed round trips:                  "
          f"{sum(1 for t in rows if t.get('pnl') is not None)}")

    _rule("ECONOMICS OF WHAT IS HELD")
    # The check that mattered on 2026-10-01: an option position whose
    # breakeven sits past its own stop cannot be won by any exit rule.
    # Printed per position so it is visible without a replay run.
    fee = 0.02
    for item in positions:
        if item.get("instrument_type") != "OPTION":
            continue
        try:
            cost = float(item.get("cost_price") or 0)
            last = float(item.get("last_price") or 0)
        except (TypeError, ValueError):
            continue
        if cost <= 0:
            continue
        print(f"  {item.get('symbol')}: cost={cost} last={last} "
              f"needs {((cost + fee - last) / last * 100):+.1f}% on the bid "
              f"to be flat" if last > 0 else "")
    if not any(i.get("instrument_type") == "OPTION" for i in positions):
        print("  (no option positions - share friction is ~0.3%, not 8%)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
