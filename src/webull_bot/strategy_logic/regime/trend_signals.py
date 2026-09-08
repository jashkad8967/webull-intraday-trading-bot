from decimal import Decimal

from webull_bot.strategy_logic.constants import OBI_BUY_THRESHOLD


def _ema(values: list[float], period: int) -> float:
    weight = 2 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = value * weight + result * (1 - weight)
    return result


def trend_signal(self, key: str, price: Decimal) -> str:
    values = self.history[key]
    values.append(float(price))
    self.tick_history[key].append(float(price))
    slow = self.config.ema_slow_period
    fast = self.config.ema_fast_period
    if len(values) < slow + 1:
        self.trend_streak[key] = 0
        return "HOLD"
    series = list(values)
    previous = series[:-1]
    old_spread = self._ema(previous[-slow:], fast) - self._ema(
        previous[-slow:],
        slow,
    )
    new_spread = self._ema(series[-slow:], fast) - self._ema(
        series[-slow:],
        slow,
    )
    if (old_spread > 0) != (new_spread > 0):
        # A stock that keeps flipping direction is exactly the kind of
        # choppy, mean-reverting mover that produces repeated small
        # scalps in a session - track it so priority_score can favor it
        # over a name that only trended once and went flat.
        symbol = key.split(":", 1)[-1]
        self.crossover_counts[symbol] += 1
    if new_spread <= 0:
        # A fresh bearish cross (was bullish/flat, now bearish) is the
        # short-side mirror of the "BUY" fresh-cross case below.
        # stock_decision only acts on this when SHORT_SELLING_ENABLED
        # is on; it's always computed here regardless so the signal is
        # available the moment shorting gets turned on without waiting
        # on fresh history.
        #
        # Mirrors BUY's reenter_on_trend continuation below - a short
        # entry also requires VWAP/SMA-trend/extension/tick-direction
        # to all align, and requiring that alignment on the exact
        # single tick of the fresh cross (with no further chances
        # after) made a real short entry all but impossible in
        # practice. trend_streak is negative for a continuing
        # downtrend, positive for a continuing uptrend, so both
        # directions share the one counter.
        if old_spread > 0:
            self.trend_streak[key] = 0
            # By request, after live evidence: 6 of 7 open general-
            # path positions were underwater at once, entered right
            # as a fresh cross fired - the exact instant a real
            # reversal and a false-signal whipsaw look identical.
            # Firing the SAME tick the cross happens means "buying
            # right when the dip starts," not after it's actually
            # confirmed. reenter_on_trend=False is the one case
            # that still fires immediately here (its whole "fires
            # once per fresh cross" design already has no
            # continuation mechanism to delay into).
            if not self.config.reenter_on_trend:
                return "SHORT"
        current_streak = self.trend_streak.get(key, 0)
        self.trend_streak[key] = current_streak - 1 if current_streak <= 0 else -1
        if (
            self.config.reenter_on_trend
            and -self.trend_streak[key] >= self.config.reenter_confirmation_polls
        ):
            return "SHORT"
        return "HOLD"
    if old_spread <= 0:
        self.trend_streak[key] = 0
        # Same one-extra-tick confirmation delay as the SHORT branch
        # above, for the same reason - see its comment.
        if not self.config.reenter_on_trend:
            return "BUY"
    self.trend_streak[key] = self.trend_streak.get(key, 0) + 1
    if (
        self.config.reenter_on_trend
        and self.trend_streak[key] >= self.config.reenter_confirmation_polls
    ):
        return "BUY"
    return "HOLD"


