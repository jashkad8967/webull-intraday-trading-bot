"""Write a dated session report, so learning accumulates without anyone
being present.

An assistant session cannot be made always-on - it dies when the session
ends, and a scheduled Claude job dies with it. What CAN survive is the
evidence: run this from a Scheduled Task after the close and every
session leaves behind a reconciled, self-contained record. The next
session starts from measurements rather than from scratch.

What it captures, and why each item earned its place:

  * RECONCILED P&L. trade_history is written at order SUBMISSION, not
    fill, so a duplicate or rejected SELL writes a phantom PROFIT while a
    rejected order never writes a loss - the error only ever flatters.
    On 2026-10-01 records said -$1.20 and the account said -$4.16. The
    balance is the only number the bot does not author.

  * ENTRY CONTEXT per closed trade. Range position is now an entry gate
    (stock_min_entry_range_position); without recording what each entry
    actually looked like there is no way to tell whether it helped.

  * GATE REJECTION COUNTS. Four separate silent dead ends were found on
    2026-10-01 where the bot could not trade and the logs were
    indistinguishable from a quiet day. A gate that refuses everything
    and a market with no setups look identical unless the counts are
    written down.

  * ERRORS, deduplicated. A 429 retry loop fired four times in 75
    seconds on a flat account; the pre-close sweep was hammering an API
    budget shared with order placement.

Read-only. Writes one file under reports/ and prints a summary.

    python scripts/daily_report.py            # today
    python scripts/daily_report.py 2026-10-01
"""

import json
import re
import sys
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOLERANCE = Decimal("0.50")


def _load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _closed_trades(conf: Path, day: date):
    trades = (_load(conf / "trade_history.json") or {}).get("trades", [])
    rows = []
    for trade in trades:
        stamp = trade.get("time")
        if not stamp or trade.get("pnl") is None:
            continue
        if datetime.fromtimestamp(stamp).date() != day:
            continue
        rows.append(trade)
    return sorted(rows, key=lambda t: t["time"])


def _balance_change(status: dict):
    points = [
        Decimal(str(e["balance"]))
        for e in (status.get("balance_history") or [])
        if e.get("balance") not in (None, "")
    ]
    if len(points) < 2:
        return None, None, None
    return points[-1] - points[0], points[0], points[-1]


def _log_signals(log_dir: Path, day: date):
    """Gate rejections and deduplicated errors from the day's log."""
    path = log_dir / f"{day.isoformat()}.log"
    rejections: Counter = Counter()
    errors: Counter = Counter()
    if not path.exists():
        # The supervisor's capture is the fallback when the bot's own
        # daily file is missing (e.g. a restart before it rotated).
        path = log_dir / "bot.err.log"
    if not path.exists():
        return rejections, errors, None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return rejections, errors, path
    for line in text.splitlines():
        if "not entering because" in line or "entries not yet firing" in line:
            # "reason=COUNT | reason=COUNT" - keep the last seen count,
            # which is cumulative for the session.
            for reason, count in re.findall(r"([a-z][^|=]{6,70})=(\d+)", line):
                rejections[reason.strip()] = int(count)
        if "REFUSED" in line:
            key = re.sub(r"\|.*$", "", line.split("REFUSED")[-1]).strip()
            if key:
                rejections[f"REFUSED {key[:60]}"] += 1
        if " ERROR " in line or "Traceback" in line:
            # Strip timestamps, ids and request ids so one recurring
            # failure counts once instead of fifty times.
            key = re.sub(r"^\d[\d:.]*\s*", "", line)
            key = re.sub(r"(RequestID|id)[=:]\s*[0-9a-f-]+", r"\1=...", key)
            key = re.sub(r"\d+", "N", key)
            errors[key[:110]] += 1
    return rejections, errors, path


def main(argv) -> int:
    day = date.fromisoformat(argv[0]) if argv else date.today()
    conf = REPO / "conf"
    logs = REPO / "logs"
    status = _load(REPO / "status.json") or _load(conf / ".." / "status.json")

    lines = [f"# Session report {day.isoformat()}", ""]

    rows = _closed_trades(conf, day)
    recorded = sum(Decimal(str(t["pnl"])) for t in rows)
    actual, opening, closing = _balance_change(status)

    lines += ["## Money", ""]
    lines.append(f"- closed round trips: {len(rows)}")
    lines.append(f"- recorded P&L: {recorded:+.2f}")
    if actual is None:
        lines.append("- actual balance change: UNAVAILABLE - recorded P&L "
                     "is NOT trustworthy alone (written at submission, "
                     "not fill)")
    else:
        gap = recorded - actual
        lines.append(f"- actual balance change: {actual:+.2f} "
                     f"({opening} -> {closing})")
        lines.append(f"- gap: {gap:+.2f}"
                     + ("  <- TRUST THE BALANCE; records overstate"
                        if gap > TOLERANCE else
                        "  <- records understate" if gap < -TOLERANCE
                        else "  (reconciles)"))
    wins = [t for t in rows if Decimal(str(t["pnl"])) > 0]
    losses = [t for t in rows if Decimal(str(t["pnl"])) < 0]
    if rows:
        lines.append(f"- wins/losses: {len(wins)}/{len(losses)}")
        if wins:
            lines.append(f"- avg win:  "
                         f"{sum(Decimal(str(t['pnl'])) for t in wins) / len(wins):+.2f}")
        if losses:
            lines.append(f"- avg loss: "
                         f"{sum(Decimal(str(t['pnl'])) for t in losses) / len(losses):+.2f}")

    lines += ["", "## Closed trades", ""]
    if not rows:
        lines.append("(none)")
    for t in rows:
        lines.append(
            f"- {datetime.fromtimestamp(t['time']).strftime('%H:%M:%S')} "
            f"{str(t.get('instrument_type'))[:6]:<6} "
            f"{str(t.get('symbol'))[:24]:<24} "
            f"{str(t.get('action'))[:12]:<12} "
            f"qty={t.get('quantity')} entry={t.get('entry_price')} "
            f"pnl={Decimal(str(t['pnl'])):+.2f}"
        )

    rejections, errors, used = _log_signals(logs, day)
    lines += ["", "## Why it did not trade more", ""]
    lines.append(f"(from {used.name if used else 'no log found'})")
    if not rejections:
        lines.append("- no gate counters recorded")
    for reason, count in rejections.most_common(14):
        lines.append(f"- {count:>6}  {reason}")

    lines += ["", "## Errors (deduplicated)", ""]
    if not errors:
        lines.append("- none")
    for key, count in errors.most_common(12):
        lines.append(f"- {count:>4}x  {key}")

    lines += ["", "## Open questions for the next session", "",
              "- is the option stop still scaled to premium rather than to "
              "the underlying's move? (a 10% premium stop was a 0.29% move "
              "in PFE - noise)",
              "- did stock_min_entry_range_position reject anything, and "
              "would those trades have lost?",
              "- does the reconciliation gap above indicate new phantom "
              "records?",
              ""]

    report_dir = REPO / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    out = report_dir / f"{day.isoformat()}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nwritten: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
