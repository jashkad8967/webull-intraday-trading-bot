from decimal import Decimal


def realized_volatility_percent(self, symbol: str) -> Decimal | None:
    """Stdev of consecutive-sample percent returns over the rolling
    window, as a fraction (0.02 = 2%) - the "how choppy has this
    symbol actually been just now" signal volatility-scalp eligibility
    is gated on. None (not "very volatile") until there's enough
    history to say so.
    """
    window = self.volatility_price_history.get(symbol)
    if not window or len(window) < self.VOLATILITY_SCALP_MIN_SAMPLES:
        return None
    returns = []
    prev = None
    for sample in window:
        if prev is not None and prev > 0:
            returns.append((sample - prev) / prev)
        prev = sample
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
    return Decimal(str(variance ** 0.5))


def trend_efficiency_ratio(self, symbol: str) -> Decimal | None:
    """Kaufman's Efficiency Ratio: net price movement over the
    lookback window divided by the sum of the window's absolute
    tick-to-tick movement. Near 1 means price moved directly toward
    wherever it ended up (efficient/trending); near 0 means it
    wandered back and forth without much net progress (choppy/
    ranging). Standard regime input behind KAMA - reuses the same
    volatility_price_history window is_volatility_scalp_eligible
    already maintains, so this costs zero additional API calls.

    None (no reading yet) with fewer than
    trend_efficiency_lookback_samples data points, or if every tick
    in the window was flat (zero total movement - direction is
    undefined, not "ranging").
    """
    window = self.volatility_price_history.get(symbol)
    lookback = self.config.trend_efficiency_lookback_samples
    if not window or len(window) < lookback:
        return None
    samples = list(window)[-lookback:]
    net_movement = abs(samples[-1] - samples[0])
    total_movement = sum(
        abs(samples[i] - samples[i - 1]) for i in range(1, len(samples))
    )
    if total_movement <= 0:
        return None
    return Decimal(str(net_movement / total_movement))


def symbol_regime(self, symbol: str) -> str:
    """"TRENDING", "RANGING", or "UNKNOWN" (insufficient history -
    fails OPEN, same "no data -> don't block" convention as every
    other gate in this file: both entry paths stay eligible rather
    than neither).
    """
    ratio = self.trend_efficiency_ratio(symbol)
    if ratio is None:
        return "UNKNOWN"
    if ratio >= self.config.trend_efficiency_trending_threshold:
        return "TRENDING"
    return "RANGING"


def seed_volatility_window(self, symbol: str, closes: list[float]) -> None:
    """One-time warm start from real M1 bar closes (see
    AutoTrader.seed_volatility_windows) - only fires while the
    window's still empty, so it never overwrites live snapshot-poll
    history already being tracked for a symbol. Without this, a
    freshly-scanned symbol needs VOLATILITY_SCALP_MIN_SAMPLES live
    polls (several scan cycles) before it can even be evaluated for
    eligibility; with it, real intraday history is available
    immediately.
    """
    if self.volatility_price_history.get(symbol):
        return
    window = self.volatility_price_history[symbol]
    for close in closes:
        if close > 0:
            window.append(close)


