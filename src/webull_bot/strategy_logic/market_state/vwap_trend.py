from decimal import Decimal


def _update_vwap(self, symbol: str, price: Decimal, volume: float) -> None:
    state = self.vwap_state.get(symbol)
    if state is None:
        self.vwap_state[symbol] = {
            "cum_pv": 0.0,
            "cum_vol": 0.0,
            "last_volume": volume,
        }
        return
    delta = max(0.0, volume - state["last_volume"])
    if delta > 0:
        state["cum_pv"] += float(price) * delta
        state["cum_vol"] += delta
    state["last_volume"] = volume


def vwap(self, symbol: str) -> Decimal | None:
    state = self.vwap_state.get(symbol)
    if not state or state["cum_vol"] <= 0:
        return None
    return Decimal(str(state["cum_pv"] / state["cum_vol"]))


def vwap_supports_entry(
    self,
    symbol: str,
    price: Decimal,
    direction: str = "BUY",
    idle_relaxation_multiplier: Decimal = Decimal("1"),
) -> bool:
    vwap = self.vwap(symbol)
    if vwap is None:
        return True
    band = self.config.vwap_entry_band_percent * idle_relaxation_multiplier
    if direction == "SHORT":
        return price <= vwap * (Decimal("1") + band)
    return price >= vwap * (Decimal("1") - band)


def volatility_scalp_vwap_supports_entry(
    self, symbol: str, price: Decimal
) -> bool:
    """Intraday counterpart to sma_trend_supports_entry - by
    request, after an end-of-day retrospective ("we just kept
    buying at the wrong time"): the SMA filter only catches a
    MULTI-DAY downtrend, nothing for a stock simply having a bad
    DAY today specifically (an intraday decline that hasn't shown
    up in the daily SMA yet). A stock trading meaningfully below
    its own session VWAP is showing real intraday weakness, not
    just a normal dip. Uses its own, much wider band
    (VOLATILITY_SCALP_VWAP_BAND_PERCENT) than the general
    vwap_supports_entry's VWAP_ENTRY_BAND_PERCENT (0.1%, tuned for
    more liquid names) - a genuinely choppy penny stock's normal
    dip-buy can easily sit several percent below its own VWAP
    without that being a real warning sign. Fails OPEN (True) with
    no VWAP data yet, same convention as every other entry gate.
    """
    vwap = self.vwap(symbol)
    if vwap is None:
        return True
    return price >= vwap * (
        Decimal("1") - self.config.volatility_scalp_vwap_band_percent
    )


def sma_trend_supports_entry(
    self, symbol: str, price: Decimal, direction: str = "BUY"
) -> bool:
    """Higher-timeframe trend filter: only let the fast EMA(3/8) scalp
    signal fire in the direction of the slower SMA_TREND_DAYS-day
    trend (see AutoTrader.refresh_sma_trend) - a scalp that's fighting
    the larger trend is a lower-quality setup even when the short-term
    crossover looks right. Off by default (SMA_TREND_FILTER_ENABLED)
    and passes through when a symbol has no cached SMA yet (fresh
    listing, screener miss, or the refresh hasn't run this run), same
    "no data -> don't block" convention as every other entry gate.
    """
    if not self.config.sma_trend_filter_enabled:
        return True
    sma = self.sma_trend.get(symbol)
    if sma is None:
        return True
    if direction == "SHORT":
        return price <= sma
    return price >= sma


def recent_momentum_supports_entry(
    self, symbol: str, direction: str = "BUY"
) -> bool:
    """By request: "look at tickers in the last 10 mins for
    momentum... to analyze the upcoming trend." sma_trend_supports_
    entry above covers the historical/multi-day trend, and volatility_
    scalp_vwap_supports_entry covers the whole session - this fills
    the gap in between. recent_momentum (see AutoTrader.refresh_
    recent_momentum) is the net % change over the last RECENT_
    MOMENTUM_LOOKBACK_MINUTES minutes of real 1-minute bars.

    Deliberately NOT "block any recent decline" - dip-buying a
    short-term pullback is this cohort's entire purpose, so a
    moderate decline is the setup, not a warning sign. Only blocks
    a BUY when the recent decline is steeper than RECENT_MOMENTUM_
    MAX_DECLINE_PERCENT - a real, fast breakdown over the last few
    minutes, not routine chop. Fails OPEN (True) with the filter
    disabled or no data yet, same convention as every other entry
    gate in this file.
    """
    if not self.config.recent_momentum_filter_enabled:
        return True
    momentum = self.recent_momentum.get(symbol)
    if momentum is None:
        return True
    threshold = self.config.recent_momentum_max_decline_percent
    if direction == "SHORT":
        return momentum <= threshold
    return momentum >= -threshold


