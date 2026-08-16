#!/usr/bin/env python3
"""CI pass/fail gate for the paper-trading-lane branch (see
.github/workflows/paper-sim-gate.yml). Every check below must pass before
that workflow auto-merges paper-trading-lane into main - which then
auto-deploys to the live account via ci.yml's existing deploy-gcp job, with
no human approval step in between. main's own live guardrails (stop-loss
guard, circuit breakers, wash-sale blocking) keep running regardless of
what merges in, but this script is the only check standing before a change
reaches real money, so it fails loudly and fails closed: any single check
failing here means main is left untouched.

Self-contained on purpose (imports and runs everything directly rather than
relying on a separate CI job) - paper-sim-gate.yml is its own workflow file
and GitHub Actions job dependencies (`needs:`) don't cross workflow files,
so this script cannot lean on ci.yml's own validate job.

Usage: python scripts/evaluate_paper_sim.py
Exit code 0 = every check passed. Exit code 1 = at least one failed.
"""

import compileall
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _compileall_ok() -> tuple[bool, str]:
    ok = True
    for directory in ("src", "tests", "ui"):
        if not compileall.compile_dir(
            str(REPO_ROOT / directory), quiet=1, force=True
        ):
            ok = False
    return ok, "" if ok else "compileall found a syntax error - see output above"


def _unittest_ok() -> tuple[bool, str]:
    loader = unittest.TestLoader()
    suite = loader.discover(
        str(REPO_ROOT / "tests"), top_level_dir=str(REPO_ROOT / "tests")
    )
    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(suite)
    if result.wasSuccessful():
        return True, f"{result.testsRun} tests passed"
    detail = f"{len(result.failures)} failure(s), {len(result.errors)} error(s)"
    return False, detail


def _scenarios_ok() -> tuple[bool, str]:
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    from paper_sim import scenarios

    results = scenarios.run_all()
    failures = [r for r in results if not r.passed]
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        detail = f" - {result.detail}" if result.detail else ""
        print(f"[{status}] {result.name}{detail}")
    if failures:
        detail = "; ".join(f"{r.name}: {r.detail}" for r in failures)
        return False, detail
    return True, f"{len(results)} scenarios passed"


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    checks = [
        ("compileall", _compileall_ok),
        ("existing test suite", _unittest_ok),
        ("synthetic scenario suite", _scenarios_ok),
    ]
    failed = []
    for name, check in checks:
        print(f"\n=== {name} ===")
        ok, detail = check()
        status = "PASS" if ok else "FAIL"
        suffix = f" - {detail}" if detail else ""
        print(f"{status}: {name}{suffix}")
        if not ok:
            failed.append((name, detail))

    print("\n" + "=" * 60)
    if failed:
        print(f"PAPER-SIM GATE: FAILED ({len(failed)} check(s) failed)")
        for name, detail in failed:
            print(f"  - {name}: {detail}")
        print("main is untouched.")
        return 1
    print("PAPER-SIM GATE: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