def is_volatility_scalp_eligible(self, symbol: str) -> bool:
    if not self.config.volatility_scalp_enabled:
        return False
    stdev = self.realized_volatility_percent(symbol)
    if stdev is None:
        return False
    if stdev < self.config.volatility_scalp_min_stdev_percent:
        return False
    # By request: "the stocks being chosen have very low volume,
    # thus they do not fluctuate much, we need high volume stocks
    # for more volatility." A thin, illiquid name can clear the
    # stdev bar above purely from a few small prints knocking a
    # wide, empty spread around - real tradeable volatility needs
    # real trading volume behind it too, all day (not just
    # extended hours). Fails closed (not eligible) with no metrics
    # yet, same "no data -> don't trust it" convention as the
    # stdev check above.
    #
    # Live incident (this bug, caught the same day it shipped): a
    # raw SHARE-count floor doesn't scale with price, so it's
    # nearly meaningless for a penny stock - SOAR cleared 500,000
    # shares of "volume" at ~$0.28/share, which is only ~$140k of
    # real dollar liquidity. That thin a book couldn't absorb this
    # strategy's own repeated buy/average-down/exit order flow: its
    # PROFIT exit failed to fill even after three separate
    # escalation-and-reprice cycles, forcing a market-order exit at
    # a loss. Measuring DOLLAR volume (price x share volume)
    # instead fixes this at any price level, not just penny names.
    metrics = self.metrics.get(symbol)
    if not metrics:
        return False
    price = self.prices.get(symbol)
    if not price or price <= 0:
        return False
    dollar_volume = Decimal(str(metrics.get("volume", 0))) * price
    return dollar_volume >= self.config.volatility_scalp_min_dollar_volume


def volatility_scalp_dip_signal(self, symbol: str, price: Decimal) -> bool:
    """True when price has pulled back at least
    volatility_scalp_dip_entry_percent from the rolling window's own
    recent high - "buy the dip" for a symbol that's already been
    confirmed choppy enough to qualify (see is_volatility_scalp_
    eligible; callers are expected to check that first).

    Measured against a short LOCAL high (the last few samples), not
    the whole rolling window's high - a stock trending hard in one
    direction all day (live example: HOWL, up ~100% intraday) keeps
    making new window highs almost every sample, so "down X% from
    the window's own all-time-today high" almost never fires once a
    strong trend is underway, even though the stock is still making
    exactly the kind of fast, small back-and-forth wiggles this
    strategy exists to capture. A local high stays reactive to those
    wiggles regardless of the larger trend.

    By explicit request, this does NOT require a bounce confirmation
    (an earlier version did, added after HOWL/GAUZ were stopped out
    within minutes of a dip-only entry - but the user explicitly
    wants continuous, high-frequency dip-buying on this cohort even
    through losses, not a cautious wait for a confirmed reversal).
    Losses on this path are an accepted, intended cost of trading
    this fast - see volatility_scalp_bypasses_loss_gates in bot.py
    for the entry-side gates (quarantine, wash-sale, stop-loss
    guard, hourly rate cap) deliberately bypassed for this cohort so
    a losing stretch doesn't pause it.
    """
    window = self.volatility_price_history.get(symbol)
    if not window:
        return False
    samples = list(window)
    # In live usage, update_stock_snapshot has already appended this
    # same price as the window's last element by the time this runs
    # - exclude it so the local high compares against history
    # strictly BEFORE this observation, not against itself.
    if samples and Decimal(str(samples[-1])) == price:
        samples = samples[:-1]
    if not samples:
        return False
    recent_samples = samples[-self.VOLATILITY_SCALP_LOCAL_HIGH_SAMPLES:]
    recent_high = Decimal(str(max(recent_samples)))
    if recent_high <= 0 or price <= 0:
        return False
    drop = (recent_high - price) / recent_high
    return drop >= self.config.volatility_scalp_dip_entry_percent


def volatility_scalp_rip_signal(self, symbol: str, price: Decimal) -> bool:
    """Mirror-image of volatility_scalp_dip_signal - true when price
    has run up at least volatility_scalp_dip_entry_percent from the
    rolling window's own recent LOW, i.e. the same "genuine local
    wiggle" bar, just the opposite direction. By request: "it doesn't
    buy puts while there is a dip, or a call on a dip entry" - a
    stock's fast up-leg is exactly as tradeable (for a PUT, betting
    on the reversion back down) as its fast down-leg is for a CALL;
    this is what lets AutoTrader.trade_options' quick-scalp PUT path
    identify that up-leg the same way the CALL path already uses
    volatility_scalp_dip_signal for the down-leg. Same local-window,
    no-bounce-confirmation reasoning throughout - see that
    function's docstring for the full rationale.
    """
    window = self.volatility_price_history.get(symbol)
    if not window:
        return False
    samples = list(window)
    if samples and Decimal(str(samples[-1])) == price:
        samples = samples[:-1]
    if not samples:
        return False
    recent_samples = samples[-self.VOLATILITY_SCALP_LOCAL_HIGH_SAMPLES:]
    recent_low = Decimal(str(min(recent_samples)))
    if recent_low <= 0 or price <= 0:
        return False
    rise = (price - recent_low) / recent_low
    return rise >= self.config.volatility_scalp_dip_entry_percent


