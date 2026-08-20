"""Fully-automatic strategy-review apply step (explicitly confirmed by
the user, twice, after being shown the risk of zero human review before
a change reaches the live account - see the session's plan file for the
full design rationale).

Run on a schedule by .github/workflows/strategy-tuning-auto-apply.yml,
never interactively. Reads the live dashboard's current strategy-review
suggestion and the live host's .env (both fetched by the workflow before
this runs), decides whether any lever needs adjusting via the pure,
already-tested strategy_tuning.apply_lever_adjustment, and if so edits
config.py's default in place. This script itself never touches git or
runs the verification suite - the workflow does that only when this
script reports changed=true, and only commits/pushes if verification
passes. A field found overridden in the live .env is skipped entirely
(editing config.py's default wouldn't change live behavior, and
silently pretending it did would be worse than doing nothing).
"""

import argparse
import hashlib
import json
import re
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from webull_bot.strategy_tuning import (  # noqa: E402
    LEVER_SPECS,
    StrategyTuningState,
    apply_lever_adjustment,
)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "src" / "webull_bot" / "config.py"
COOLDOWN_STATE_PATH = "conf/strategy_tuning_cooldown.json"
LAST_SEEN_PATH = Path(__file__).resolve().parent.parent / "conf" / "strategy_tuning_last_seen.json"


def _read_text(path: Path) -> str:
    # newline="" disables Python's universal-newline translation on
    # both read and write - without it, reading an LF file and writing
    # it back on Windows silently converts every line ending to CRLF
    # (str.open()'s text-mode default), turning what should be a 1-line
    # config.py diff into a full-file rewrite. config.py itself has no
    # CRLF in it at all (confirmed directly) - this must round-trip
    # byte-for-byte outside the one line actually being changed.
    with open(path, "r", newline="", encoding="utf-8") as handle:
        return handle.read()


