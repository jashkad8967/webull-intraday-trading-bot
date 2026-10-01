#!/usr/bin/env sh
# The single permitted order path, kept deliberately tiny.
#
# This exists so a permission rule can name ONE command:
#
#     "Bash(./scripts/trade.sh:*)"
#
# rather than granting arbitrary python, which would be a far broader
# capability than placing a trade. Everything it can do is reachable only
# through scripts/place_order.py, which re-verifies against live quotes
# and honours the standing operator veto.
#
#     ./scripts/trade.sh buy NVDA
#     ./scripts/trade.sh sell NVDA
#     ./scripts/trade.sh sell NVDA --force
#
# --force IS REFUSED FOR BUYS, on purpose. It skips re-verification, which
# is acceptable when closing a position but not when opening one on an
# unattended schedule - the entire safety of autonomous entry is that the
# setup is re-checked at the moment of placement. A setup can evaporate
# between decision and order: ACN went from +18% to 14% of its daily range
# inside one session on 2026-10-01. Closing never needs a reason, so
# --force stays available for sells.
set -eu

cd "$(dirname "$0")/.." || exit 1

if [ "$#" -lt 2 ]; then
    echo "usage: trade.sh {buy|sell} SYMBOL [--instrument OPTION] [--force]" >&2
    exit 64
fi

side="$1"
case "$side" in
    buy|sell) ;;
    *) echo "trade.sh: side must be buy or sell, got '$side'" >&2; exit 64 ;;
esac

if [ "$side" = "buy" ]; then
    for arg in "$@"; do
        if [ "$arg" = "--force" ]; then
            echo "trade.sh: --force is refused for BUY - an unattended" >&2
            echo "  entry must re-verify the setup at placement time." >&2
            exit 64
        fi
    done
fi

PYTHONPATH=src exec .venv/Scripts/python.exe scripts/place_order.py "$@"