def volatility_scalp_momentum_stalled_or_rising(
    self, symbol: str, price: Decimal
) -> bool:
    """True the instant downward momentum stops being negative
    after a genuine run of consecutive downticks - fires right as
    the rise starts, not several ticks after it. By request: "we
    want the momentum to stop being negative after consecutive
    downticks, then we buy, almost as the rise starts." An AND gate
    alongside every entry trigger (dip/breakout/HA-reversal), not a
    replacement for any of them - a symbol can clear the dip-
    percent threshold and still be actively falling the very
    instant it does, which is exactly the case this exists to
    block.

    Two-part check, not a single "N non-declining ticks" window
    (which is what this used to be, recalibrated 1 -> 2 -> 3 ticks
    across earlier requests - still too slow/noisy either way):

    1. Requires a REAL net decline across the 3-sample window (the
       earliest sample above the most recent one) - this is what
       proves a genuine falling-knife dip actually happened, not
       noise. Without this, "stalled" and "never was falling in
       the first place" were indistinguishable.
    2. Then fires on the very FIRST tick that is no longer lower
       than the one before it - a single-tick turn confirmation, so
       entry lands right at the reversal instead of waiting out
       several more ticks of confirmation and missing the move.

    By request, after live evidence ("it is not averaging down at
    all"): part 1 originally required EVERY intermediate step to
    be strictly decreasing (earlier > middle > last), not just net
    decline - live evidence over a full session showed this
    essentially never registered a stall in real market data (one
    flat tick or one-cent bounce anywhere in a 3-sample window
    failed the whole check), blocking averaging down almost
    continuously right up to a stop-loss instead of the occasional
    genuine no-stall case it was meant for. Relaxed to net decline
    across the window (earliest sample above the most recent one),
    tolerating a single intermediate wobble within a real decline -
    still requires actual net downward movement, not flat/noise-
    only, just no longer a strict staircase.

    Fails OPEN (True, doesn't block) with fewer than 3 samples yet,
    same "no data -> don't block" convention as every other entry
    gate in this file.
    """
    window = self.volatility_price_history.get(symbol)
    if not window:
        return True
    samples = list(window)
    if samples and Decimal(str(samples[-1])) == price:
        samples = samples[:-1]
    if len(samples) < 3:
        return True
    previous = Decimal(str(samples[-1]))
    earlier_still = Decimal(str(samples[-3]))
    if previous <= 0 or earlier_still <= 0:
        return True
    was_falling = earlier_still > previous
    if not was_falling:
        return False
    return price >= previous


def volatility_scalp_momentum_stalling(self, symbol: str, price: Decimal) -> bool:
    """Mirror of volatility_scalp_momentum_stalled_or_rising for the
    exit side: true once upward momentum has stopped making fresh
    highs for TWO consecutive ticks, not just one. By request: "if
    there is a profit and it doesn't seem to be going much higher,
    then sell it off... before the next dip." Combined with an
    in-profit check by the caller (volatility_scalp_exit_override) -
    this alone doesn't imply profitability, just that the price
    isn't still climbing.

    Recalibrated by request - "too trigger happy to sell... not
    capturing the profits when it can": a single flat/down tick is
    normal noise even in a genuine uptrend and was cutting winners
    off before they had room to run. Requiring the current tick AND
    the one before it to both fail to make a fresh high is a much
    stronger, less noise-sensitive stall confirmation - a real
    plateau, not a single wobble.

    Fails CLOSED (False, doesn't force an exit) with no history -
    the opposite convention from the entry-side stall check, since
    an unknown momentum read should never itself trigger closing a
    position, only a confirmed stall should.
    """
    window = self.volatility_price_history.get(symbol)
    if not window:
        return False
    samples = list(window)
    if samples and Decimal(str(samples[-1])) == price:
        samples = samples[:-1]
    if len(samples) < 2:
        return False
    previous = Decimal(str(samples[-1]))
    before_that = Decimal(str(samples[-2]))
    if previous <= 0 or before_that <= 0:
        return False
    return price <= previous <= before_that


