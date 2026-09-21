import time
from decimal import Decimal

ZERO = Decimal("0")
ONE = Decimal("1")


def update_net_pressure(self, symbol: str, price: Decimal, moment: float) -> None:
    """By explicit request: "see the momentum by the buys and sells."

    HONEST CONSTRAINT, stated up front because it bounds what this
    function can possibly be: update_volume_delta (vwap_trend.py) is
    UNSIGNED. It diffs Webull's cumulative day-volume snapshot field,
    so it measures PARTICIPATION, not aggressor side. This API exposes
    no bid/ask-tagged tape, so a true buy-volume-vs-sell-volume split
    is not obtainable here and this function does not pretend to
    compute one.

    What it builds instead is the best proxy the existing data
    supports, combining two things already tracked every cycle:

    - DIRECTION, from the price change over the same interval the
      volume sample covers. Price rose across that interval => buyers
      were the ones lifting; price fell => sellers were hitting.
    - CONVICTION, from volume_delta_latest against its own EMA
      baseline. Scaled so an average-volume interval (ratio 1.0)
      contributes ZERO conviction and a 2x interval contributes the
      full 1.0 - the same 2x bar the relative-volume research puts at
      the boundary between negative and positive expectancy.

    The product lands in -1..+1: strongly positive means buyers are in
    control on real volume, strongly negative means sellers are, and
    anything near zero means the move carries no participation behind
    it regardless of how big the price change looks.

    Must be called AFTER update_volume_delta for the same symbol on
    the same cycle (it reads that cycle's delta), and is the only
    writer of pressure state - the read helpers below are pure, so
    bot.py's existing habit of evaluating every gate twice per cycle
    (once for real, once for the diagnostic log) can never double-
    count into this.
    """
    if not self.config.pressure_enabled:
        return
    if price is None or price <= 0:
        return
    previous_price = self.pressure_price_baseline.get(symbol)
    self.pressure_price_baseline[symbol] = price
    if previous_price is None or previous_price <= 0:
        # First sample for this symbol - nothing to diff against yet,
        # same deploy-resilient initialization update_volume_delta
        # itself uses.
        return
    volume_ema = self.volume_delta_ema.get(symbol)
    latest_delta = self.volume_delta_latest.get(symbol)
    if volume_ema is None or volume_ema <= 0 or latest_delta is None:
        return
    ratio = Decimal(latest_delta) / Decimal(volume_ema)
    conviction = min(ONE, max(ZERO, ratio - ONE))
    if price > previous_price:
        pressure = conviction
    elif price < previous_price:
        pressure = -conviction
    else:
        pressure = ZERO
    history = self.pressure_history[symbol]
    history.append((moment, pressure))


def net_pressure(self, symbol: str) -> Decimal | None:
    """The most recent pressure reading, or None when there isn't a
    fresh one. Stale readings are discarded rather than returned -
    a pressure value from several minutes ago is not evidence about
    what buyers and sellers are doing right now.
    """
    if not self.config.pressure_enabled:
        return None
    history = self.pressure_history.get(symbol)
    if not history:
        return None
    moment, pressure = history[-1]
    if time.monotonic() - moment > self.config.pressure_history_seconds:
        return None
    return pressure


def pressure_supports_entry(self, symbol: str, option_type: str) -> bool:
    """Entry-timing gate - by request: "make sure the entry and exit
    happens at the right time according to the momentum."

    A CALL wants buyers in control, a PUT wants sellers. Upgrades the
    existing dip/rip entry triggers from a price-only read to
    price-plus-participation: a dip that nobody is buying is not a
    dip worth buying a call into.

    Fails OPEN (True) with the filter off or no fresh reading - the
    same "no data, don't block" convention every other gate in this
    codebase uses.
    """
    if not self.config.pressure_enabled:
        return True
    pressure = self.net_pressure(symbol)
    if pressure is None:
        return True
    minimum = self.config.pressure_min_for_entry
    if str(option_type).upper() == "CALL":
        return pressure >= minimum
    return pressure <= -minimum


def pressure_flipped_against(self, symbol: str, option_type: str) -> bool:
    """Exit-timing signal: participation has turned against an open
    position hard enough to read as momentum exhaustion - the "tip of
    momentum" the flip logic is meant to catch.

    Joins rsi_divergence and the resistance check as a PROFIT-
    PROTECTION trigger only; callers gate it on the position already
    showing a gain, so this can never be the thing that cuts a loser
    (STOP owns loss-cutting on its own separate terms).

    Fails CLOSED (False) with no fresh reading - an exit trigger that
    fired on missing data would close real positions on nothing.
    """
    if not self.config.pressure_enabled:
        return False
    if not self.config.pressure_flip_exit_enabled:
        return False
    pressure = self.net_pressure(symbol)
    if pressure is None:
        return False
    threshold = self.config.pressure_flip_exit_threshold
    if str(option_type).upper() == "CALL":
        return pressure <= -threshold
    return pressure >= threshold
