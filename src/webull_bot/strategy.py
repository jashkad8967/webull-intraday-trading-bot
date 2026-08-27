import math
from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

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


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    target_price: Decimal | None = None


@dataclass(frozen=True)
class PortfolioDecision:
    action: str
    reason: str
    losing_positions: int = 0
    total_loss: Decimal = Decimal("0")


class TradingStrategy:
    """Owns selection, sizing, entry, exit, and portfolio policy."""

    def __init__(self, config):
        if config.ema_fast_period >= config.ema_slow_period:
            raise ValueError("fast EMA must be lower than slow EMA")
        self.config = config
        self.history = defaultdict(
            lambda: deque(maxlen=config.ema_slow_period + 1)
        )
        self.activity: dict[str, float] = {}
        self.prices: dict[str, Decimal] = {}
        self.metrics: dict[str, dict] = {}
        self.selection_buckets: dict[str, str] = {}
        self.trend_streak: dict[str, int] = {}
        self.vwap_state: dict[str, dict] = {}
        self.crossover_counts: dict[str, int] = defaultdict(int)
        self.tick_history: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=config.tick_direction_window)
        )
        # Higher-timeframe SMA trend reference (real daily-bar closes, not
        # derived from tick polls) - refreshed once daily by
        # AutoTrader.refresh_sma_trend, deliberately NOT cleared by
        # clear_market_state's once-daily reset so a failed refresh keeps
        # yesterday's (still roughly valid) SMA rather than going empty.
        self.sma_trend: dict[str, Decimal] = {}
        # Webull's most-active screener, refreshed independently every
        # MARKET_PULSE_REFRESH_SECONDS by AutoTrader.refresh_market_pulse -
        # not tied to clear_market_state's once-daily reset, same as
        # market_pulse_cache itself isn't.
        self.most_active_symbols: set[str] = set()
        # Analyst target-price/rating soft priority nudge, refreshed
        # independently (and much more slowly) by AutoTrader's
        # AnalystDataService - see analyst_priority_bonus. Not tied to
        # clear_market_state's once-daily reset, same as most_active_symbols
        # and sma_trend isn't: this is background-fetched on its own
        # gradual cadence and shouldn't be thrown away just because the
        # trading day rolled over.
        self.analyst_priority: dict[str, float] = {}
        # Volatility-scalp: a rolling window of raw prices per symbol
        # (independent of self.prices, which only ever holds the latest
        # one) - see update_stock_snapshot, realized_volatility_percent,
        # volatility_scalp_dip_signal.
        self.volatility_price_history: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=config.volatility_scalp_lookback_samples)
        )

    def clear_market_state(self) -> None:
        self.activity.clear()
        self.prices.clear()
        self.metrics.clear()
        self.selection_buckets.clear()
        self.vwap_state.clear()
        self.crossover_counts.clear()
        self.tick_history.clear()
        self.volatility_price_history.clear()

    @staticmethod
    def rotating_batch(items: list, cursor: int, batch_size: int) -> tuple[list, int]:
        if not items:
            return [], 0
        size = min(batch_size, len(items))
        batch = [items[(cursor + offset) % len(items)] for offset in range(size)]
        return batch, (cursor + size) % len(items)

    @staticmethod
    def quote_number(quote: dict, *fields: str) -> float:
        for field in fields:
            try:
                value = float(quote.get(field, ""))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                return value
        return 0.0

    def update_stock_snapshot(self, quote: dict, price: Decimal) -> None:
        symbol = str(quote.get("symbol", "")).upper()
        if not symbol:
            return
        regular_volume = self.quote_number(quote, "volume")
        extended_volume = self.quote_number(quote, "extend_hour_volume")
        volume = max(0.0, regular_volume + extended_volume)
        movement = max(
            abs(self.quote_number(quote, "change_ratio")),
            abs(self.quote_number(quote, "extend_hour_change_ratio")),
        )
        high = self.quote_number(quote, "extend_hour_high", "high")
        low = self.quote_number(quote, "extend_hour_low", "low")
        bid = self.quote_number(quote, "bid")
        ask = self.quote_number(quote, "ask")
        midpoint = (bid + ask) / 2 if bid > 0 and ask >= bid else 0.0
        spread_percent = (
            ((ask - bid) / midpoint) * 100
            if midpoint > 0
            else 0.0
        )
        range_ratio = (
            (high - low) / float(price)
            if price > 0 and high >= low
            else 0.0
        )
        activity = (
            math.log10(1.0 + volume)
            + 50.0 * movement
            + 25.0 * max(0.0, range_ratio)
        )
        self.prices[symbol] = price
        self.activity[symbol] = activity
        self.metrics[symbol] = {
            "volume": int(volume),
            "change_ratio": movement,
            "bid": bid,
            "ask": ask,
            "spread_percent": round(spread_percent, 4),
            "range_ratio": round(max(0.0, range_ratio), 4),
            "high": high,
            "low": low,
            "activity_score": activity,
        }
        self._update_vwap(symbol, price, volume)
        if price > 0:
            self.volatility_price_history[symbol].append(float(price))

    # Below this many samples, a stdev estimate is too noisy to trust -
    # not configurable (unlike the window length itself), same reasoning
    # as the other hardcoded quality-filter thresholds up top.
    VOLATILITY_SCALP_MIN_SAMPLES = 5
    # How many of the most recent samples count as the "local high" a dip
    # is measured against - see volatility_scalp_dip_signal.
    VOLATILITY_SCALP_LOCAL_HIGH_SAMPLES = 5

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

        1. Requires the two ticks immediately before now to have been a
           REAL consecutive decline (strictly falling, not flat) - this
           is what proves a genuine falling-knife dip actually happened,
           not noise. Without this, "stalled" and "never was falling in
           the first place" were indistinguishable.
        2. Then fires on the very FIRST tick that is no longer lower
           than the one before it - a single-tick turn confirmation, so
           entry lands right at the reversal instead of waiting out
           several more ticks of confirmation and missing the move.

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
        before_that = Decimal(str(samples[-2]))
        earlier_still = Decimal(str(samples[-3]))
        if previous <= 0 or before_that <= 0 or earlier_still <= 0:
            return True
        was_falling = earlier_still > before_that > previous
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

    @staticmethod
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

    def volatility_scalp_exit_override(
        self,
        decision: Decision,
        quantity,
        average_cost: Decimal,
        price: Decimal,
        averaging_available: bool = True,
        symbol: str = "",
    ) -> Decision:
        """Called for any held position currently in the volatility-scalp
        cohort - not just ones opened via the dip-buy path (a position
        already held through the normal trend entry gets the same fast
        profit-take once its symbol is picked into the cohort).

        Two things, by explicit request: lets its own small, fast profit
        target fire the exit earlier than stock_decision's normal
        adaptive target would (promotes a HOLD to PROFIT once price
        clears the quick target) - this always applies; and suppresses a
        LOSS entirely - "focus less on the stop loss" - since a dip on
        this cohort is meant to be averaged into instead of stopped out
        of. average_cost keeps reflecting the broker's own blended cost
        across every averaging buy, so the quick-target sell price
        naturally sits above the new average once one fires, with no
        extra tracking needed here.

        The LOSS suppression only applies when averaging_available is
        True (the position is actually eligible for AutoTrader's
        averaging-buy entry path, i.e. it was opened via the dip-buy
        path in the first place) - sanity-check fix: a position that
        got the fast profit-take purely because its symbol is in the
        cohort, but was never opened by this strategy, has no averaging-
        down recovery plan behind it. Suppressing its stop-loss with
        nothing else backing it up would just leave it bleeding
        indefinitely with no path back to even. Its normal stop-loss
        stays fully in effect instead.

        A THIRD, independent way to reach PROFIT: a Parabolic SAR trend
        reversal (see parabolic_sar_exit_signal), but ONLY once price
        has at least cleared cost - this locks in a reversal early
        rather than waiting for the full quick target, without ever
        turning into a second, backdoor stop-loss (which the LOSS
        suppression above deliberately disables for this cohort).

        A FOURTH, even more eager way to reach PROFIT: by request, "if
        there is a profit and it doesn't seem to be going much higher,
        then sell it off... before the next dip." Recalibrated by a
        later request - "too trigger happy to sell... not capturing the
        profits when it can" - this used to fire on ANY real profit at
        all (even a fraction of a cent) combined with a single stalled
        tick, which was cashing out winners before they had room to
        run. Now requires price to have already covered at least
        VOLATILITY_SCALP_MOMENTUM_STALL_MIN_PROFIT_FRACTION of the full
        distance from cost to the quick target (default 60%) AND a
        stronger two-tick stall confirmation (see
        volatility_scalp_momentum_stalling) before taking the early
        exit. Checked last (after the bigger, slower targets) so a
        position that's still climbing - or hasn't earned enough of the
        move yet - keeps riding toward the larger target instead of
        being cashed out early.
        """
        if quantity <= 0 or average_cost <= 0:
            return decision
        if decision.action == "LOSS" and averaging_available:
            # Research finding (freqtrade's documented DCA pattern,
            # compared against ours after "basically only taking
            # losses" was reported live): a mature DCA implementation
            # NEVER fully suppresses the stop-loss during averaging - it
            # keeps a wide-but-always-active hard stop live from entry,
            # sized to not fight the DCA ladder, specifically as a
            # catastrophic-loss backstop distinct from the per-level
            # re-buy logic. Ours removed the stop-loss entirely instead,
            # leaving an averaging-eligible position with NO risk
            # ceiling until all averaging attempts were exhausted - the
            # likely root cause of realized losses running several
            # times the size of this cohort's own tiny profit-takes.
            # VOLATILITY_SCALP_HARD_STOP_PERCENT (default 5%) restores
            # that backstop: still lets a normal-sized dip average down
            # freely (the 5-level, 0.2%-per-level DCA ladder covers
            # about 1% of adverse movement), but a drop beyond it means
            # a real breakdown, not a normal dip - the actual stop-loss
            # is allowed through instead of being suppressed forever.
            drop = (average_cost - price) / average_cost
            if drop < self.config.volatility_scalp_hard_stop_percent:
                return Decision(
                    "HOLD",
                    "volatility scalp - averaging down instead of stopping out",
                    price,
                )
            return decision
        if decision.action != "HOLD":
            return decision
        target = self.volatility_scalp_target_price(average_cost)
        if price >= target:
            return Decision("PROFIT", "volatility scalp quick target reached", target)
        if symbol and price >= average_cost:
            if self.parabolic_sar_exit_signal(symbol, price):
                return Decision(
                    "PROFIT", "parabolic SAR trend reversal exit", price
                )
            min_stall_price = average_cost + (target - average_cost) * (
                self.config.volatility_scalp_momentum_stall_min_profit_fraction
            )
            if price >= min_stall_price and self.volatility_scalp_momentum_stalling(
                symbol, price
            ):
                return Decision(
                    "PROFIT",
                    "momentum stalling on a profitable position - selling "
                    "ahead of the next dip",
                    price,
                )
        return decision

    def volatility_scalp_average_down_signal(
        self, price: Decimal, average_cost: Decimal, level: int = 0
    ) -> bool:
        """True when price has dropped enough below the position's OWN
        average cost (not the rolling window's local high, unlike the
        fresh-entry dip signal) - "if they dip a lot after you buy,
        average it out with another buy."

        Structural fix (not a same-day band-aid): compared against
        freqtrade's documented DCA pattern after watching a real
        position (BTCT) burn through its averaging buys at 1.79 -> 1.78
        -> essentially the same price, no real risk-reduction gained
        per add. freqtrade's own docs warn a tight, non-widening re-buy
        trigger "runs out of money" refilling into noise rather than a
        real dip. The required drop now WIDENS with each successive
        averaging level (0-indexed: the first averaging buy still uses
        the base VOLATILITY_SCALP_DIP_ENTRY_PERCENT, the second requires
        1.5x that, the third 2x, etc., via
        VOLATILITY_SCALP_AVERAGING_STEP_MULTIPLIER) - a position that's
        already averaged down several times needs a genuinely bigger
        move to justify yet another add, not just another noise-level
        tick, and the whole averaging ladder now spans a real range
        instead of exhausting itself within ~1% of movement.
        """
        if average_cost <= 0 or price <= 0:
            return False
        drop = (average_cost - price) / average_cost
        required = self.config.volatility_scalp_dip_entry_percent * (
            Decimal("1") + self.config.volatility_scalp_averaging_step_multiplier * level
        )
        return drop >= required

    def averaging_down_capacity(
        self,
        per_buy_risk_dollars: Decimal,
        buying_power: Decimal,
        max_symbol_risk_fraction: Decimal,
        max_averaging_buys: int,
    ) -> int:
        """Bounds how many ADDITIONAL averaging-down buys a single
        symbol can take, on top of its already-configured ceiling
        (max_averaging_buys), so total worst-case exposure to one
        symbol - even fully averaged down and hitting the hard-stop
        floor - can't exceed max_symbol_risk_fraction of buying_power.

        Research finding acted on directly: "doubling down three times
        can turn a 7% position into an 18% loss... in a bad market that
        50% can be 80%." Each volatility-scalp buy (fresh entry and
        every averaging-down add) targets roughly the same per-trade
        notional (see volatility_scalp_share_count), so per_buy_risk_
        dollars (that one buy's notional times the hard-stop-floor
        percent) approximates every subsequent buy's incremental risk
        too - total risk after N total buys is roughly
        N * per_buy_risk_dollars.

        Returns 0 (no more averaging at all) if even the second buy
        (the first averaging-down add) would already breach the
        fraction. This is a CAP on top of max_averaging_buys, not a
        replacement - whichever is smaller wins; a small account's real
        exposure limit may bind well before the configured "5" ever
        would, and that's the point.
        """
        if per_buy_risk_dollars <= 0 or buying_power <= 0:
            return max_averaging_buys
        max_symbol_risk_dollars = buying_power * max_symbol_risk_fraction
        total_buys_affordable = int(max_symbol_risk_dollars / per_buy_risk_dollars)
        # -1 for the initial fresh entry itself, which has already
        # happened and isn't part of "additional averaging capacity."
        capacity = max(0, total_buys_affordable - 1)
        return min(capacity, max_averaging_buys)

    def volatility_scalp_share_count(
        self,
        price: Decimal,
        buying_power: Decimal | None = None,
        intensity: Decimal = Decimal("1"),
    ) -> int:
        """Dollar-notional-target sizing for the volatility-scalp
        strategy, by request: don't cap every penny stock at a flat 100
        shares or every $1+ stock at a flat small count - size UP toward
        a target notional instead, rounded to a clean lot.

        The target itself is min(VOLATILITY_SCALP_TARGET_NOTIONAL, a
        fraction of the caller's own buying_power) when buying_power is
        passed - a flat dollar target alone doesn't scale with account
        size (live sanity check caught this: on a small account, a
        large flat target gets silently zeroed by the caller's
        affordability check on nearly every attempt, meaning close to
        ZERO trades instead of "high frequency" - see trade_stocks).
        buying_power=None (the caller doesn't have it handy) just uses
        the flat target as-is.

        intensity (0-1, default 1 = full size) further scales the
        target down. AutoTrader.trade_stocks always passes 1 now -
        volatility-scalp entries never fire outside core hours at all
        (by request, after pre-market losses), so there's no dampened-
        intensity case left to apply. The parameter itself stays, since
        the sub-$1 rounding-up behavior below is keyed off it too.

        Under $1, always rounds UP to at least 100 shares regardless of
        how small the target (or intensity) computes to - Webull's own
        lot-restricted-band minimum there (see minimum_lot_size) leaves
        no smaller valid order to fall back to, so intensity dampening
        has no effect on sub-$1 trade size specifically - the caller's
        own affordability/exposure checks are the real backstop on this
        floor, not this function. At $1 and up (no exchange-mandated
        minimum), rounds to the nearest 10 shares when the target
        affords at least one full 10-share lot - "in the tens, if not
        the hundreds" - but degrades to whatever whole-share quantity
        the target actually affords (down to 1) rather than forcing a
        10-share lot a small target can't comfortably support, or
        skipping an otherwise fine smaller trade purely over lot-
        rounding.

        0 (skip) for anything priced at or below zero, above
        VOLATILITY_SCALP_MAX_PRICE, or too small to afford even one
        share of a $1+ stock.
        """
        if price <= 0 or price > self.config.volatility_scalp_max_price:
            return 0
        target_notional = self.config.volatility_scalp_target_notional
        if buying_power is not None and buying_power > 0:
            target_notional = min(
                target_notional,
                buying_power
                * self.config.volatility_scalp_target_notional_buying_power_fraction,
            )
        target_notional *= max(Decimal("0"), min(Decimal("1"), intensity))
        raw_quantity = int(
            (target_notional / price).to_integral_value(rounding=ROUND_DOWN)
        )
        if price < Decimal("1"):
            return max(100, (raw_quantity // 100) * 100)
        if raw_quantity >= 10:
            return (raw_quantity // 10) * 10
        return max(0, raw_quantity)

    def volatility_scalp_target_price(self, average_cost: Decimal) -> Decimal:
        """Sell the rip: a small, fixed quick-profit target above cost -
        deliberately not the normal adaptive-stop-scaled target
        (stock_decision's base_target), since the whole point of this
        path is cycling capital fast on a volatile symbol's own natural
        back-and-forth rather than waiting for one bigger move.
        """
        return average_cost * (
            Decimal("1") + self.config.volatility_scalp_target_percent
        )

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

    def priority_score(self, symbol: str, assessment: dict | None) -> float:
        score = self.activity.get(symbol, 0.0)
        # Reward symbols with a recurring back-and-forth pattern today (a
        # capped count of EMA direction flips) so the scanner keeps favoring
        # stocks that repeatedly create fresh scalp setups over ones that
        # already made their one move for the day.
        oscillation = min(20, self.crossover_counts.get(symbol, 0))
        score += oscillation * float(self.config.stock_oscillation_weight)
        if symbol in self.most_active_symbols:
            score += float(self.config.most_active_priority_bonus)
        score += self.analyst_priority.get(symbol, 0.0)
        if not assessment:
            return score
        confidence = float(assessment.get("confidence", 0))
        research_priority = float(assessment.get("priority", 0))
        spread_opportunity = float(
            assessment.get("spread_opportunity", 0)
        )
        quick_trade_score = float(assessment.get("quick_trade_score", 0))
        symbol_volatility = float(assessment.get("symbol_volatility", 0))
        expected_move = min(
            1.0,
            abs(float(assessment.get("expected_move_percent", 0))) / 5.0,
        )
        catalyst = abs(float(assessment.get("catalyst_strength", 0)))
        volatility = float(assessment.get("market_volatility", 0))
        return score + 2.0 * confidence * (
            research_priority
            + 1.5 * quick_trade_score
            + 1.25 * symbol_volatility
            + spread_opportunity
            + expected_move
            + catalyst
            + volatility
        )

    @staticmethod
    def analyst_priority_bonus(
        price: Decimal,
        target_mean: Decimal | None,
        rating: dict | None,
        bonus_max: Decimal,
    ) -> Decimal:
        """A soft, two-sided priority_score nudge (see analyst_priority) -
        never blocks or forces anything, just re-ranks candidates already
        eligible on every other gate. Combines two independent -1..1
        signals, averaged:

        - Upside: how far below the analyst mean target the current price
          sits, clipped to +-50% so one stale or outlier target can't
          dominate, then rescaled so a 25%+ gap already reads as "fully"
          bullish on this axis (a bigger gap adds no further weight).
        - Rating lean: (bullish - bearish) analyst counts as a fraction of
          total coverage.

        Neutral (0) with no coverage on either signal - this must never
        become a de facto exclusion filter for the many penny/micro-cap
        names this bot trades that analysts simply don't cover.
        """
        if price <= 0:
            return Decimal("0")
        rating = rating or {}
        strong_buy = int(rating.get("strong_buy", 0))
        buy = int(rating.get("buy", 0))
        hold = int(rating.get("hold", 0))
        sell = int(rating.get("sell", 0))
        under_perform = int(rating.get("under_perform", 0))
        total = strong_buy + buy + hold + sell + under_perform
        rating_lean = (
            Decimal(strong_buy + buy - sell - under_perform) / Decimal(total)
            if total > 0
            else Decimal("0")
        )
        if target_mean and target_mean > 0:
            upside = (target_mean - price) / price
            upside = max(Decimal("-0.5"), min(Decimal("0.5"), upside))
            upside_lean = max(
                Decimal("-1"), min(Decimal("1"), upside / Decimal("0.25"))
            )
        else:
            upside_lean = Decimal("0")
        return (rating_lean + upside_lean) / Decimal("2") * bonus_max

    def stock_scan_concurrent_batches(
        self, total_symbols: int, core_session_active: bool
    ) -> int:
        """How many STOCK_BATCH_SIZE-sized quote batches trade_stocks
        should fetch CONCURRENTLY this cycle - by request: "scan
        through all [the universe]... as many as needed to scan
        everything and filter it down, then dynamically less as it is
        filtered down... does not need to be as intense in extended
        hours."

        Scales with the current universe size: enough concurrent
        batches to cover the WHOLE universe once within roughly
        stock_scan_target_full_coverage_cycles cycles. As the universe
        shrinks (or simply stops growing once the daily download
        finishes), fewer batches are needed to hit the same coverage
        target - this is what "dynamically less as it is filtered
        down" means in terms of this function's only input, since
        prioritized_stock_batch's own activity-based ranking (not this
        function) is what actually concentrates real candidates within
        each batch.

        Halved (rounded down, floor 1) outside core hours via
        stock_scan_extended_hours_concurrency_fraction - by request,
        matches the existing "no volatility scalp in extended hours...
        only established/popular symbols" philosophy of scanning less
        aggressively when there's less real opportunity to act on it.

        Bounded by stock_scan_max_concurrent_batches regardless of
        universe size - a hard safety cap on real Webull API request
        volume, given it already returned live 429 TOO_MANY_REQUESTS
        errors this session.
        """
        if total_symbols <= 0 or self.config.stock_batch_size <= 0:
            return 1
        cycles = max(1, self.config.stock_scan_target_full_coverage_cycles)
        needed = -(-total_symbols // (self.config.stock_batch_size * cycles))
        needed = max(1, needed)
        if not core_session_active:
            needed = max(
                1,
                int(
                    needed
                    * self.config.stock_scan_extended_hours_concurrency_fraction
                ),
            )
        return min(needed, self.config.stock_scan_max_concurrent_batches)

    def prioritized_stock_batch(
        self,
        symbols: list[str],
        cursor: int,
        positions: list[dict],
        assessment_for,
        research_symbols: set[str] | None = None,
    ) -> tuple[list[str], int]:
        if not symbols:
            return [], 0
        size = min(self.config.stock_batch_size, len(symbols))
        available = set(symbols)
        research_symbols = (research_symbols or set()) & available
        held = [
            str(item.get("symbol", "")).upper()
            for item in positions
            if item.get("instrument_type") == "EQUITY"
            and Decimal(str(item.get("quantity", "0"))) != 0
            and str(item.get("symbol", "")).upper() in available
        ]
        ranked = sorted(
            (symbol for symbol in self.activity if symbol in available),
            key=lambda symbol: self.priority_score(
                symbol,
                assessment_for(symbol),
            ),
            reverse=True,
        )
        penny = [
            symbol
            for symbol in ranked
            if self.prices.get(symbol, Decimal("Infinity"))
            < self.config.penny_stock_max_price
        ]
        liquid_popular = [
            symbol
            for symbol in ranked
            if self.prices.get(symbol, Decimal("0"))
            >= self.config.penny_stock_max_price
            and self.metrics.get(symbol, {}).get("volume", 0)
            >= self.config.popular_stock_min_volume
            and Decimal(
                str(self.metrics.get(symbol, {}).get("spread_percent", "999"))
            )
            <= self.config.popular_stock_max_spread_percent
        ]
        researched = sorted(
            research_symbols,
            key=lambda symbol: self.priority_score(
                symbol,
                assessment_for(symbol),
            ),
            reverse=True,
        )
        popular = list(dict.fromkeys(researched + liquid_popular))
        penny_count = int(size * self.config.stock_penny_fraction)
        popular_count = int(size * self.config.stock_priority_fraction)
        popular_selected = popular[:popular_count]
        penny_selected = penny[:penny_count]
        # Reserve a guaranteed slice of every batch for fresh exploration so
        # the scanner keeps paging the whole universe instead of re-scanning
        # the same top-ranked names each cycle.
        explore_floor = max(1, size - popular_count - penny_count)
        priority = list(dict.fromkeys(held + popular_selected + penny_selected))
        priority = priority[: size - explore_floor]
        # Request exactly the open exploration slots, skipping symbols already
        # in the priority slice so exploration always keeps paging forward
        # through fresh names instead of re-picking (and then discarding)
        # names the priority slice already covered.
        exploration_slots = max(0, size - len(priority))
        priority_set = set(priority)
        exploration: list[str] = []
        attempts = 0
        while len(exploration) < exploration_slots and attempts < len(symbols):
            probe, cursor = self.rotating_batch(symbols, cursor, 1)
            attempts += 1
            if probe and probe[0] not in priority_set:
                exploration.append(probe[0])
        selected = list(dict.fromkeys(priority + exploration))
        selected = selected[:size]
        self.selection_buckets = {}
        held_set = set(held)
        popular_set = set(popular_selected)
        penny_set = set(penny_selected)
        for symbol in selected:
            if symbol in held_set:
                self.selection_buckets[symbol] = "HELD"
            elif symbol in popular_set:
                self.selection_buckets[symbol] = "POPULAR"
            elif symbol in penny_set:
                self.selection_buckets[symbol] = "PENNY"
            else:
                self.selection_buckets[symbol] = "DISCOVERY"
        return selected, cursor

    def selection_bucket(self, symbol: str) -> str:
        return self.selection_buckets.get(symbol, "DISCOVERY")

    def research_candidates(
        self,
        limit: int,
        excluded: set[str],
        assessment_for,
        blocked_until,
    ) -> list[dict]:
        ranked = sorted(
            self.activity,
            key=lambda symbol: self.priority_score(
                symbol,
                assessment_for(symbol),
            ),
            reverse=True,
        )
        results = []
        for symbol in ranked:
            if symbol in excluded or blocked_until(symbol):
                continue
            price = self.prices.get(symbol)
            if not price:
                continue
            results.append(
                {
                    "symbol": symbol,
                    "type": (
                        "PENNY"
                        if price < self.config.penny_stock_max_price
                        else "POPULAR_VOLATILE"
                    ),
                    "price": str(price),
                    **self.metrics.get(symbol, {}),
                }
            )
            if len(results) >= limit:
                break
        return results

    @staticmethod
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
        cross. Deliberately skips trend_signal's reenter_on_trend/streak
        re-entry logic - theta already punishes waiting on an option, so
        this fires once per fresh cross and goes quiet, rather than
        re-firing on a continued trend.
        """
        values = self.history[key]
        values.append(float(price))
        self.tick_history[key].append(float(price))
        slow = self.config.ema_slow_period
        fast = self.config.ema_fast_period
        if len(values) < slow + 1:
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
        if new_spread > 0 and old_spread <= 0:
            return "CALL"
        if new_spread < 0 and old_spread >= 0:
            return "PUT"
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

    @staticmethod
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

    @staticmethod
    def option_delta_ok(delta: Decimal | None) -> bool:
        """Quality filter, not strike selection: rejects a contract that's
        too far OTM to have real directional exposure (lottery-ticket cheap,
        decays fast) or so deep ITM it's paying for intrinsic value with no
        leverage left. `None` (delta unavailable on this account's snapshot)
        passes through untouched, same as every other best-effort gate.
        """
        return delta is None or OPTION_DELTA_MIN <= abs(delta) <= OPTION_DELTA_MAX

    @staticmethod
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

    @staticmethod
    def option_iv_percentile_ok(
        iv_history,
        current_iv: Decimal | None,
    ) -> bool:
        """Rejects an entry when current_iv sits in the priciest tail of
        this SAME contract's own recent IV samples - no external IV-rank
        source exists here. Passes through when IV data or enough history
        isn't available yet.
        """
        return TradingStrategy._percentile_reject_ok(
            iv_history,
            current_iv,
            OPTION_IV_PERCENTILE_MIN_SAMPLES,
            OPTION_IV_REJECT_PERCENTILE,
        )

    @staticmethod
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
        return TradingStrategy._percentile_reject_ok(
            vixy_history,
            current_vixy,
            OPTION_IV_PERCENTILE_MIN_SAMPLES,
            OPTION_VIXY_REJECT_PERCENTILE,
        )

    @staticmethod
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
        return TradingStrategy._percentile_reject_ok(
            vixy_history,
            current_vixy,
            OPTION_IV_PERCENTILE_MIN_SAMPLES,
            reject_percentile,
        )

    def adaptive_stop_percent(
        self, symbol: str, seconds_since_entry: float | None = None
    ) -> Decimal:
        range_ratio = Decimal(str(self.metrics.get(symbol, {}).get("range_ratio", 0)))
        scaled = range_ratio * self.config.stock_stop_loss_range_multiplier
        percent = max(
            self.config.stock_stop_loss_min_percent,
            min(self.config.stock_stop_loss_max_percent, scaled),
        )
        if (
            self.config.time_aware_stop_enabled
            and seconds_since_entry is not None
            and seconds_since_entry < self.config.time_aware_stop_widen_seconds
        ):
            # Deliberately widens past the normal max - quote noise right
            # at fill shouldn't stop a position out before the strategy's
            # real edge has had a chance to play out. Tightens back to the
            # normal adaptive value once TIME_AWARE_STOP_WIDEN_SECONDS
            # elapses.
            percent *= self.config.time_aware_stop_widen_multiplier
        return percent

    def stock_decision(
        self,
        key: str,
        price: Decimal,
        quantity: int,
        average_cost: Decimal,
        assessment: dict | None = None,
        opening_grace_active: bool = False,
        idle_relaxation_multiplier: Decimal = Decimal("1"),
        idle_relaxation_amount: Decimal = Decimal("0"),
        seconds_since_entry: float | None = None,
    ) -> Decision:
        trend = self.trend_signal(key, price)
        symbol = key.split(":", 1)[-1]
        if quantity > 0:
            # The flat per-sell fee doesn't scale with share count, so it
            # has to be converted to a per-share amount before it can be
            # added to a per-share target/breakeven price - otherwise a
            # target hit at exactly the percentage-based price would still
            # net a loss once the flat fee comes out of the actual fill.
            fee_per_share = self.config.sell_fee_dollars / quantity
            stop_percent = self.adaptive_stop_percent(symbol, seconds_since_entry)
            # A fractional (core-session dollar-sized) position targets a
            # much smaller move than a whole-share one - it can only be
            # exited during core hours at all (see is_fractional_quantity
            # usage in bot.py), so it should cycle capital quickly within
            # that window (many trades/hour) rather than sit waiting for
            # the same larger move a whole-share position can afford to
            # hold toward across a longer stretch of the day.
            # quantity isn't guaranteed to already be a Decimal here (a
            # plain int is a valid whole-share quantity too) - only
            # Decimal has to_integral_value().
            quantity_decimal = (
                quantity if isinstance(quantity, Decimal) else Decimal(quantity)
            )
            is_fractional = quantity_decimal != quantity_decimal.to_integral_value()
            floor_percent = (
                self.config.stock_min_net_profit_percent
                + self.config.stock_estimated_round_trip_cost_percent
            )
            # A tiny fractional position's fee-per-share (the flat
            # SELL_FEE_DOLLARS spread over a quantity well under 1) is
            # already a meaningfully larger relative cost than a
            # whole-share position's - scaling the target off the
            # adaptive stop on top of that (like whole-share does below)
            # ends up demanding much more absolute price appreciation
            # than "capture the profit quickly" intends. A fractional
            # position's target is just the flat cost-recovery floor -
            # fires on any solidly fee-covered gain - with no additional
            # stop-scaled requirement layered on top of it.
            target_percent = (
                floor_percent
                if is_fractional
                else max(floor_percent, stop_percent * self.config.stock_target_stop_multiple)
            )
            base_target = average_cost * (Decimal("1") + target_percent) + fee_per_share
            stop = average_cost * (Decimal("1") - stop_percent)
            if average_cost > 0 and price <= stop:
                return Decision("LOSS", "percentage stop reached", price)
            if average_cost > 0 and price > 0:
                # Checked here, not before the stop check above: a stop-
                # loss must still fire even when average_cost has drifted
                # a lot from the live price (a real, if large, move over
                # several days is a legitimate reason for a stop to
                # trigger - live incident: AZI, down ~30% since entry, sat
                # completely unprotected because this used to gate the
                # stop check too, not just the target below). Only
                # deriving a PROFIT target from a suspect average_cost is
                # the actual risk (an unreachable target that just churns
                # failed orders forever - see stock_price_sanity_percent).
                divergence = abs(average_cost - price) / price
                if divergence > self.config.stock_price_sanity_percent:
                    return Decision(
                        "HOLD",
                        "cost basis diverges implausibly from live price",
                        price,
                    )
            bias = self._exit_bias(assessment)
            target = base_target
            if (
                self.config.agent_exit_influence_enabled
                and average_cost > 0
                and bias >= self.config.agent_runner_bias_threshold
            ):
                target = max(
                    base_target,
                    average_cost * (Decimal("1") + self.config.agent_runner_profit_percent)
                    + fee_per_share,
                )
            if average_cost > 0 and price >= target:
                reason = (
                    "agent runner target reached"
                    if target > base_target
                    else "percentage profit reached"
                )
                return Decision("PROFIT", reason, target)
            if (
                self.config.agent_exit_influence_enabled
                and average_cost > 0
                and bias <= self.config.agent_derisk_bias_threshold
            ):
                breakeven = average_cost * (
                    Decimal("1")
                    + self.config.stock_estimated_round_trip_cost_percent
                ) + fee_per_share
                if price >= breakeven:
                    return Decision(
                        "PROFIT",
                        "agent de-risk lock-in on fading catalyst",
                        breakeven,
                    )
            return Decision("HOLD", "position between target and stop", target)
        if quantity < 0:
            # Mirror image of the long-side math above: a short's
            # average_cost is the price it was sold short at, so it
            # profits as price falls and loses as price rises - target/
            # stop are below/above cost respectively, the opposite of a
            # long position's.
            short_quantity = -quantity
            fee_per_share = self.config.sell_fee_dollars / short_quantity
            stop_percent = self.adaptive_stop_percent(symbol, seconds_since_entry)
            target_percent = max(
                self.config.stock_min_net_profit_percent
                + self.config.stock_estimated_round_trip_cost_percent,
                stop_percent * self.config.stock_target_stop_multiple,
            )
            base_target = average_cost * (Decimal("1") - target_percent) - fee_per_share
            stop = average_cost * (Decimal("1") + stop_percent)
            if average_cost > 0 and price >= stop:
                return Decision("LOSS", "percentage stop reached (short)", price)
            if average_cost > 0 and price > 0:
                # See the mirrored long-side comment above - a stop must
                # still fire on a real, large move; only the profit target
                # below needs protecting from a suspect average_cost.
                divergence = abs(average_cost - price) / price
                if divergence > self.config.stock_price_sanity_percent:
                    return Decision(
                        "HOLD",
                        "cost basis diverges implausibly from live price",
                        price,
                    )
            bias = self._exit_bias(assessment)
            target = base_target
            if (
                self.config.agent_exit_influence_enabled
                and average_cost > 0
                and bias <= self.config.agent_derisk_bias_threshold
            ):
                # A negative exit_bias means "de-risk/exit now" for a long;
                # for a short that same bearish-catalyst-fading signal is
                # the runner case - a strong bearish thesis still playing
                # out supports holding for a larger move down.
                target = min(
                    base_target,
                    average_cost * (Decimal("1") - self.config.agent_runner_profit_percent)
                    - fee_per_share,
                )
            if average_cost > 0 and price <= target:
                reason = (
                    "agent runner target reached (short)"
                    if target < base_target
                    else "percentage profit reached (short)"
                )
                return Decision("PROFIT", reason, target)
            if (
                self.config.agent_exit_influence_enabled
                and average_cost > 0
                and bias >= self.config.agent_runner_bias_threshold
            ):
                # A positive exit_bias (bullish catalyst) is the de-risk
                # signal for a short - lock in whatever's there once past
                # breakeven rather than let a reversal erase it.
                breakeven = average_cost * (
                    Decimal("1")
                    - self.config.stock_estimated_round_trip_cost_percent
                ) - fee_per_share
                if price <= breakeven:
                    return Decision(
                        "PROFIT",
                        "agent de-risk lock-in on fading short catalyst",
                        breakeven,
                    )
            return Decision("HOLD", "short position between target and stop", target)
        if not self.entry_spread_ok(
            key, opening_grace_active, idle_relaxation_multiplier
        ):
            return Decision("HOLD", "spread too wide to scalp profitably")
        if trend == "SHORT" and self.config.short_selling_enabled:
            if not self.vwap_supports_entry(
                symbol, price, "SHORT", idle_relaxation_multiplier
            ):
                return Decision("HOLD", "price above session VWAP")
            if not self.entry_extension_ok(
                symbol, price, opening_grace_active, "SHORT", idle_relaxation_multiplier
            ):
                return Decision(
                    "HOLD", "price already extended near today's low"
                )
            if not self.sma_trend_supports_entry(symbol, price, "SHORT"):
                return Decision(
                    "HOLD", "price above the higher-timeframe SMA trend"
                )
            if self.tick_direction_ok(key, "SHORT", idle_relaxation_amount):
                return Decision("SHORT", "EMA bearish entry confirmed")
            return Decision(
                "HOLD", "recent ticks trending against the short entry"
            )
        if not self.vwap_supports_entry(
            symbol, price, "BUY", idle_relaxation_multiplier
        ):
            return Decision("HOLD", "price below session VWAP")
        if not self.entry_extension_ok(
            symbol, price, opening_grace_active, "BUY", idle_relaxation_multiplier
        ):
            return Decision("HOLD", "price already extended near today's high")
        if not self.sma_trend_supports_entry(symbol, price):
            return Decision("HOLD", "price below the higher-timeframe SMA trend")
        if trend == "BUY":
            if self.tick_direction_ok(key, "BUY", idle_relaxation_amount):
                return Decision("BUY", "EMA entry confirmed")
            return Decision(
                "HOLD", "recent ticks trending against the EMA entry"
            )
        if self.research_supports_entry(assessment):
            return Decision(
                "BUY",
                "strong liquid short-horizon research setup",
            )
        return Decision("HOLD", "EMA entry not ready")

    def _exit_bias(self, assessment: dict | None) -> Decimal:
        if not assessment:
            return Decimal("0")
        try:
            confidence = Decimal(str(assessment.get("confidence", 0)))
            bias = Decimal(str(assessment.get("exit_bias", 0)))
        except Exception:
            return Decimal("0")
        if confidence < self.config.agent_exit_min_confidence:
            return Decimal("0")
        return max(Decimal("-1"), min(Decimal("1"), bias))

    def entry_spread_ok(
        self,
        key: str,
        opening_grace_active: bool = False,
        idle_relaxation_multiplier: Decimal = Decimal("1"),
    ) -> bool:
        symbol = key.split(":", 1)[-1]
        spread = self.metrics.get(symbol, {}).get("spread_percent")
        if spread in (None, ""):
            return True
        threshold = self.config.stock_entry_max_spread_percent
        multiplier = idle_relaxation_multiplier
        if opening_grace_active:
            multiplier = max(multiplier, self.config.opening_grace_spread_multiplier)
        threshold *= multiplier
        try:
            return Decimal(str(spread)) <= threshold
        except Exception:
            return True

    def volatility_scalp_entry_spread_ok(self, symbol: str) -> bool:
        """By request: "make sure the algo plays around in the spread
        while ensuring a profit, or a profitable entry." Entries had NO
        spread-quality check at all - only exits did (_stall_exit_price's
        VOLATILITY_SCALP_MAX_EXIT_SPREAD_PERCENT bound). Buying into an
        absurdly wide spread sets up a losing trade before it even
        starts: the fill happens near the ask/mid, but the very next
        exit still has to clear the SAME wide spread to reach a real
        bid-side profit. Reuses the exit side's own bound for symmetry -
        this cohort's naturally wide (but legitimate, not glitchy)
        spreads should be tradable on both sides equally. True (don't
        block) when spread data isn't available yet, same "no data ->
        don't block" convention as every other entry gate.
        """
        spread = self.metrics.get(symbol, {}).get("spread_percent")
        if spread in (None, ""):
            return True
        try:
            return (
                Decimal(str(spread))
                <= self.config.volatility_scalp_max_exit_spread_percent
            )
        except Exception:
            return True

    def entry_extension_ok(
        self,
        symbol: str,
        price: Decimal,
        opening_grace_active: bool = False,
        direction: str = "BUY",
        idle_relaxation_multiplier: Decimal = Decimal("1"),
    ) -> bool:
        """Block chasing a name that's already sitting at today's high (or,
        for a short, today's low).

        A crossover that only confirms once price is already at the peak
        of a fast spike is buying the top, not the move - require some
        room below today's high before allowing a fresh entry (mirrored:
        a short needs room above today's low, not already chasing the
        bottom). Right after the open, today's high/low is barely
        established yet and gets set/reset constantly, so the grace window
        shrinks the room required (smaller buffer = more lenient) instead
        of dropping the check entirely. idle_relaxation_multiplier does the
        same shrink for the opposite reason - cash sitting idle too long,
        not the opening print.
        """
        reference = self.metrics.get(symbol, {}).get(
            "low" if direction == "SHORT" else "high"
        )
        if not reference:
            return True
        try:
            reference_decimal = Decimal(str(reference))
        except Exception:
            return True
        if reference_decimal <= 0:
            return True
        extension_percent = self.config.stock_entry_max_extension_percent
        multiplier = idle_relaxation_multiplier
        if opening_grace_active:
            multiplier = max(multiplier, self.config.opening_grace_extension_multiplier)
        extension_percent /= multiplier
        if direction == "SHORT":
            return price >= reference_decimal * (Decimal("1") + extension_percent)
        return price <= reference_decimal * (Decimal("1") - extension_percent)

    @staticmethod
    def research_supports_entry(assessment: dict | None) -> bool:
        if not assessment:
            return False
        return (
            float(assessment.get("confidence", 0)) >= 0.70
            and float(assessment.get("quick_trade_score", 0)) >= 0.70
            and float(assessment.get("symbol_volatility", 0)) >= 0.60
            and float(assessment.get("expected_move_percent", 0)) > 0
            and float(assessment.get("catalyst_strength", 0)) > 0
            and float(assessment.get("liquidity_risk", 1)) <= 0.40
            and float(assessment.get("downside_risk", 1)) <= 0.50
            and int(assessment.get("horizon_minutes", 390)) <= 30
        )

    def option_decision(
        self,
        price: Decimal,
        quantity: int,
        average_cost: Decimal,
        days_to_expiration: int,
    ) -> Decision:
        """Exit-only: entries are now decided externally by
        option_direction_signal/option_entry_confirmed (bot.py calls those
        before ever opening a position), since a call needs a bullish
        underlying and a put needs a bearish one - a single BUY/HOLD signal
        on the option's own premium can't express that distinction.
        """
        if quantity <= 0:
            return Decision("HOLD", "no position")
        # Same flat-fee-to-per-share conversion as stock_decision, but one
        # contract represents 100 shares, so the fee is spread over
        # quantity * 100, not quantity alone.
        fee_per_share = self.config.sell_fee_dollars / (quantity * 100)
        if average_cost > 0 and days_to_expiration <= self.config.option_min_hold_dte:
            # Forced exit regardless of target/stop - theta/gamma accelerate
            # sharply in the final days before expiration, and holding
            # through that isn't a directional bet anymore, it's a coin
            # flip on pin risk.
            reason = f"time decay exit - {days_to_expiration}d to expiration"
            if price > average_cost:
                return Decision("PROFIT", reason, price)
            return Decision("LOSS", reason, price)
        target = average_cost * (
            Decimal("1") + self.config.option_take_profit_percent
        ) + fee_per_share
        stop = average_cost * (
            Decimal("1") - self.config.option_stop_loss_percent
        )
        if average_cost > 0 and price <= stop:
            return Decision("LOSS", "option percentage stop reached", price)
        if average_cost > 0 and price >= target:
            return Decision("PROFIT", "option profit target reached", target)
        return Decision("HOLD", "option waiting for profit", target)

    @staticmethod
    def minimum_lot_size(price: Decimal) -> int:
        """Webull rejects any order under 100 shares outright for stocks
        priced $0.10-$0.999 (OAUTH_OPENAPI_CANT_TRADE_FOR_PRICE_BETWEEN_
        0099_AND_0999) - a plain per-order STOCK_QUANTITY of 1 would fail
        every single time in that band, not just occasionally.
        """
        if Decimal("0.10") <= price <= Decimal("0.999"):
            return 100
        return 1

    @classmethod
    def exit_blocked_by_lot_restriction(cls, quantity: Decimal, price: Decimal) -> bool:
        """True only when price sits in the $0.10-$0.999 band AND quantity
        can't clear that band's 100-share minimum - NOT whenever quantity
        is merely less than minimum_lot_size's return value.

        minimum_lot_size returns 1 (a no-op floor) for every price outside
        that band, so a bare `quantity < minimum_lot_size(price)` comparison
        - three separate call sites in bot.py used to write it exactly that
        way - reads as "true for practically every fractional position at
        a normal price", since a fractional quantity is by definition under
        1. That silently blocked every fractional position's PROFIT/LOSS/
        stall-breaker exit at a normal price, indefinitely: not a rare
        edge case, the common one, since fractional entries are dollar-
        sized slices of ordinarily-priced stocks, not usually penny stocks.
        """
        min_lot = cls.minimum_lot_size(price)
        return min_lot > 1 and quantity < min_lot

    def risk_based_share_count(
        self,
        price: Decimal,
        stop_price: Decimal,
        buying_power: Decimal,
        risk_fraction: Decimal,
    ) -> int:
        """The professional 1-2% position-sizing rule: size an entry so
        that hitting the stop costs no more than risk_fraction of
        buying_power, not however many shares a fixed dollar budget
        happens to afford. `risk_dollars = buying_power * risk_fraction`,
        `shares = risk_dollars / abs(price - stop_price)`, floored to a
        whole share.

        By design, this is a CAP layered on top of the existing
        affordability/max_order_notional caps (see stock_order_quantity),
        not a replacement for them - a caller takes the min() of this
        and its other sizing result. stock_risk_per_trade_fraction is
        set deliberately above the professional 1-2% standard for this
        account's size: a very small account's fixed per-trade costs
        (spread, fees) make 1% barely actionable, so a somewhat larger
        fraction is a documented, deliberate tradeoff, not a hidden
        compromise.

        Returns 0 if the stop distance is zero/invalid (can't size
        against an undefined risk) - same "no data -> don't trade"
        convention as every other gate in this file.
        """
        stop_distance = abs(price - stop_price)
        if stop_distance <= 0 or buying_power <= 0:
            return 0
        risk_dollars = buying_power * risk_fraction
        return int((risk_dollars / stop_distance).to_integral_value(rounding=ROUND_DOWN))

    def stock_order_quantity(
        self,
        price: Decimal,
        buying_power: Decimal,
    ) -> tuple[int, Decimal]:
        buffered_price = price * Decimal("1.03")
        affordable = int(
            (buying_power / buffered_price).to_integral_value(
                rounding=ROUND_DOWN
            )
        )
        notional_limit = int(
            (self.config.max_order_notional / price).to_integral_value(
                rounding=ROUND_DOWN
            )
        )
        quantity = min(self.config.stock_quantity, affordable, notional_limit)
        min_lot = self.minimum_lot_size(price)
        if quantity < min_lot:
            quantity = (
                min_lot
                if min_lot <= affordable and min_lot <= notional_limit
                else 0
            )
        return quantity, buffered_price

    def fractional_stock_quantity(
        self,
        price: Decimal,
        buying_power: Decimal,
    ) -> Decimal:
        """Fallback sizing for when buying_power can't afford even one whole
        share: Webull's fractional orders are quantity-capped to (0, 1] and
        must clear a minimum order value, so this returns Decimal("0") -
        meaning "skip, don't place a fractional order" - whenever either
        constraint can't be met, rather than rounding into an invalid order.

        A stock priced $0.10-$0.999 needs a 100-share minimum order size
        (see minimum_lot_size) that no fractional order (always <= 1 share)
        can ever satisfy, so fractional sizing is skipped entirely there.
        """
        if price <= 0 or self.minimum_lot_size(price) > 1:
            return Decimal("0")
        min_notional = self.config.fractional_shares_min_notional
        affordable_notional = min(buying_power, price)
        if affordable_notional < min_notional:
            return Decimal("0")
        quantity = (affordable_notional / price).quantize(
            Decimal("0.0001"),
            rounding=ROUND_DOWN,
        )
        quantity = min(quantity, Decimal("1"))
        if quantity <= 0 or quantity * price < min_notional:
            return Decimal("0")
        return quantity

    def dollar_stock_quantity(
        self,
        price: Decimal,
        target_notional: Decimal,
    ) -> tuple[Decimal, Decimal]:
        """Core-session entry sizing: convert a dollar budget directly into a
        decimal share quantity for a fractional MARKET order, instead of
        picking a share count first and checking whether it's affordable.
        Unlike fractional_stock_quantity, this is not capped at one share -
        Webull's QTY-type fractional orders accept decimal quantities above 1
        (only its separate, unused AMOUNT order type is capped under one
        share's price). Skips the $0.10-$0.999 lot-restricted band entirely
        (see minimum_lot_size) since Webull requires a 100-share lot there
        that no decimal-quantity order can satisfy.
        """
        buffered_price = price * Decimal("1.03")
        if price <= 0 or self.minimum_lot_size(price) > 1:
            return Decimal("0"), buffered_price
        if target_notional < self.config.fractional_shares_min_notional:
            return Decimal("0"), buffered_price
        quantity = (target_notional / buffered_price).quantize(
            Decimal("0.0001"), rounding=ROUND_DOWN
        )
        if quantity <= 0:
            return Decimal("0"), buffered_price
        return quantity, buffered_price

    def option_order_quantity(
        self,
        limit_price: Decimal,
        buying_power: Decimal,
    ) -> tuple[int, Decimal]:
        contract_cost = limit_price * 100
        affordable = int(
            (buying_power / contract_cost).to_integral_value(
                rounding=ROUND_DOWN
            )
        )
        notional_limit = int(
            (
                self.config.max_order_notional / contract_cost
            ).to_integral_value(rounding=ROUND_DOWN)
        )
        # Never risk more than this fraction of buying power on one entry -
        # a defined-risk-per-trade cap on top of (not instead of) the
        # option_quantity/max_order_notional caps above.
        risk_cap = int(
            (
                buying_power * self.config.option_capital_fraction / contract_cost
            ).to_integral_value(rounding=ROUND_DOWN)
        )
        return (
            min(self.config.option_quantity, affordable, notional_limit, risk_cap),
            contract_cost,
        )

    @staticmethod
    def open_position_count(positions: list[dict]) -> int:
        return sum(
            1
            for item in positions
            if Decimal(str(item.get("quantity", "0"))) != 0
        )

    def position_unrealized_pnl(self, position: dict) -> Decimal:
        """Net of the flat sell fee this position hasn't paid yet - it's
        still open, but closing it will cost that fee, so showing the raw
        pre-fee mark-to-market number would overstate what selling right
        now actually nets.

        Since cost, not since today - a position held across several
        sessions accumulates this the whole time it's open. See
        position_day_pnl for the today-only figure the dashboard's "P&L
        Today" panel actually wants.
        """
        reported = position.get("unrealized_profit_loss")
        if reported not in (None, ""):
            try:
                return Decimal(str(reported)) - self.config.sell_fee_dollars
            except Exception:
                pass
        try:
            quantity = Decimal(str(position.get("quantity", "0")))
            cost = Decimal(str(position.get("cost_price", "0")))
            price = Decimal(
                str(
                    position.get("last_price")
                    or position.get("market_price")
                    or "0"
                )
            )
            multiplier = (
                Decimal("100")
                if position.get("instrument_type") == "OPTION"
                else Decimal("1")
            )
            return (price - cost) * quantity * multiplier - self.config.sell_fee_dollars
        except Exception:
            return Decimal("0")

    def position_day_pnl(self, position: dict) -> Decimal:
        """This position's mark-to-market move since the prior session's
        4pm ET close, net of the flat sell fee not yet paid - Webull's own
        day_profit_loss field, which resets independently of when the
        position was originally opened (unlike unrealized_profit_loss,
        which accumulates since cost for as long as the position is held).

        A whole-share position opened before today has no local record of
        yesterday's close to fall back to, so an unreported field returns
        0 (unknown) rather than silently substituting the since-cost
        figure under a "today" label. A fractional position is the one
        exception: Webull's fractional order type is core-hours-only and
        cannot be held overnight, so a fractional position was, by
        construction, always opened earlier the same day - "since cost"
        and "since today" are the same number for it, making the
        since-cost fallback exact rather than a guess. Live complaint:
        the dashboard's open/daily P&L read wrong specifically for
        fractional holdings - this is the gap that explains it, since
        Webull doesn't always report day_profit_loss for a fractional
        position, and the unconditional-0 fallback silently understated
        (or hid entirely) exactly those positions' contribution.
        """
        reported = position.get("day_profit_loss")
        if reported not in (None, ""):
            try:
                return Decimal(str(reported)) - self.config.sell_fee_dollars
            except Exception:
                return Decimal("0")
        try:
            quantity = Decimal(str(position.get("quantity", "0")))
        except Exception:
            return Decimal("0")
        if quantity != quantity.to_integral_value():
            return self.position_unrealized_pnl(position)
        return Decimal("0")

    @staticmethod
    def portfolio_decision(
        position_states: list[dict],
        minimum_losers: int,
        loss_threshold: Decimal,
    ) -> PortfolioDecision:
        losing = [
            item
            for item in position_states
            if Decimal(str(item.get("unrealized_pnl", "0"))) < 0
        ]
        if len(losing) < minimum_losers:
            return PortfolioDecision(
                "HOLD",
                "loss cluster below position threshold",
            )
        total_loss = -sum(
            Decimal(str(item["unrealized_pnl"]))
            for item in losing
        )
        if total_loss >= loss_threshold:
            return PortfolioDecision(
                "LIQUIDATE",
                "simultaneous loss threshold reached",
                len(losing),
                total_loss,
            )
        return PortfolioDecision(
            "HOLD",
            "loss cluster below liquidation threshold",
            len(losing),
            total_loss,
        )