def volatility_scalp_momentum_stalling_short(
    self, symbol: str, price: Decimal
) -> bool:
    """Mirror of volatility_scalp_momentum_stalling for a SHORT
    position: a short profits as price falls, so "still running"
    means still making fresh LOWS - stalling means two consecutive
    ticks failing to make a fresh low, the exact opposite direction
    of the long-side check.
    """
    window = self.volatility_price_history.get(symbol)
    if not window:
        return False
    samples = list(window)
    if samples and Decimal(str(samples[-1])) == price:
        samples = samples[:-1]
    if len(samples) < 2:
        return False
    previous = Decimal(str(samples[-1]))
    before_that = Decimal(str(samples[-2]))
    if previous <= 0 or before_that <= 0:
        return False
    return price >= previous >= before_that


def _synthetic_bars(self, symbol: str) -> list[dict]:
    """Buckets the rolling tick-price window (volatility_price_history)
    into fixed-size synthetic OHLC bars, HEIKIN_ASHI_BAR_SAMPLES ticks
    per bar - no separate bar/candle feed exists here, so this is the
    only OHLC series available to build Heikin-Ashi or Parabolic SAR
    from.

    Degrades gracefully instead of requiring the FULL
    heikin_ashi_bar_count * heikin_ashi_bar_samples history: uses
    however many complete buckets the window (bounded by
    volatility_scalp_lookback_samples, which can be smaller than
    that product) actually holds, capped at heikin_ashi_bar_count,
    and always bucketed from the most recent samples so a partial
    trailing bar is never included. Requiring the exact full count
    would mean these signals could never fire at all whenever the
    window is shorter than bar_samples * bar_count - not a rare
    edge case with the two configs' actual defaults. Empty list
    when there isn't even 2 full buckets' worth of history yet.
    """
    window = self.volatility_price_history.get(symbol)
    if not window:
        return []
    bar_samples = self.config.heikin_ashi_bar_samples
    bar_count = self.config.heikin_ashi_bar_count
    samples = list(window)
    usable = len(samples) - (len(samples) % bar_samples)
    if usable < bar_samples * 2:
        return []
    max_samples = min(usable, bar_samples * bar_count)
    recent = samples[-max_samples:]
    bars = []
    for start in range(0, len(recent), bar_samples):
        chunk = recent[start : start + bar_samples]
        bars.append(
            {
                "open": chunk[0],
                "high": max(chunk),
                "low": min(chunk),
                "close": chunk[-1],
            }
        )
    return bars


def heikin_ashi_bullish_reversal_signal(self, symbol: str) -> bool:
    """True on a confirmed Heikin-Ashi bullish reversal: the most
    recent completed synthetic bar is bearish (red) immediately
    followed by a bullish (green) one with little/no lower wick on
    the green bar - HA's own "strength confirmed" reversal read,
    not just a single green print that could reverse again next
    tick. An ADDITIONAL alternative entry trigger alongside the dip
    and breakout signals (OR'd, not required) - by request, every
    extra qualifying signal should mean MORE trading opportunities.
    """
    bars = self._synthetic_bars(symbol)
    if len(bars) < 2:
        return False
    ha_bars = []
    prev_ha_open = None
    prev_ha_close = None
    for bar in bars:
        ha_close = (bar["open"] + bar["high"] + bar["low"] + bar["close"]) / 4
        if prev_ha_open is None:
            ha_open = (bar["open"] + bar["close"]) / 2
        else:
            ha_open = (prev_ha_open + prev_ha_close) / 2
        ha_low = min(bar["low"], ha_open, ha_close)
        ha_bars.append({"open": ha_open, "low": ha_low, "close": ha_close})
        prev_ha_open, prev_ha_close = ha_open, ha_close
    previous, current = ha_bars[-2], ha_bars[-1]
    if previous["close"] >= previous["open"]:
        return False
    if current["close"] <= current["open"]:
        return False
    body = current["close"] - current["open"]
    lower_wick = current["open"] - current["low"]
    return lower_wick <= body * 0.25


