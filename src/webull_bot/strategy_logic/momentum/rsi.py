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
