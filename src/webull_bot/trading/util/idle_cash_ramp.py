import time
from decimal import Decimal


def idle_cash_ramp_progress(self, buying_power: Decimal) -> Decimal:
    """0..1 - how far along the idle-cash gate-relaxation ramp the bot
    currently is. Keeping buying_power (already net of
    MIN_CASH_RESERVE_DOLLARS) deployed outranks entry quality, so the
    longer it sits unspent, the more entry_spread_ok/entry_extension_ok/
    vwap_supports_entry/tick_direction_ok loosen - see their
    idle_relaxation_multiplier parameter. Resets to 0 the moment
    record_trade() sees a new BUY/SHORT/MANUAL_BUY fill that counts
    toward this ramp - volatility-scalp fills deliberately don't (see
    record_trade's counts_toward_idle_cash_ramp), since this ramp
    only ever loosens the GENERAL strategy's own gates and scalp
    activity firing every few minutes was otherwise starving it
    from ever advancing, even while real buying power sat unused
    for hours.
    """
    if not self.config.idle_cash_relaxation_enabled or buying_power <= 0:
        return Decimal("0")
    idle_seconds = time.monotonic() - self.last_capital_deployed_at
    grace = float(self.config.idle_cash_grace_seconds)
    if idle_seconds <= grace:
        return Decimal("0")
    ramp = float(self.config.idle_cash_ramp_seconds)
    return Decimal(str(min(1.0, (idle_seconds - grace) / ramp)))