def dual_thrust_breakout_signal(self, symbol: str, price: Decimal) -> bool:
    """Opening-range-breakout entry signal, adapted from the classic
    Dual Thrust strategy: fires when price pushes above the rolling
    window's own recent local range (the same lookback the dip
    signal already uses) by VOLATILITY_SCALP_BREAKOUT_K times that
    range's own size - a fresh breakout to a new high with real
    range behind it, not a single-tick blip. Uses the SAME rolling
    window as the dip signal (not the separate synthetic-bar
    bucketing the Heikin-Ashi/SAR signals use) since a breakout
    needs to react to the very latest tick, not wait for a bucket to
    complete. An ADDITIONAL alternative entry trigger, OR'd with the
    dip signal - the mirror case of it (buy a fresh push to a new
    high, instead of buying a pullback).
    """
    window = self.volatility_price_history.get(symbol)
    if not window:
        return False
    samples = list(window)
    if samples and Decimal(str(samples[-1])) == price:
        samples = samples[:-1]
    if not samples:
        return False
    recent = samples[-self.VOLATILITY_SCALP_LOCAL_HIGH_SAMPLES:]
    range_high = Decimal(str(max(recent)))
    range_low = Decimal(str(min(recent)))
    session_range = range_high - range_low
    if session_range <= 0 or price <= 0:
        return False
    upper_band = range_high + session_range * self.config.volatility_scalp_breakout_k
    return price >= upper_band


def _parabolic_sar(bars: list[dict], af_step: float, af_max: float):
    """Standard Wilder Parabolic SAR over a synthetic bar series.
    Returns (sar_level, trend_is_up) computed through the last bar,
    or None if there isn't enough history. trend_is_up flips to
    False the moment a bar's low breaks the trailing SAR level -
    that flip IS the trailing-stop signal callers act on.
    """
    if len(bars) < 2:
        return None
    trend_up = bars[1]["close"] >= bars[0]["close"]
    sar = bars[0]["low"] if trend_up else bars[0]["high"]
    ep = bars[0]["high"] if trend_up else bars[0]["low"]
    af = af_step
    prev_bar = bars[0]
    for bar in bars[1:]:
        sar = sar + af * (ep - sar)
        if trend_up:
            # Clamped by the PRIOR bar only, never the current one -
            # folding bar["low"] into this clamp would pin sar to
            # exactly the current bar's own low, making "bar['low']
            # < sar" structurally impossible to ever trigger.
            sar = min(sar, prev_bar["low"])
            if bar["low"] < sar:
                trend_up = False
                sar = ep
                ep = bar["low"]
                af = af_step
            elif bar["high"] > ep:
                ep = bar["high"]
                af = min(af + af_step, af_max)
        else:
            sar = max(sar, prev_bar["high"])
            if bar["high"] > sar:
                trend_up = True
                sar = ep
                ep = bar["high"]
                af = af_step
            elif bar["low"] < ep:
                ep = bar["low"]
                af = min(af + af_step, af_max)
        prev_bar = bar
    return sar, trend_up