def option_direction_signal(self, key: str, price: Decimal) -> str:
    """Dual-sided sibling of trend_signal for options: a stock strategy
    only ever needs a bullish entry, but a call needs the same fresh
    bullish EMA cross while a put needs the mirror-image fresh bearish
    cross.

    By explicit request ("screw the direction signal, i feel maybe
    there are too many barriers"), then clarified as "re-fire on a
    continued trend, not just the fresh cross": this used to go
    quiet the instant the fresh-cross cycle passed, even if the
    underlying kept trending - given how the option-contract quote
    rotation and the outer loop's own cadence both compete for the
    same cycle, a real fresh cross could easily land on a cycle
    that never actually reaches the gate-check loop for that
    contract, and then never fire again until the NEXT fresh cross.
    Now mirrors trend_signal's own reenter_on_trend/trend_streak
    continuation exactly (same config, same "OPTU:"-namespaced key
    so it shares no state with the stock side's "STOCK:" keys) -
    a signal keeps re-firing every cycle the trend continues, once
    it's held for REENTER_CONFIRMATION_POLLS cycles.
    """
    values = self.history[key]
    values.append(float(price))
    self.tick_history[key].append(float(price))
    slow = self.config.ema_slow_period
    fast = self.config.ema_fast_period
    if len(values) < slow + 1:
        self.trend_streak[key] = 0
        return "HOLD"
    series = list(values)
    previous = series[:-1]
    old_spread = self._ema(previous[-slow:], fast) - self._ema(
        previous[-slow:],
        slow,
    )
    new_spread = self._ema(series[-slow:], fast) - self._ema(
        series[-slow:],
        slow,
    )
    if new_spread <= 0:
        if old_spread > 0:
            self.trend_streak[key] = 0
            return "PUT"
        current_streak = self.trend_streak.get(key, 0)
        self.trend_streak[key] = current_streak - 1 if current_streak <= 0 else -1
        if (
            self.config.reenter_on_trend
            and -self.trend_streak[key] >= self.config.reenter_confirmation_polls
        ):
            return "PUT"
        return "HOLD"
    if old_spread <= 0:
        self.trend_streak[key] = 0
        return "CALL"
    self.trend_streak[key] = self.trend_streak.get(key, 0) + 1
    if (
        self.config.reenter_on_trend
        and self.trend_streak[key] >= self.config.reenter_confirmation_polls
    ):
        return "CALL"
    return "HOLD"


def option_entry_confirmed(
    self,
    direction: str,
    tick_score: Decimal | None,
    obi_score: Decimal | None,
) -> bool:
    """Secondary confirmation for an option_direction_signal read, same
    "no data -> don't block" convention as every other entry gate here.
    tick_score is -1..+1 (see tick_direction_score); obi_score is
    bid/(bid+ask) depth imbalance (see obi_supports_entry) and is only
    ever passed when a depth snapshot happened to already be cached for
    this underlying this cycle.
    """
    if direction not in ("CALL", "PUT"):
        return False
    if tick_score is not None:
        if direction == "CALL" and tick_score <= 0:
            return False
        if direction == "PUT" and tick_score >= 0:
            return False
    if obi_score is not None:
        if direction == "CALL" and obi_score < OBI_BUY_THRESHOLD:
            return False
        if direction == "PUT" and obi_score > Decimal("1") - OBI_BUY_THRESHOLD:
            return False
    return True


def tick_direction_score(self, key: str) -> Decimal:
    """Net upticks vs downticks over the recent poll-to-poll price
    prints, as a proxy for order-flow imbalance - real bid/ask depth
    isn't available from the quote feed. Ranges -1 (all downticks) to
    +1 (all upticks); 0 when there's too little data or no net
    direction (flat prints, or an equal mix of up/down).
    """
    values = list(self.tick_history.get(key, ()))
    if len(values) < 2:
        return Decimal("0")
    up = down = 0
    for previous, current in zip(values, values[1:]):
        if current > previous:
            up += 1
        elif current < previous:
            down += 1
    total = up + down
    if total == 0:
        return Decimal("0")
    return Decimal(up - down) / Decimal(total)


def tick_direction_ok(
    self,
    key: str,
    direction: str = "BUY",
    idle_relaxation_amount: Decimal = Decimal("0"),
) -> bool:
    if not self.config.tick_direction_enabled:
        return True
    score = self.tick_direction_score(key)
    threshold = self.config.tick_direction_veto_threshold - idle_relaxation_amount
    if direction == "SHORT":
        return score <= -threshold
    return score >= threshold
