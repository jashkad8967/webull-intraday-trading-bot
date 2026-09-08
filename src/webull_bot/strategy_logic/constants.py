"""Hardcoded (not .env-configurable) strategy thresholds, shared by
TradingStrategy and its extracted strategy_logic/** helper modules.
Kept in a separate module (rather than in strategy.py itself) so
extracted modules can reference these constants without creating a
circular import back into strategy.py, which itself imports from
strategy_logic/**. strategy.py re-exports all of these unchanged for
existing importers (e.g. webull_bot.trading.util.order_book_imbalance).
"""
from decimal import Decimal

# Order-book-imbalance secondary entry gate. Hardcoded, not config, since
# it's a fixed institutional-style heuristic rather than a per-account
# tuning knob: real bid/ask depth is scarce data (needs an L2 market-data
# entitlement most retail accounts don't have) and only ever fetched for a
# symbol that already cleared every other entry gate, so it isn't worth
# exposing as yet another .env dial.
OBI_ENABLED = True
OBI_DEPTH_LEVELS = 5
OBI_BUY_THRESHOLD = Decimal("0.60")

# Options quality-filter thresholds. Hardcoded for the same reason as the
# OBI constants above: these are fixed heuristics on data that may not even
# be present on this account's option snapshot (delta/IV field names are
# unconfirmed - see option_delta/option_implied_vol in webull_api.py), not
# a per-account risk knob a user would tune via .env.
OPTION_DELTA_MIN = Decimal("0.20")
OPTION_DELTA_MAX = Decimal("0.85")
OPTION_IV_PERCENTILE_MIN_SAMPLES = 10
OPTION_IV_REJECT_PERCENTILE = Decimal("0.85")
# VIXY (VIX-futures ETF) proxy for a market-wide volatility regime gate -
# real VIX/CGIF index data isn't reachable through Webull's OpenAPI
# (confirmed live: raw "VIX" returns INVALID_SYMBOL, VIXY resolves fine
# through the ordinary stock-quote path everything else here already uses).
OPTION_VIXY_SYMBOL = "VIXY"
OPTION_VIXY_REJECT_PERCENTILE = Decimal("0.85")
