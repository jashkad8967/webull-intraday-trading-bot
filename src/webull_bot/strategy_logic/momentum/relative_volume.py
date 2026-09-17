def relative_volume_ok(self, symbol: str) -> bool:
    """By request (momentum-shift overview): a general entry-quality
    filter - is this candidate's current move actually backed by real
    volume, or is it thin/low-conviction? The codebase already tracks
    exactly this (volume_delta_latest/volume_delta_ema, updated every
    cycle by update_volume_delta - see stock_symbol_processing.py) for
    volatility_scalp_micro_exhaustion_confirmed's sharper reversal-
    confirmation gate, but nothing applied it to the general trend-
    following entry path or option entries. Reuses that SAME state
    (no new volume pipeline) with a deliberately gentler multiplier
    (rvol_entry_multiplier, default 1.2x vs micro-exhaustion's own
    sharper spike bar) - this is a baseline conviction check, not a
    sharp reversal-confirmation gate.

    Fails open (True) with the filter disabled or missing/zero
    volume-delta data - same "no data, don't block" convention as
    every other gate in this file.
    """
    try:
        if not self.config.rvol_entry_filter_enabled:
            return True
        volume_ema = self.volume_delta_ema.get(symbol)
        latest_delta = self.volume_delta_latest.get(symbol)
        if volume_ema is None or volume_ema <= 0 or latest_delta is None:
            return True
        return latest_delta >= volume_ema * self.config.rvol_entry_multiplier
    except AttributeError:
        # Fails open on an incomplete config/state object - same
        # convention as rsi_divergence.
        return True