def _write_text(path: Path, text: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        handle.write(text)

_PAIR_FIELDS = (
    "stock_core_session_position_fraction",
    "stock_whole_share_core_session_fraction",
)


def _env_var_name(field: str) -> str:
    return field.upper()


def _is_overridden_in_live_env(field: str, live_env_text: str) -> bool:
    pattern = re.compile(
        rf"^{re.escape(_env_var_name(field))}=", re.MULTILINE
    )
    return bool(pattern.search(live_env_text))


def _read_current_decimal_or_int(config_text: str, field: str) -> Decimal | None:
    match = re.search(
        rf"{re.escape(field)}:\s*\w+\s*=\s*Field\(\s*(?:#.*\n\s*)*default=(?:Decimal\(\"([^\"]*)\"\)|(\d+(?:\.\d+)?))",
        config_text,
    )
    if not match:
        return None
    raw = match.group(1) or match.group(2)
    try:
        return Decimal(raw)
    except Exception:
        return None


def _read_current_bool(config_text: str, field: str) -> bool | None:
    match = re.search(rf"{re.escape(field)}:\s*bool\s*=\s*(True|False)", config_text)
    if not match:
        return None
    return match.group(1) == "True"


def _write_decimal_or_int(config_text: str, field: str, new_value: Decimal, is_int: bool) -> str:
    literal = str(int(new_value)) if is_int else f'Decimal("{new_value}")'
    pattern = re.compile(
        rf'({re.escape(field)}:\s*\w+\s*=\s*Field\(\s*(?:#.*\n\s*)*default=)(?:Decimal\("[^"]*"\)|\d+(?:\.\d+)?)'
    )
    new_text, count = pattern.subn(rf"\g<1>{literal}", config_text, count=1)
    if count != 1:
        raise RuntimeError(f"could not locate a unique default= for {field}")
    return new_text


def _write_bool(config_text: str, field: str, new_value: bool) -> str:
    pattern = re.compile(rf"({re.escape(field)}:\s*bool\s*=\s*)(True|False)")
    new_text, count = pattern.subn(rf"\g<1>{new_value}", config_text, count=1)
    if count != 1:
        raise RuntimeError(f"could not locate a unique bool default for {field}")
    return new_text


def _is_int_field(field: str) -> bool:
    return field in {
        "reenter_confirmation_polls",
        "time_aware_stop_widen_seconds",
    }


def _suggestion_hash(review: dict) -> str:
    return hashlib.sha256(
        json.dumps(review, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", required=True)
    parser.add_argument("--live-env", required=True)
    parser.add_argument("--cooldown-hours", type=int, default=24)
    parser.add_argument("--step-fraction", type=str, default="0.10")
    args = parser.parse_args()

    outputs = {"changed": "false", "commit_message": ""}

    status = json.loads(_read_text(Path(args.status)))
    review = ((status.get("agent") or {}).get("strategy_review")) or None
    if not review or review.get("severity") == "none":
        print("No actionable strategy-review suggestion (severity=none or missing).")
        _emit(outputs)
        return 0

    digest = _suggestion_hash(review)
    last_seen = {}
    if LAST_SEEN_PATH.exists():
        try:
            last_seen = json.loads(_read_text(LAST_SEEN_PATH))
        except json.JSONDecodeError:
            last_seen = {}
    if last_seen.get("hash") == digest:
        print("This exact suggestion was already processed - skipping.")
        _emit(outputs)
        return 0

    live_env_text = _read_text(Path(args.live_env))
    config_text = _read_text(CONFIG_PATH)
    state = StrategyTuningState(COOLDOWN_STATE_PATH)
    step_fraction = Decimal(args.step_fraction)

    applied_summaries: list[str] = []
    changed_levers: list[str] = []

    for change in review.get("suggested_changes", []):
        lever = str(change.get("lever", ""))
        direction = str(change.get("direction", ""))
        reasoning = str(change.get("reasoning", ""))
        spec = LEVER_SPECS.get(lever)

        if not state.ready(lever, args.cooldown_hours):
            print(f"SKIP  | {lever} | still within the cooldown window")
            continue

        if direction in ("enable", "disable") and spec and spec.enabled_field:
            field = spec.enabled_field
            if _is_overridden_in_live_env(field, live_env_text):
                print(f"SKIP  | {field} | overridden in the live .env")
                continue
            current = _read_current_bool(config_text, field)
            if current is None:
                print(f"SKIP  | {field} | could not read current value")
                continue
            new_value = direction == "enable"
            if new_value == current:
                continue
            config_text = _write_bool(config_text, field, new_value)
            state.record(lever)
            changed_levers.append(lever)
            applied_summaries.append(
                f"{field}: {current} -> {new_value} ({lever}/{direction}: {reasoning})"
            )
            continue

        if lever == "fractional-vs-whole-share balance":
            if any(
                _is_overridden_in_live_env(field, live_env_text)
                for field in _PAIR_FIELDS
            ):
                print("SKIP  | fractional-vs-whole-share balance | overridden in the live .env")
                continue
            current_values = {
                field: _read_current_decimal_or_int(config_text, field)
                for field in _PAIR_FIELDS
            }
            if any(v is None for v in current_values.values()):
                print("SKIP  | fractional-vs-whole-share balance | could not read current values")
                continue
            result = apply_lever_adjustment(lever, direction, current_values, step_fraction)
            if result is None:
                continue
            config_text = _write_decimal_or_int(config_text, result.field, result.new_value, False)
            config_text = _write_decimal_or_int(
                config_text, result.paired_field, result.paired_new_value, False
            )
            state.record(lever)
            changed_levers.append(lever)
            applied_summaries.append(
                f"{result.field}: {result.old_value} -> {result.new_value}, "
                f"{result.paired_field}: {result.paired_old_value} -> {result.paired_new_value} "
                f"({lever}/{direction}: {reasoning})"
            )
            continue

        if spec is None:
            print(f"SKIP  | {lever} | not a recognized lever")
            continue
        if _is_overridden_in_live_env(spec.field, live_env_text):
            print(f"SKIP  | {spec.field} | overridden in the live .env")
            continue
        current = _read_current_decimal_or_int(config_text, spec.field)
        if current is None:
            print(f"SKIP  | {spec.field} | could not read current value")
            continue
        result = apply_lever_adjustment(lever, direction, {spec.field: current}, step_fraction)
        if result is None:
            continue
        config_text = _write_decimal_or_int(
            config_text, result.field, result.new_value, _is_int_field(result.field)
        )
        state.record(lever)
        changed_levers.append(lever)
        applied_summaries.append(
            f"{result.field}: {result.old_value} -> {result.new_value} "
            f"({lever}/{direction}: {reasoning})"
        )

    LAST_SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    _write_text(LAST_SEEN_PATH, json.dumps({"hash": digest}))

    if not changed_levers:
        print("No lever produced an adjustment this cycle.")
        _emit(outputs)
        return 0

    _write_text(CONFIG_PATH, config_text)
    outputs["changed"] = "true"
    summary_line = f"Auto-apply strategy review: {review.get('assessment', '')}"
    body = "\n".join(f"- {line}" for line in applied_summaries)
    outputs["commit_message"] = f"{summary_line}\n\n{body}"
    for line in applied_summaries:
        print(f"APPLY | {line}")
    _emit(outputs)
    return 0


def _emit(outputs: dict) -> None:
    github_output = __import__("os").environ.get("GITHUB_OUTPUT")
    if not github_output:
        return
    with open(github_output, "a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            if "\n" in value:
                delimiter = "EOF_APPLY_STRATEGY_REVIEW"
                handle.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")
            else:
                handle.write(f"{key}={value}\n")


if __name__ == "__main__":
    raise SystemExit(main())
