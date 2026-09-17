from decimal import Decimal


def relative_strength_index(self, symbol: str) -> Decimal | None:
    """By request: "find common instances historically of dips...
    known patterns... use that to also decide on an entry or
    exit." RSI is the single most widely documented, historically-
    validated way to identify a statistically overextended dip
    (oversold, < 30) or peak (overbought, > 70) - standard Wilder-
    style calculation, reused against the same recent_tick_history
    (real (timestamp, price) samples, already populated for every
    scanned symbol) the micro-exhaustion check above uses, rather
    than adding a new data source.

    Uses whatever samples are available up to RSI_PERIOD (default
    14, the classic convention) - returns None (fails open,
    callers already treat that as "no data, don't block") with
    fewer than 3 price changes to measure, same convention as
    every other gate in this file.
    """
    samples = self.recent_tick_history.get(symbol)
    if not samples:
        return None
    prices = [p for _, p in samples][-(self.config.rsi_period + 1) :]
    if len(prices) < 3:
        return None
    gains = Decimal("0")
    losses = Decimal("0")
    count = 0
    for previous, current in zip(prices, prices[1:]):
        change = current - previous
        if change > 0:
            gains += change
        elif change < 0:
            losses += -change
        count += 1
    if count == 0:
        return None
    avg_gain = gains / count
    avg_loss = losses / count
    if avg_loss == 0:
        return Decimal("100") if avg_gain > 0 else Decimal("50")
    rs = avg_gain / avg_loss
    return Decimal("100") - (Decimal("100") / (Decimal("1") + rs))


def rsi_supports_entry(self, symbol: str) -> bool:
    """True when RSI confirms a genuine statistical dip (oversold,
    below RSI_OVERSOLD_THRESHOLD) or there isn't enough data yet -
    fails open, same "no data -> don't block" convention as every
    other entry gate. Deliberately an AND alongside volatility_
    scalp_dip_signal (a local-high pullback %) and volatility_
    scalp_micro_exhaustion_confirmed, not a replacement - RSI adds
    the historically-standard "is this dip statistically extreme"
    read on top of the existing "has price actually pulled back
    and shown a floor" reads.
    """
    if not self.config.rsi_filter_enabled:
        return True
    rsi = self.relative_strength_index(symbol)
    if rsi is None:
        return True
    return rsi <= self.config.rsi_oversold_threshold


def rsi_overbought_exit(self, symbol: str) -> bool:
    """True when RSI confirms a genuine statistical peak
    (overbought, above RSI_OVERBOUGHT_THRESHOLD) - an additional,
    historically-standard "sell into strength" signal alongside
    the existing momentum-stall and Parabolic SAR reversal exits
    in volatility_scalp_exit_override. False (never blocks a
    profitable exit) with the filter disabled or insufficient
    data.
    """
    if not self.config.rsi_filter_enabled:
        return False
    rsi = self.relative_strength_index(symbol)
    if rsi is None:
        return False
    return rsi >= self.config.rsi_overbought_threshold


def update_recent_rsi_history(self, symbol: str, moment: float) -> None:
    """Records the current RSI reading alongside recent_tick_history's
    own price samples, at the same per-cycle cadence (same caller,
    same moment) - see rsi_divergence's docstring for why this needs
    RSI values tied to specific past moments rather than just the
    current single reading relative_strength_index already provides.
    No-ops (same "insufficient data" convention as every RSI read
    here) when there isn't yet a real RSI value to record.
    """
    rsi = self.relative_strength_index(symbol)
    if rsi is None:
        return
    self.recent_rsi_history[symbol].append((moment, rsi))


def rsi_divergence(self, symbol: str, price: Decimal, moment: float) -> str:
    """By explicit request (momentum-shift overview + walkthrough):
    price/RSI divergence - the price makes a fresh high (or low) but
    RSI does NOT confirm it with a matching one - is a distinct,
    earlier-warning pattern from a flat overbought/oversold threshold.
    The walkthrough's own reference trade is a divergence, not a
    threshold breach: at 09:55 price makes a HIGHER high (153.40 vs
    153.10) while RSI makes a LOWER high (74 vs 78) - the mismatch,
    not RSI's absolute level (74 isn't even still above 70), is what
    actually flags the shift.

    Returns "BEARISH" (price higher-high, RSI lower-high by at least
    rsi_divergence_min_gap), "BULLISH" (mirror image at a fresh low),
    or "NONE" - fails open/no-signal with the filter disabled or
    insufficient recent_tick_history/recent_rsi_history, same
    convention as every other gate in this file. Looks only at
    samples within rsi_divergence_lookback_seconds of `moment`, same
    real-elapsed-time filtering volatility_scalp_micro_exhaustion_
    confirmed already uses on this same recent_tick_history structure.
    """
    try:
        if not self.config.rsi_divergence_exit_enabled:
            return "NONE"
        if price is None or price <= 0:
            return "NONE"
        current_rsi = self.relative_strength_index(symbol)
        if current_rsi is None:
            return "NONE"
        price_samples = self.recent_tick_history.get(symbol)
        rsi_samples = self.recent_rsi_history.get(symbol)
        if not price_samples or not rsi_samples:
            return "NONE"
        lookback = float(self.config.rsi_divergence_lookback_seconds)
        valid_prices = [(t, p) for t, p in price_samples if moment - t <= lookback]
        valid_rsi = [(t, r) for t, r in rsi_samples if moment - t <= lookback]
        if len(valid_prices) < 2 or len(valid_rsi) < 2:
            return "NONE"
        min_gap = self.config.rsi_divergence_min_gap
        high_t, high_p = max(valid_prices, key=lambda sample: sample[1])
        if price >= high_p:
            rsi_at_high = min(
                valid_rsi, key=lambda sample: abs(sample[0] - high_t)
            )[1]
            if current_rsi < rsi_at_high - min_gap:
                return "BEARISH"
        low_t, low_p = min(valid_prices, key=lambda sample: sample[1])
        if price <= low_p:
            rsi_at_low = min(
                valid_rsi, key=lambda sample: abs(sample[0] - low_t)
            )[1]
            if current_rsi > rsi_at_low + min_gap:
                return "BULLISH"
        return "NONE"
    except AttributeError:
        # Fails open (no signal) on an incomplete config/state object -
        # same "no data, don't block" convention as every other gate
        # in this file, just extended to cover a fixture/caller that
        # hasn't set up this specific optional field yet, rather than
        # crashing a caller that only expected the ORIGINAL RSI gates.
        return "NONE"
