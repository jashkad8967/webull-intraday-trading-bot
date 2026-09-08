from decimal import Decimal

from webull_bot.strategy_logic.constants import (
    OBI_ENABLED,
    OBI_BUY_THRESHOLD,
    OPTION_DELTA_MAX,
    OPTION_DELTA_MIN,
    OPTION_IV_PERCENTILE_MIN_SAMPLES,
    OPTION_IV_REJECT_PERCENTILE,
    OPTION_VIXY_REJECT_PERCENTILE,
)


def obi_supports_entry(obi_score: Decimal | None) -> bool:
    """bid volume / (bid + ask volume) across the top few book levels
    (or top-of-book size as a fallback) - a heavy imbalance toward the
    bid statistically favors an upward move over the next few seconds.
    `obi_score` is fetched and computed by the caller (it needs a live
    API round-trip, unlike every other gate here); `None` means no
    depth/size data was available and the gate passes through, same
    convention as entry_spread_ok/entry_extension_ok with missing data.
    """
    return (
        not OBI_ENABLED
        or obi_score is None
        or obi_score >= OBI_BUY_THRESHOLD
    )


def option_delta_ok(delta: Decimal | None) -> bool:
    """Quality filter, not strike selection: rejects a contract that's
    too far OTM to have real directional exposure (lottery-ticket cheap,
    decays fast) or so deep ITM it's paying for intrinsic value with no
    leverage left. `None` (delta unavailable on this account's snapshot)
    passes through untouched, same as every other best-effort gate.
    """
    return delta is None or OPTION_DELTA_MIN <= abs(delta) <= OPTION_DELTA_MAX


def _percentile_reject_ok(
    history,
    current: Decimal | None,
    min_samples: int,
    reject_percentile: Decimal,
) -> bool:
    """Shared rank-within-own-history check: rejects when `current`
    sits at or above `reject_percentile` of `history`'s own samples -
    relative, not an absolute threshold, since "high" only means
    anything compared to that same series' own recent range. Passes
    through when there's no current sample or not enough history yet
    to judge (both `option_iv_percentile_ok` and
    `option_market_regime_ok` share this).
    """
    if current is None:
        return True
    samples = list(history)
    if len(samples) < min_samples:
        return True
    rank = sum(1 for sample in samples if sample <= current) / len(samples)
    return Decimal(str(rank)) < reject_percentile


def option_iv_percentile_ok(
    iv_history,
    current_iv: Decimal | None,
) -> bool:
    """Rejects an entry when current_iv sits in the priciest tail of
    this SAME contract's own recent IV samples - no external IV-rank
    source exists here. Passes through when IV data or enough history
    isn't available yet.
    """
    return _percentile_reject_ok(
        iv_history,
        current_iv,
        OPTION_IV_PERCENTILE_MIN_SAMPLES,
        OPTION_IV_REJECT_PERCENTILE,
    )


def option_market_regime_ok(
    vixy_history,
    current_vixy: Decimal | None,
) -> bool:
    """Market-wide volatility regime gate for options entries: VIXY (a
    VIX-futures ETF - real VIX/CGIF index data isn't reachable through
    Webull's OpenAPI, confirmed live) stands in for broad market fear.
    Rejects a new entry when VIXY is spiking into the top of its own
    recent range - a bad time to be buying option premium anywhere,
    regardless of how any one contract's own delta/IV look. Relative to
    VIXY's own recent range, not an absolute level (VIXY's baseline
    drifts with its futures-roll decay over time). Passes through when
    there's no VIXY quote or not enough history yet.
    """
    return _percentile_reject_ok(
        vixy_history,
        current_vixy,
        OPTION_IV_PERCENTILE_MIN_SAMPLES,
        OPTION_VIXY_REJECT_PERCENTILE,
    )


def stock_market_regime_ok(
    vixy_history,
    current_vixy: Decimal | None,
    reject_percentile: Decimal,
) -> bool:
    """Same VIXY-rolling-percentile regime gate as
    option_market_regime_ok, generalized to stock entries with their
    own configurable REGIME_GATE_REJECT_PERCENTILE instead of the
    options-only hardcoded OPTION_VIXY_REJECT_PERCENTILE - a vol
    regime that's a reason to skip option premium isn't necessarily
    the same bar for skipping a stock scalp. Passes through when
    there's no VIXY quote or not enough history yet.
    """
    return _percentile_reject_ok(
        vixy_history,
        current_vixy,
        OPTION_IV_PERCENTILE_MIN_SAMPLES,
        reject_percentile,
    )