def parabolic_sar_exit_signal(self, symbol: str, price: Decimal) -> bool:
    """True when Parabolic SAR has flipped bearish (or the live tick
    has already pushed below the trailing SAR level even though the
    last completed synthetic bar hasn't confirmed it yet) - an
    ADDITIONAL exit trigger for a held volatility-scalp position,
    alongside (not instead of) the existing quick profit target.
    Either one can independently close the position, so a trend
    reversal locks in gains even before price clears the fixed
    percentage target.
    """
    bars = self._synthetic_bars(symbol)
    if len(bars) < 2:
        return False
    result = self._parabolic_sar(
        bars,
        float(self.config.parabolic_sar_af_step),
        float(self.config.parabolic_sar_af_max),
    )
    if result is None:
        return False
    sar, trend_up = result
    return (not trend_up) or float(price) < sar


def volatility_scalp_micro_exhaustion_confirmed(
    self, symbol: str, price: Decimal, moment: float
) -> bool:
    """4th confirmation gate, downstream of volatility_scalp_dip_
    signal specifically (breakout/reversal are unaffected) - by
    request, proves a dip-signal candidate is actual liquidity
    exhaustion (a sharp drop, a real bounce off the floor, and a
    volume spike that's already fading), not just "X% off a local
    high" on its own.

    Pure, side-effect-free read of state maintained by update_
    recent_tick_history/update_volume_delta - deliberately, since
    bot.py's existing gate-visibility pattern evaluates every
    condition TWICE per cycle (the real gate, and a diagnostic-only
    tuple that logs why entries aren't firing) - a stateful check
    here would double-count on every single cycle.

    Three AND'd conditions, all measured over the last
    VOLATILITY_SCALP_MICRO_EXHAUSTION_LOOKBACK_SECONDS of real
    elapsed time (moment is a time.monotonic() reading, same clock
    recent_tick_history's timestamps use):
    1. Velocity: price has dropped at least VELOCITY_PERCENT from
       the window's local high.
    2. Wick/absorption: price has already recovered at least
       WICK_RATIO of the window's full high-low range off the
       local low - proves a floor has already formed, not still
       falling.
    3. Volume: the most recent single-cycle volume-delta reading is
       at least VOLUME_MULTIPLIER times its OWN smoothed rolling
       baseline (volume_delta_ema) - a real capitulation spike
       against this symbol's typical recent trading rate, not a
       fixed number that means something different for a $0.30
       stock than a $30 one.

    Fails OPEN (True) with the filter disabled or insufficient
    data (fewer than 2 samples in the lookback window, or no
    volume-delta EMA yet) - same "no data -> don't block"
    convention as every other entry gate in this file.
    """
    if not self.config.volatility_scalp_micro_exhaustion_filter_enabled:
        return True
    if price is None or price <= 0:
        return True
    lookback = float(
        self.config.volatility_scalp_micro_exhaustion_lookback_seconds
    )
    samples = self.recent_tick_history.get(symbol)
    if not samples:
        return True
    valid_prices = [p for t, p in samples if moment - t <= lookback]
    if len(valid_prices) < 2:
        return True
    local_high = max(valid_prices)
    local_low = min(valid_prices)
    if local_high <= 0:
        return True
    velocity_drop = (price - local_high) / local_high
    price_range = local_high - local_low
    wick_ratio = (price - local_low) / price_range if price_range > 0 else Decimal("0")
    volume_ema = self.volume_delta_ema.get(symbol)
    latest_delta = self.volume_delta_latest.get(symbol)
    if volume_ema is None or volume_ema <= 0 or latest_delta is None:
        return True
    velocity_threshold = (
        self.config.volatility_scalp_micro_exhaustion_velocity_percent
    )
    wick_threshold = self.config.volatility_scalp_micro_exhaustion_wick_ratio
    volume_threshold = (
        volume_ema * self.config.volatility_scalp_micro_exhaustion_volume_multiplier
    )
    return (
        velocity_drop <= -velocity_threshold
        and wick_ratio >= wick_threshold
        and latest_delta >= volume_threshold
    )