def multi_day_momentum_supports_entry(
    self, symbol: str, direction: str = "BUY", price: Decimal | None = None
) -> bool:
    """By request: "also include not only short term patterns like
    5-10 mins, but also 1 day and 5 day and month." recent_momentum
    (10 min) and sma_trend (50-day average) already existed; this
    fills the explicit 1-day/5-day/~1-month timeframes named
    directly, using real daily-bar closes (self.daily_closes,
    newest-first - see AutoTrader.refresh_multi_day_momentum)
    instead of the tick window or an averaged value.

    Deliberately more permissive at longer horizons - a stock can
    legitimately be down over a month while still being a good
    dip-buy today (that's the whole cohort's thesis), so this only
    blocks a genuinely severe, sustained decline at each horizon
    (15%/1d, 30%/5d, 50%/month by default), not routine drift.
    Fails open with the filter disabled or insufficient daily-bar
    history at a given horizon, same convention as every other
    entry gate in this file - each of the three checks is
    evaluated independently, so having only 1-day data (say) still
    lets that one check apply.

    Also blocks the opposite extreme on a BUY (live incident: MGN/
    FAMI - see multi_day_momentum_max_extension_1d's config
    comment): a stock whose CURRENT live price is still more than
    that fraction above YESTERDAY's close (closes[0] - the daily-
    bar API's most recent bar is always the last fully-closed day,
    never today's still-forming one) is mid-unwind of an intraday
    spike, not a stable range - a "dip" a few cents off that kind
    of high is still a falling knife, not a real mean-reversion
    setup, no matter how the last few minutes of ticks look on
    their own. Needs the live price passed in separately from
    closes for this one check; skipped (fails open) if the caller
    doesn't have it yet.
    """
    if not self.config.multi_day_momentum_filter_enabled:
        return True
    closes = self.daily_closes.get(symbol)
    if not closes or closes[0] <= 0:
        return True
    checks = (
        (1, self.config.multi_day_momentum_max_decline_1d),
        (5, self.config.multi_day_momentum_max_decline_5d),
        (20, self.config.multi_day_momentum_max_decline_month),
    )
    for offset, threshold in checks:
        if len(closes) <= offset or closes[offset] <= 0:
            continue
        change = (closes[0] - closes[offset]) / closes[offset]
        if direction == "SHORT":
            if change >= threshold:
                return False
        elif change <= -threshold:
            return False
    if direction == "BUY" and price is not None and price > 0:
        prior_close = Decimal(str(closes[0]))
        extension = (price - prior_close) / prior_close
        if extension >= self.config.multi_day_momentum_max_extension_1d:
            return False
    return True


def update_recent_tick_history(
    self, symbol: str, price: Decimal, moment: float
) -> None:
    """Appends (moment, price) to recent_tick_history - see its
    own __init__ comment for why this is a SEPARATE structure from
    volatility_price_history. moment is a time.monotonic() reading,
    passed in by the caller rather than read here, so a caller
    processing a whole batch can share one consistent timestamp
    across every symbol in it instead of a slightly-different one
    per call.
    """
    if price is None or price <= 0:
        return
    self.recent_tick_history[symbol].append((moment, price))


def update_volume_delta(self, symbol: str, cumulative_volume: Decimal) -> None:
    """Derives an incremental volume-delta reading from Webull's
    cumulative day-volume snapshot field, smoothed into a rolling
    EMA baseline - see volatility_price_history's __init__ comment
    for why this can't just be a moving average over a bare sample
    window. First call for a symbol only sets the baseline (a
    single snapshot has no prior reading to diff against) - no
    delta or EMA update happens until the second call, same
    "deploy-resilient" initialization every restart needs.
    """
    if cumulative_volume is None or cumulative_volume < 0:
        return
    previous = self.volume_delta_baseline.get(symbol)
    self.volume_delta_baseline[symbol] = cumulative_volume
    if previous is None or cumulative_volume < previous:
        # cumulative_volume < previous means the day rolled over
        # (or this is stale data) - restart the baseline instead of
        # recording a nonsensical negative delta.
        return
    delta = cumulative_volume - previous
    self.volume_delta_latest[symbol] = delta
    alpha = self.config.volatility_scalp_micro_exhaustion_volume_ema_alpha
    prior_ema = self.volume_delta_ema.get(symbol)
    self.volume_delta_ema[symbol] = (
        delta if prior_ema is None else alpha * delta + (1 - alpha) * prior_ema
    )
