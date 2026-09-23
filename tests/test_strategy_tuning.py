import json
import shutil
import time
import unittest
import unittest.mock
from collections import deque
from decimal import Decimal
from pathlib import Path

from webull_bot.strategy import TradingStrategy
from webull_bot.strategy_tuning import (
    LEVER_SPECS,
    SAFETY_DENYLIST,
    StrategyTuningState,
    apply_lever_adjustment,
)

from support.fixtures import StrategyConfigMixin


class StrategyTuningTests(StrategyConfigMixin, unittest.TestCase):
    def test_vwap_gate_blocks_entry_below_session_vwap(self):
        strategy = TradingStrategy(self.config())
        strategy.update_stock_snapshot(
            {"symbol": "VWAPTEST", "volume": "1000", "price": "10"},
            Decimal("10"),
        )
        strategy.update_stock_snapshot(
            {"symbol": "VWAPTEST", "volume": "2000", "price": "12"},
            Decimal("12"),
        )
        self.assertEqual(strategy.vwap("VWAPTEST"), Decimal("12"))
        self.assertFalse(strategy.vwap_supports_entry("VWAPTEST", Decimal("9")))
        self.assertTrue(strategy.vwap_supports_entry("VWAPTEST", Decimal("12")))

    def test_vwap_gate_does_not_block_entry_without_data(self):
        strategy = TradingStrategy(self.config())
        self.assertTrue(strategy.vwap_supports_entry("UNSEEN", Decimal("5")))

    def test_sma_trend_gate_off_by_default_passes_regardless_of_data(self):
        strategy = TradingStrategy(self.config())
        strategy.sma_trend["BELOWTREND"] = Decimal("100")
        self.assertTrue(
            strategy.sma_trend_supports_entry("BELOWTREND", Decimal("50"))
        )

    def test_sma_trend_gate_blocks_price_below_the_daily_sma(self):
        config = self.config()
        config.sma_trend_filter_enabled = True
        strategy = TradingStrategy(config)
        strategy.sma_trend["TREND"] = Decimal("100")
        self.assertFalse(strategy.sma_trend_supports_entry("TREND", Decimal("99")))
        self.assertTrue(strategy.sma_trend_supports_entry("TREND", Decimal("100")))
        self.assertTrue(strategy.sma_trend_supports_entry("TREND", Decimal("101")))

    def test_sma_trend_gate_does_not_block_entry_without_data(self):
        config = self.config()
        config.sma_trend_filter_enabled = True
        strategy = TradingStrategy(config)
        self.assertTrue(strategy.sma_trend_supports_entry("UNSEEN", Decimal("5")))

    def test_recent_momentum_gate_blocks_a_steep_recent_decline(self):
        """By request: "look at tickers in the last 10 mins for
        momentum... to analyze the upcoming trend." A 10% decline over
        the lookback window is steeper than the 5% default threshold -
        a real breakdown, not a normal dip.
        """
        config = self.config()
        config.recent_momentum_filter_enabled = True
        config.recent_momentum_max_decline_percent = Decimal("0.05")
        strategy = TradingStrategy(config)
        strategy.recent_momentum["BREAKDOWN"] = Decimal("-0.10")
        self.assertFalse(
            strategy.recent_momentum_supports_entry("BREAKDOWN", "BUY")
        )

    def test_recent_momentum_gate_allows_a_normal_dip(self):
        """A moderate 2% pullback is exactly the setup this cohort
        exists to dip-buy - must NOT be blocked just for being negative.
        """
        config = self.config()
        config.recent_momentum_filter_enabled = True
        config.recent_momentum_max_decline_percent = Decimal("0.05")
        strategy = TradingStrategy(config)
        strategy.recent_momentum["NORMALDIP"] = Decimal("-0.02")
        self.assertTrue(
            strategy.recent_momentum_supports_entry("NORMALDIP", "BUY")
        )

    def test_recent_momentum_gate_does_not_block_entry_without_data(self):
        config = self.config()
        config.recent_momentum_filter_enabled = True
        strategy = TradingStrategy(config)
        self.assertTrue(
            strategy.recent_momentum_supports_entry("UNSEEN", "BUY")
        )

    def test_recent_momentum_gate_off_by_default_passes_regardless_of_data(self):
        config = self.config()
        config.recent_momentum_filter_enabled = False
        strategy = TradingStrategy(config)
        strategy.recent_momentum["BREAKDOWN"] = Decimal("-0.50")
        self.assertTrue(
            strategy.recent_momentum_supports_entry("BREAKDOWN", "BUY")
        )

    @staticmethod
    def _micro_exhaustion_config(base, **overrides):
        """StrategyConfigMixin.config() is a curated SimpleNamespace,
        not a real Settings() with defaults for every field - every
        field volatility_scalp_micro_exhaustion_confirmed/
        update_volume_delta read has to be set explicitly, same
        convention every other test in this class already follows.
        """
        base.volatility_scalp_micro_exhaustion_filter_enabled = overrides.get(
            "filter_enabled", True
        )
        base.volatility_scalp_micro_exhaustion_lookback_seconds = overrides.get(
            "lookback_seconds", 300
        )
        base.volatility_scalp_micro_exhaustion_velocity_percent = overrides.get(
            "velocity_percent", Decimal("0.025")
        )
        base.volatility_scalp_micro_exhaustion_wick_ratio = overrides.get(
            "wick_ratio", Decimal("0.40")
        )
        base.volatility_scalp_micro_exhaustion_volume_multiplier = overrides.get(
            "volume_multiplier", Decimal("2.5")
        )
        base.volatility_scalp_micro_exhaustion_volume_ema_alpha = overrides.get(
            "volume_ema_alpha", Decimal("0.2")
        )
        return base

    def test_update_volume_delta_first_call_only_sets_baseline(self):
        """First snapshot has no prior reading to diff against - by
        design, no delta/EMA update happens until the second call, the
        "deploy-resilient initialization" every restart needs.
        """
        strategy = TradingStrategy(self._micro_exhaustion_config(self.config()))
        strategy.update_volume_delta("VOL", Decimal("1000"))
        self.assertNotIn("VOL", strategy.volume_delta_ema)
        self.assertNotIn("VOL", strategy.volume_delta_latest)
        self.assertEqual(strategy.volume_delta_baseline["VOL"], Decimal("1000"))

    def test_update_volume_delta_second_call_computes_delta_and_ema(self):
        strategy = TradingStrategy(self._micro_exhaustion_config(self.config()))
        strategy.update_volume_delta("VOL", Decimal("1000"))
        strategy.update_volume_delta("VOL", Decimal("1300"))
        self.assertEqual(strategy.volume_delta_latest["VOL"], Decimal("300"))
        # First real EMA reading just equals the delta itself (no prior
        # EMA to blend with yet).
        self.assertEqual(strategy.volume_delta_ema["VOL"], Decimal("300"))

    def test_update_volume_delta_resets_baseline_on_day_rollover(self):
        """A cumulative volume reading LOWER than the prior one means
        the trading day rolled over (or stale data) - restart the
        baseline instead of recording a nonsensical negative delta.
        """
        strategy = TradingStrategy(self._micro_exhaustion_config(self.config()))
        strategy.update_volume_delta("VOL", Decimal("5000"))
        strategy.update_volume_delta("VOL", Decimal("6000"))
        strategy.update_volume_delta("VOL", Decimal("100"))
        self.assertEqual(strategy.volume_delta_baseline["VOL"], Decimal("100"))
        # The stale/rollover call must not corrupt the EMA from before.
        self.assertEqual(strategy.volume_delta_ema["VOL"], Decimal("1000"))

    def test_micro_exhaustion_gate_fires_when_all_three_conditions_met(self):
        config = self._micro_exhaustion_config(self.config())
        strategy = TradingStrategy(config)
        # Local high 100 -> low 95 (a 5% drop, past the 2.5% velocity
        # bar) -> recovered to 97 (a 40% wick off the low, exactly at
        # the bar: (97-95)/(100-95) = 0.4).
        strategy.recent_tick_history["EXHAUST"].append((0.0, Decimal("100")))
        strategy.recent_tick_history["EXHAUST"].append((10.0, Decimal("95")))
        strategy.volume_delta_ema["EXHAUST"] = Decimal("100")
        strategy.volume_delta_latest["EXHAUST"] = Decimal("300")
        self.assertTrue(
            strategy.volatility_scalp_micro_exhaustion_confirmed(
                "EXHAUST", Decimal("97"), 20.0
            )
        )

    def test_micro_exhaustion_gate_blocks_a_normal_dip_with_no_volume_spike(self):
        """Same price action as the passing case above, but the volume
        delta never actually spiked - a normal dip, not a capitulation
        event, must NOT pass just from price action alone.
        """
        config = self._micro_exhaustion_config(self.config())
        strategy = TradingStrategy(config)
        strategy.recent_tick_history["NOVOL"].append((0.0, Decimal("100")))
        strategy.recent_tick_history["NOVOL"].append((10.0, Decimal("95")))
        strategy.volume_delta_ema["NOVOL"] = Decimal("100")
        strategy.volume_delta_latest["NOVOL"] = Decimal("100")
        self.assertFalse(
            strategy.volatility_scalp_micro_exhaustion_confirmed(
                "NOVOL", Decimal("97"), 20.0
            )
        )

    def test_micro_exhaustion_gate_ignores_samples_outside_the_lookback_window(self):
        """A sample older than the lookback window must not count
        toward the local high/low - proves the time-anchored filtering
        (not sample count) actually works.
        """
        config = self._micro_exhaustion_config(self.config(), lookback_seconds=60)
        strategy = TradingStrategy(config)
        # A much older, unrelated high far outside the 60s window -
        # must be excluded, leaving fewer than 2 valid samples and
        # failing open.
        strategy.recent_tick_history["STALE"].append((0.0, Decimal("200")))
        strategy.volume_delta_ema["STALE"] = Decimal("100")
        strategy.volume_delta_latest["STALE"] = Decimal("300")
        self.assertTrue(
            strategy.volatility_scalp_micro_exhaustion_confirmed(
                "STALE", Decimal("97"), 1000.0
            )
        )

    def test_micro_exhaustion_gate_off_by_default_passes_regardless_of_data(self):
        config = self._micro_exhaustion_config(self.config(), filter_enabled=False)
        strategy = TradingStrategy(config)
        strategy.recent_tick_history["ANY"].append((0.0, Decimal("100")))
        strategy.recent_tick_history["ANY"].append((10.0, Decimal("95")))
        strategy.volume_delta_ema["ANY"] = Decimal("100")
        strategy.volume_delta_latest["ANY"] = Decimal("100")
        self.assertTrue(
            strategy.volatility_scalp_micro_exhaustion_confirmed(
                "ANY", Decimal("97"), 20.0
            )
        )

    def test_micro_exhaustion_gate_does_not_block_entry_without_data(self):
        config = self._micro_exhaustion_config(self.config())
        strategy = TradingStrategy(config)
        self.assertTrue(
            strategy.volatility_scalp_micro_exhaustion_confirmed(
                "UNSEEN", Decimal("5"), 100.0
            )
        )

    def test_trend_signal_fires_short_on_a_fresh_bearish_cross(self):
        strategy = TradingStrategy(self.config())
        key = "STOCK:TEST"
        uptrend = [10, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8]
        for price in uptrend:
            strategy.trend_signal(key, Decimal(str(price)))
        # 10.4 still confirms the ongoing uptrend (reenter_on_trend fires
        # here per the shared fixture's reenter_confirmation_polls=2,
        # unrelated to shorting). By request, after live evidence
        # (6 of 7 open positions underwater, entered right as a fresh
        # cross fired): a fresh cross no longer fires the same tick it
        # happens - it needs one more confirming tick in the same
        # direction first (reenter_confirmation_polls=2), same delay
        # the continuation case already used. 10.3 is the fresh cross
        # itself (HOLD, not yet confirmed); 10.2 confirms it (SHORT).
        self.assertEqual(strategy.trend_signal(key, Decimal("10.4")), "BUY")
        self.assertEqual(strategy.trend_signal(key, Decimal("10.3")), "HOLD")
        self.assertEqual(strategy.trend_signal(key, Decimal("10.2")), "SHORT")

    def test_vwap_gate_short_direction_blocks_price_above_vwap(self):
        strategy = TradingStrategy(self.config())
        strategy.update_stock_snapshot(
            {"symbol": "SHORTVWAP", "volume": "1000", "price": "10"},
            Decimal("10"),
        )
        strategy.update_stock_snapshot(
            {"symbol": "SHORTVWAP", "volume": "2000", "price": "12"},
            Decimal("12"),
        )
        self.assertFalse(
            strategy.vwap_supports_entry("SHORTVWAP", Decimal("13"), "SHORT")
        )
        self.assertTrue(
            strategy.vwap_supports_entry("SHORTVWAP", Decimal("12"), "SHORT")
        )

    def test_extension_gate_short_direction_blocks_chasing_todays_low(self):
        strategy = TradingStrategy(self.config())
        strategy.metrics["DIPPED"] = {"low": 50.0}
        # By request ("days high only matters when it is a straight jump
        # pattern, once it stabilizes it is fine"): the buffer is only
        # enforced while recent_momentum shows the stock still actively
        # racing toward that low - set it here so this test still
        # exercises the buffer itself, not the new "stabilized" bypass
        # (covered separately below).
        strategy.recent_momentum["DIPPED"] = Decimal("-0.10")
        self.assertFalse(
            strategy.entry_extension_ok("DIPPED", Decimal("50.2"), direction="SHORT")
        )
        self.assertTrue(
            strategy.entry_extension_ok("DIPPED", Decimal("52.0"), direction="SHORT")
        )

    def test_tick_direction_short_requires_downticks(self):
        strategy = TradingStrategy(self.config())
        for price in ["10", "9.9", "9.8", "9.7"]:
            strategy.trend_signal("STOCK:DOWN", Decimal(price))
        self.assertTrue(strategy.tick_direction_ok("STOCK:DOWN", "SHORT"))
        for price in ["10", "10.1", "10.2", "10.3"]:
            strategy.trend_signal("STOCK:UP", Decimal(price))
        self.assertFalse(strategy.tick_direction_ok("STOCK:UP", "SHORT"))

    def test_stock_decision_opens_a_short_on_a_fresh_bearish_cross(self):
        config = self.config()
        config.short_selling_enabled = True
        # Tick-direction confirmation is exercised separately in
        # test_tick_direction_short_requires_downticks - disabled here so
        # this test isolates just the SHORT entry gate/sizing mechanics.
        config.tick_direction_enabled = False
        strategy = TradingStrategy(config)
        key = "STOCK:TEST"
        uptrend = [10, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8]
        for price in uptrend:
            strategy.stock_decision(key, Decimal(str(price)), 0, Decimal("0"))
        strategy.stock_decision(key, Decimal("10.4"), 0, Decimal("0"))
        # Fresh cross (10.3) needs one more confirming tick (10.2) before
        # firing - see trend_signal's own comment on this.
        strategy.stock_decision(key, Decimal("10.3"), 0, Decimal("0"))
        decision = strategy.stock_decision(key, Decimal("10.2"), 0, Decimal("0"))
        self.assertEqual(decision.action, "SHORT")

    def test_stock_decision_short_signal_is_a_noop_when_disabled(self):
        config = self.config()
        config.short_selling_enabled = False
        strategy = TradingStrategy(config)
        key = "STOCK:TEST"
        uptrend = [10, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8]
        for price in uptrend:
            strategy.stock_decision(key, Decimal(str(price)), 0, Decimal("0"))
        strategy.stock_decision(key, Decimal("10.4"), 0, Decimal("0"))
        decision = strategy.stock_decision(key, Decimal("10.3"), 0, Decimal("0"))
        self.assertEqual(decision.action, "HOLD")

    def test_stock_decision_holds_when_cost_diverges_implausibly_from_price(self):
        """Regression test for a live incident: a held position (FPE) kept
        computing a PROFIT target 13-60% above the live market price and
        repeatedly submitting a limit order that could never fill, because
        a quote field the target derived from had drifted far from
        reality. The guard only needs to suppress that unreachable-target
        path - a price implausibly *above* cost, not below (see the
        sibling test confirming a stop still fires on a real, large
        decline - the guard must never block that).
        """
        strategy = TradingStrategy(self.config())
        key = "STOCK:FPE"
        # price (12) is 20% above cost (10) - without the guard this fires
        # PROFIT off a suspect gain (target maxes ~2.7% above cost here).
        decision = strategy.stock_decision(key, Decimal("12"), 1, Decimal("10"))
        self.assertEqual(decision.action, "HOLD")
        self.assertEqual(
            decision.reason, "cost basis diverges implausibly from live price"
        )

    def test_stock_decision_short_position_also_guards_against_implausible_cost(self):
        strategy = TradingStrategy(self.config())
        key = "STOCK:SHORTBAD"
        # price (8) is 20% below cost (10) - the mirror of the long case:
        # a suspiciously large paper gain for a short, not a loss.
        decision = strategy.stock_decision(key, Decimal("8"), -1, Decimal("10"))
        self.assertEqual(decision.action, "HOLD")
        self.assertEqual(
            decision.reason, "cost basis diverges implausibly from live price"
        )

    def test_stock_decision_stop_loss_still_fires_despite_implausible_cost_divergence(self):
        """Regression test for a live incident: a real, large decline
        (AZI, ~31% since entry - a genuinely volatile stock, not bad
        data: bid/ask were tight and consistent with the last-trade price)
        sat with no working stop at all, because the sanity guard used to
        run before the stop check too, not just before the profit target.
        A stop-loss must always be able to fire regardless of how
        implausible the divergence looks - failing to protect capital is
        worse than a hypothetical bad cost reading.
        """
        strategy = TradingStrategy(self.config())
        key = "STOCK:AZI"
        decision = strategy.stock_decision(key, Decimal("1.21"), 1, Decimal("1.75"))
        self.assertEqual(decision.action, "LOSS")

    def test_stock_decision_tolerates_a_normal_divergence_within_the_sanity_band(self):
        """A real, if unusually large, intraday move shouldn't trip the
        guard - only a divergence beyond what this strategy's own stop
        discipline would ever let survive should.
        """
        strategy = TradingStrategy(self.config())
        key = "STOCK:MOVED"
        # ~10% divergence - well under the 15% sanity band but still much
        # larger than a normal scalp - should reach real stop/target math,
        # not the sanity guard (the stop fires here since 10% down blows
        # through even a widened adaptive stop).
        decision = strategy.stock_decision(key, Decimal("90"), 1, Decimal("100"))
        self.assertEqual(decision.action, "LOSS")

    def test_stock_decision_short_position_stop_and_profit_are_mirrored(self):
        strategy = TradingStrategy(self.config())
        key = "STOCK:SHORTPOS"
        # Shorted at 100: a short profits as price falls, stops out as
        # price rises - the exact mirror of the long-side math.
        loss = strategy.stock_decision(key, Decimal("102"), -10, Decimal("100"))
        self.assertEqual(loss.action, "LOSS")

        profit = strategy.stock_decision(key, Decimal("90"), -10, Decimal("100"))
        self.assertEqual(profit.action, "PROFIT")

        hold = strategy.stock_decision(key, Decimal("99.9"), -10, Decimal("100"))
        self.assertEqual(hold.action, "HOLD")

    def test_stock_decision_buy_blocked_when_price_is_below_sma_trend(self):
        config = self.config()
        config.sma_trend_filter_enabled = True
        strategy = TradingStrategy(config)
        strategy.sma_trend["TREND"] = Decimal("100")
        key = "STOCK:TREND"
        downtrend = [10, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.2]
        for price in downtrend:
            strategy.stock_decision(key, Decimal(str(price)), 0, Decimal("0"))
        strategy.stock_decision(key, Decimal("9.6"), 0, Decimal("0"))
        decision = strategy.stock_decision(key, Decimal("9.7"), 0, Decimal("0"))
        self.assertEqual(decision.action, "HOLD")
        self.assertEqual(decision.reason, "price below the higher-timeframe SMA trend")

    def test_extension_gate_blocks_entry_right_at_todays_high(self):
        strategy = TradingStrategy(self.config())
        strategy.metrics["SPIKED"] = {"high": 100.0}
        strategy.recent_momentum["SPIKED"] = Decimal("0.10")
        self.assertFalse(strategy.entry_extension_ok("SPIKED", Decimal("99.5")))
        self.assertTrue(strategy.entry_extension_ok("SPIKED", Decimal("98.0")))

    def test_extension_gate_does_not_block_entry_without_data(self):
        strategy = TradingStrategy(self.config())
        self.assertTrue(strategy.entry_extension_ok("UNSEEN", Decimal("50")))

    def test_extension_gate_does_not_block_a_stabilized_high(self):
        """By request: "days high only matters when it is a straight
        jump pattern, once it stabilizes it is fine." Same price/high
        pair test_extension_gate_blocks_entry_right_at_todays_high
        rejects, but WITHOUT the recent-momentum "still jumping"
        evidence - price sitting near the high here is a stabilized
        level, not a spike being chased.
        """
        strategy = TradingStrategy(self.config())
        strategy.metrics["SPIKED"] = {"high": 100.0}
        strategy.recent_momentum["SPIKED"] = Decimal("0.01")
        self.assertTrue(strategy.entry_extension_ok("SPIKED", Decimal("99.5")))

    def test_opening_grace_widens_extension_gate(self):
        strategy = TradingStrategy(self.config())
        strategy.metrics["SPIKED"] = {"high": 100.0}
        strategy.recent_momentum["SPIKED"] = Decimal("0.10")
        # Same price/high pair the plain gate rejects above - the grace
        # multiplier (2x -> 2% room instead of 1%) should let it through.
        self.assertFalse(strategy.entry_extension_ok("SPIKED", Decimal("99.5")))
        self.assertTrue(
            strategy.entry_extension_ok("SPIKED", Decimal("99.5"), True)
        )

    def test_opening_grace_widens_spread_gate(self):
        strategy = TradingStrategy(self.config())
        strategy.metrics["WIDE"] = {"spread_percent": 0.25}
        key = "STOCK:WIDE"
        self.assertFalse(strategy.entry_spread_ok(key))
        self.assertTrue(strategy.entry_spread_ok(key, True))

    def test_idle_cash_relaxation_widens_extension_gate(self):
        strategy = TradingStrategy(self.config())
        strategy.metrics["SPIKED"] = {"high": 100.0}
        strategy.recent_momentum["SPIKED"] = Decimal("0.10")
        self.assertFalse(strategy.entry_extension_ok("SPIKED", Decimal("99.5")))
        self.assertTrue(
            strategy.entry_extension_ok(
                "SPIKED", Decimal("99.5"), False, "BUY", Decimal("2")
            )
        )

    def test_idle_cash_relaxation_widens_spread_gate(self):
        strategy = TradingStrategy(self.config())
        strategy.metrics["WIDE"] = {"spread_percent": 0.25}
        key = "STOCK:WIDE"
        self.assertFalse(strategy.entry_spread_ok(key))
        self.assertTrue(
            strategy.entry_spread_ok(key, False, Decimal("2"))
        )

    def test_extended_hours_widens_spread_gate(self):
        """By request: "we do not want intense play in extended hours,
        but we want play for sure" - live evidence: "spread too wide
        to scalp profitably" rejected candidates ~2-3x more than every
        other reason combined over a multi-hour pre-market stretch,
        since the tight core-hours 0.15% (test config)/0.5% (real
        default) threshold is realistic for real liquidity but hard to
        clear pre-market. core_session_active=False widens it via
        extended_hours_spread_multiplier, same "max with whatever else
        is active" convention as opening_grace/idle-cash relaxation.
        """
        strategy = TradingStrategy(self.config())
        strategy.metrics["WIDE"] = {"spread_percent": 0.40}
        key = "STOCK:WIDE"
        # 0.40 clears neither the bare 0.15 threshold nor core-hours
        # defaults (idle multiplier 1) - but 0.15*3=0.45 does, once
        # outside core hours.
        self.assertFalse(strategy.entry_spread_ok(key))
        self.assertFalse(strategy.entry_spread_ok(key, core_session_active=True))
        self.assertTrue(strategy.entry_spread_ok(key, core_session_active=False))

    def test_extended_hours_multiplier_does_not_apply_during_core_hours(self):
        strategy = TradingStrategy(self.config())
        # 0.60 clears NEITHER the bare threshold (0.15) NOR the
        # extended-hours-widened one (0.15*3=0.45) - core_session_
        # active=True must not accidentally apply the wider bound.
        strategy.metrics["VERYWIDE"] = {"spread_percent": 0.60}
        key = "STOCK:VERYWIDE"
        self.assertFalse(strategy.entry_spread_ok(key, core_session_active=True))

    def test_extended_hours_and_idle_cash_relaxation_take_the_larger_multiplier(self):
        strategy = TradingStrategy(self.config())
        strategy.metrics["WIDE"] = {"spread_percent": 0.40}
        key = "STOCK:WIDE"
        # extended_hours_spread_multiplier=3 alone already covers 0.40
        # (0.15*3=0.45) - a smaller idle multiplier shouldn't override it.
        self.assertTrue(
            strategy.entry_spread_ok(
                key, False, Decimal("1.1"), core_session_active=False
            )
        )

    def test_idle_cash_relaxation_and_opening_grace_take_the_larger_multiplier(self):
        """Both mechanisms widen the same gates for different reasons -
        whichever justifies more room this cycle should win, not average
        or stack multiplicatively.
        """
        strategy = TradingStrategy(self.config())
        strategy.metrics["WIDE"] = {"spread_percent": 0.25}
        key = "STOCK:WIDE"
        # opening_grace_multiplier=2 alone already covers a 0.25 spread
        # (0.15*2=0.30) - a smaller idle multiplier shouldn't override it.
        self.assertTrue(
            strategy.entry_spread_ok(key, True, Decimal("1.1"))
        )

    def test_idle_cash_relaxation_widens_vwap_band(self):
        strategy = TradingStrategy(self.config())
        strategy._update_vwap("TEST", Decimal("100"), 100.0)
        strategy._update_vwap("TEST", Decimal("100"), 200.0)
        # 0.5% below VWAP - outside the default 0.1% band, inside a 10x one.
        price = Decimal("99.5")
        self.assertFalse(strategy.vwap_supports_entry("TEST", price))
        self.assertTrue(
            strategy.vwap_supports_entry("TEST", price, "BUY", Decimal("10"))
        )

    def test_idle_cash_relaxation_lowers_tick_direction_veto(self):
        strategy = TradingStrategy(self.config())
        key = "STOCK:TICKY"
        for price in (10.0, 9.9, 9.8):
            strategy.tick_history[key].append(price)
        # All downticks - score is -1, fails the default (>=0) BUY veto.
        self.assertFalse(strategy.tick_direction_ok(key))
        self.assertTrue(
            strategy.tick_direction_ok(key, "BUY", Decimal("1.5"))
        )

    def test_adaptive_stop_percent_is_clamped_between_configured_bounds(self):
        strategy = TradingStrategy(self.config())
        strategy.metrics["CALM"] = {"range_ratio": 0.001}
        strategy.metrics["WILD"] = {"range_ratio": 0.10}
        self.assertEqual(strategy.adaptive_stop_percent("CALM"), Decimal("0.0015"))
        self.assertEqual(strategy.adaptive_stop_percent("WILD"), Decimal("0.02"))

    def test_reentry_requires_confirmation_polls_after_initial_crossover(self):
        strategy = TradingStrategy(self.config())
        key = "STOCK:REENTRY"
        downtrend = [10, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.2]
        for price in downtrend:
            strategy.trend_signal(key, Decimal(str(price)))

        # By this point the downtrend has already held for
        # reenter_confirmation_polls cycles, so this re-fires "SHORT" -
        # trend_streak's re-entry counter is shared symmetrically between
        # directions (see test_reentry_requires_confirmation_polls_for_a_
        # continuing_downtrend_too for a dedicated check of that).
        self.assertEqual(strategy.trend_signal(key, Decimal("9.6")), "SHORT")
        # By request, after live evidence (6 of 7 open positions
        # underwater, entered right as a fresh cross fired): a fresh
        # crossover no longer fires the same tick it happens - it needs
        # one more confirming tick in the same direction first, same as
        # the continuation case already required.
        self.assertEqual(strategy.trend_signal(key, Decimal("9.7")), "HOLD")
        self.assertEqual(strategy.trend_signal(key, Decimal("9.9")), "BUY")
        # Once the uptrend has held for the configured confirmation polls,
        # it keeps firing on every subsequent tick.
        self.assertEqual(strategy.trend_signal(key, Decimal("10.2")), "BUY")

    def test_reentry_requires_confirmation_polls_for_a_continuing_downtrend_too(self):
        """SHORT's re-entry mechanism mirrors BUY's exactly - without it, a
        short entry needs VWAP/SMA-trend/extension/tick-direction to all
        align on the single exact tick of the fresh bearish cross, which
        in production essentially never happened. A persisting downtrend
        must get repeated chances, the same way a persisting uptrend
        already does for BUY.
        """
        strategy = TradingStrategy(self.config())
        key = "STOCK:SHORTREENTRY"
        uptrend = [10, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8]
        for price in uptrend:
            strategy.trend_signal(key, Decimal(str(price)))

        self.assertEqual(strategy.trend_signal(key, Decimal("10.4")), "BUY")
        # By request, after live evidence (6 of 7 open positions
        # underwater, entered right as a fresh cross fired): a fresh
        # crossover no longer fires the same tick it happens - it needs
        # one more confirming tick in the same direction first.
        self.assertEqual(strategy.trend_signal(key, Decimal("10.3")), "HOLD")
        self.assertEqual(strategy.trend_signal(key, Decimal("10.1")), "SHORT")
        # Once the downtrend has held for the configured confirmation
        # polls, it keeps firing on every subsequent tick.
        self.assertEqual(strategy.trend_signal(key, Decimal("9.8")), "SHORT")

    def test_tick_direction_score_ranges_from_all_downticks_to_all_upticks(self):
        strategy = TradingStrategy(self.config())
        self.assertEqual(strategy.tick_direction_score("STOCK:NODATA"), Decimal("0"))

        for price in ["10", "10.1", "10.2", "10.3"]:
            strategy.trend_signal("STOCK:UP", Decimal(price))
        self.assertEqual(strategy.tick_direction_score("STOCK:UP"), Decimal("1"))

        for price in ["10", "9.9", "9.8", "9.7"]:
            strategy.trend_signal("STOCK:DOWN", Decimal(price))
        self.assertEqual(strategy.tick_direction_score("STOCK:DOWN"), Decimal("-1"))

    def test_tick_direction_veto_blocks_an_otherwise_qualifying_ema_entry(self):
        """Real bid/ask depth isn't available from the quote feed, so tick
        direction (net upticks vs downticks in the recent tape) is the
        closest proxy for order-flow imbalance. An EMA crossover can fire
        right as a long downtrend just barely turns - the smoothed EMA
        already reads "up" while the last several individual prints are
        still dominated by the downtrend that preceded it. This must hold
        instead of chasing an entry the raw tape doesn't yet support.
        """
        strategy = TradingStrategy(self.config())
        key = "STOCK:REENTRY2"
        downtrend = [10, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.2]
        for price in downtrend:
            strategy.trend_signal(key, Decimal(str(price)))
        strategy.trend_signal(key, Decimal("9.6"))
        # By request, after live evidence: a fresh crossover now needs
        # one more confirming tick before trend_signal itself fires -
        # 9.7 is that unconfirmed fresh cross (HOLD); 9.9 is what
        # actually confirms it into a real "BUY" trend read, which is
        # the point this test's tick-direction veto needs to intercept.
        strategy.trend_signal(key, Decimal("9.7"))

        decision = strategy.stock_decision(key, Decimal("9.9"), 0, Decimal("0"), None)
        self.assertEqual(decision.action, "HOLD")
        self.assertIn("recent ticks", decision.reason)

    def test_tick_direction_disabled_lets_the_ema_entry_through_regardless(self):
        config = self.config()
        config.tick_direction_enabled = False
        strategy = TradingStrategy(config)
        key = "STOCK:REENTRY3"
        downtrend = [10, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.2]
        for price in downtrend:
            strategy.trend_signal(key, Decimal(str(price)))
        strategy.trend_signal(key, Decimal("9.6"))
        # See test_tick_direction_veto_blocks_an_otherwise_qualifying_
        # ema_entry's own comment - 9.7 is the unconfirmed fresh cross,
        # 9.9 is what confirms it into a real "BUY" trend read.
        strategy.trend_signal(key, Decimal("9.7"))

        decision = strategy.stock_decision(key, Decimal("9.9"), 0, Decimal("0"), None)
        self.assertEqual(decision.action, "BUY")

    def test_recurring_crossovers_boost_priority_score(self):
        strategy = TradingStrategy(self.config())
        choppy = [10, 9.8, 9.6, 9.8, 10, 9.8, 9.6, 9.8, 10, 9.8, 9.6, 9.8, 10, 9.8, 9.6]
        for price in choppy:
            strategy.trend_signal("STOCK:CHOP", Decimal(str(price)))
        smooth = [Decimal("10") + Decimal(str(i)) * Decimal("0.1") for i in range(15)]
        for price in smooth:
            strategy.trend_signal("STOCK:SMOOTH", price)

        self.assertGreater(strategy.crossover_counts["CHOP"], strategy.crossover_counts["SMOOTH"])

        strategy.activity["CHOP"] = 5.0
        strategy.activity["SMOOTH"] = 5.0
        self.assertGreater(
            strategy.priority_score("CHOP", None),
            strategy.priority_score("SMOOTH", None),
        )

    def test_clear_market_state_resets_crossover_counts(self):
        strategy = TradingStrategy(self.config())
        for price in [10, 9.8, 9.6, 9.8, 10, 9.8, 9.6, 9.8, 10, 9.8]:
            strategy.trend_signal("STOCK:CHOP", Decimal(str(price)))
        self.assertGreater(strategy.crossover_counts["CHOP"], 0)
        strategy.clear_market_state()
        self.assertEqual(strategy.crossover_counts["CHOP"], 0)

    def test_clear_market_state_resets_tick_history(self):
        strategy = TradingStrategy(self.config())
        for price in [10, 9.9, 9.8, 9.7]:
            strategy.trend_signal("STOCK:CHOP", Decimal(str(price)))
        self.assertNotEqual(strategy.tick_direction_score("STOCK:CHOP"), Decimal("0"))
        strategy.clear_market_state()
        self.assertEqual(strategy.tick_direction_score("STOCK:CHOP"), Decimal("0"))

    def test_stop_and_target_scale_with_adaptive_stop_percent(self):
        strategy = TradingStrategy(self.config())
        strategy.metrics["WILD"] = {"range_ratio": 0.10}
        decision = strategy.stock_decision(
            "STOCK:WILD",
            Decimal("97.9"),
            10,
            Decimal("100"),
            None,
        )
        self.assertEqual(decision.action, "LOSS")

    def test_fractional_position_targets_a_smaller_move_than_whole_share(self):
        """A fractional position can only be exited during core hours at
        all, so it should cycle capital quickly (many trades/hour) rather
        than sit waiting for the same larger move a whole-share position
        can hold toward - a fractional position's target is just the flat
        cost-recovery floor (stock_min_net_profit_percent +
        stock_estimated_round_trip_cost_percent), not also scaled by the
        adaptive stop like whole-share's is, so at the same elevated
        volatility the fractional target is reached first.
        """
        strategy = TradingStrategy(self.config())
        strategy.metrics["WILD"] = {"range_ratio": 0.10}
        price = Decimal("101.80")

        whole_share = strategy.stock_decision(
            "STOCK:WILD", price, 10, Decimal("100"), None
        )
        self.assertEqual(whole_share.action, "HOLD")

        fractional = strategy.stock_decision(
            "STOCK:WILD", price, Decimal("0.5"), Decimal("100"), None
        )
        self.assertEqual(fractional.action, "PROFIT")

    def test_fractional_target_ignores_the_adaptive_stop_scaling(self):
        """Regression test: a fractional target that also scales with
        stop_percent * FRACTIONAL_TARGET_STOP_MULTIPLE (like whole-share
        does) combines badly with a tiny fractional quantity's inflated
        fee-per-share (SELL_FEE_DOLLARS spread over well under 1 share) -
        together they demanded far more absolute price appreciation than
        "capture the profit quickly" intends. Confirmed against a real
        live position (MSFT, 0.0108 shares, cost $493.23): the old target
        math required ~$498.63 before firing; the position was already
        genuinely profitable at ~$496.93 and should have sold. Using just
        the flat cost-recovery floor for a fractional position's target
        (skipping the stop-scaled term entirely) fixes it.
        """
        strategy = TradingStrategy(self.config())
        strategy.metrics["MSFT"] = {"range_ratio": 0.10}  # elevated stop_percent
        decision = strategy.stock_decision(
            "STOCK:MSFT",
            Decimal("496.9337037037037037037037037"),
            Decimal("0.0108"),
            Decimal("493.23"),
            None,
        )
        self.assertEqual(decision.action, "PROFIT")

    def test_profit_target_waits_for_momentum_to_stall_before_taking_profit(self):
        """By explicit request: "when you buy and there is momentum
        up, only sell when the momentum shifts to down, or when the
        profit is decreasing... sell during that initial momentum run
        itself after the buy." Reaching the target price used to take
        profit immediately, even mid-run, capping every winner at the
        same fixed size. Whole-share only.
        """
        strategy = TradingStrategy(self.config())
        key = "STOCK:RUN"
        symbol = "RUN"
        for price in [100, 100.3, 100.6, 100.9, 101.2, 101.5]:
            strategy.update_stock_snapshot(
                {"symbol": symbol, "volume": "1000", "price": str(price)},
                Decimal(str(price)),
            )
        decision = strategy.stock_decision(
            key, Decimal("101.5"), 10, Decimal("95"), None
        )
        self.assertEqual(decision.action, "HOLD")

    def test_profit_target_fires_once_momentum_stalls(self):
        strategy = TradingStrategy(self.config())
        key = "STOCK:RUN2"
        symbol = "RUN2"
        for price in [100, 100.3, 100.6, 100.9, 101.2, 101.5]:
            strategy.update_stock_snapshot(
                {"symbol": symbol, "volume": "1000", "price": str(price)},
                Decimal(str(price)),
            )
        strategy.stock_decision(key, Decimal("101.5"), 10, Decimal("95"), None)
        # Two consecutive ticks failing to make a fresh high - a real
        # plateau, not single-tick noise.
        for price in [101.4, 101.3]:
            strategy.update_stock_snapshot(
                {"symbol": symbol, "volume": "1000", "price": str(price)},
                Decimal(str(price)),
            )
        decision = strategy.stock_decision(
            key, Decimal("101.3"), 10, Decimal("95"), None
        )
        self.assertEqual(decision.action, "PROFIT")

    def test_profit_target_fires_immediately_without_momentum_data(self):
        """With no volatility_price_history at all for this symbol,
        the momentum-stall gate must fail open (fire immediately) -
        the default action here is "take the profit," and withholding
        it needs positive evidence of still-running momentum, not the
        absence of data.
        """
        strategy = TradingStrategy(self.config())
        key = "STOCK:NODATA"
        decision = strategy.stock_decision(
            key, Decimal("101.5"), 10, Decimal("95"), None
        )
        self.assertEqual(decision.action, "PROFIT")

    def test_profit_target_ignores_momentum_stall_for_a_fractional_position(self):
        strategy = TradingStrategy(self.config())
        key = "STOCK:RUN3"
        symbol = "RUN3"
        for price in [100, 100.3, 100.6, 100.9, 101.2, 101.5]:
            strategy.update_stock_snapshot(
                {"symbol": symbol, "volume": "1000", "price": str(price)},
                Decimal(str(price)),
            )
        decision = strategy.stock_decision(
            key, Decimal("101.5"), Decimal("0.5"), Decimal("95"), None
        )
        self.assertEqual(decision.action, "PROFIT")

    def test_option_decision_cuts_loss_before_it_reaches_zero(self):
        strategy = TradingStrategy(self.config())
        decision = strategy.option_decision(
            Decimal("0.40"),
            5,
            Decimal("1.00"),
            10,
        )
        self.assertEqual(decision.action, "LOSS")

    def test_option_decision_holds_above_stop_and_below_target(self):
        strategy = TradingStrategy(self.config())
        decision = strategy.option_decision(
            Decimal("0.90"),
            5,
            Decimal("1.00"),
            10,
        )
        self.assertEqual(decision.action, "HOLD")

    def test_option_decision_forces_exit_inside_the_dte_window(self):
        strategy = TradingStrategy(self.config())
        profit_case = strategy.option_decision(
            Decimal("1.10"),
            5,
            Decimal("1.00"),
            2,
        )
        self.assertEqual(profit_case.action, "PROFIT")
        self.assertIn("time decay exit", profit_case.reason)

        loss_case = strategy.option_decision(
            Decimal("0.90"),
            5,
            Decimal("1.00"),
            2,
        )
        self.assertEqual(loss_case.action, "LOSS")
        self.assertIn("time decay exit", loss_case.reason)

    def test_option_decision_no_position_holds(self):
        strategy = TradingStrategy(self.config())
        decision = strategy.option_decision(Decimal("1.00"), 0, Decimal("0"), 10)
        self.assertEqual(decision.action, "HOLD")

    def test_option_direction_signal_fires_call_on_a_fresh_bullish_cross(self):
        strategy = TradingStrategy(self.config())
        key = "OPTU:TEST"
        downtrend = [10, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.2]
        for price in downtrend:
            strategy.option_direction_signal(key, Decimal(str(price)))
        # 9.6 still confirms the ongoing downtrend (reenter_on_trend fires
        # here per the shared fixture's reenter_confirmation_polls=2 - by
        # request, "re-fire on a continued trend, not just the fresh
        # cross," same mechanism trend_signal already uses) - the fresh
        # bullish cross only fires once the EMA spread actually flips
        # positive, at 9.7.
        self.assertEqual(strategy.option_direction_signal(key, Decimal("9.6")), "PUT")
        self.assertEqual(strategy.option_direction_signal(key, Decimal("9.7")), "CALL")

    def test_option_direction_signal_fires_put_on_a_fresh_bearish_cross(self):
        strategy = TradingStrategy(self.config())
        key = "OPTU:TEST"
        uptrend = [10, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8]
        for price in uptrend:
            strategy.option_direction_signal(key, Decimal(str(price)))
        # Same reenter_on_trend continuation as the CALL case above, mirror
        # image: 10.4 still confirms the ongoing uptrend.
        self.assertEqual(strategy.option_direction_signal(key, Decimal("10.4")), "CALL")
        self.assertEqual(strategy.option_direction_signal(key, Decimal("10.3")), "PUT")

    def test_option_direction_signal_reenters_on_a_continued_trend(self):
        """By explicit request ("screw the direction signal... re-fire
        on a continued trend, not just the fresh cross"): unlike the
        old behavior (fire once on the fresh cross, then go quiet even
        if the trend keeps going), this should keep re-firing every
        REENTER_CONFIRMATION_POLLS cycles the trend continues - a real
        signal landing on a cycle whose contract never reaches the
        gate-check loop is no longer a permanently missed opportunity.
        """
        strategy = TradingStrategy(self.config())
        key = "OPTU:CONTINUE"
        uptrend = [10, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8]
        for price in uptrend:
            strategy.option_direction_signal(key, Decimal(str(price)))
        # The fresh cross already fired once during the loop above; by
        # 10.9 the streak has held long enough (reenter_confirmation_
        # polls=2) to re-fire CALL again on the still-continuing trend.
        self.assertEqual(strategy.option_direction_signal(key, Decimal("10.9")), "CALL")

    def test_option_entry_confirmed_requires_direction(self):
        strategy = TradingStrategy(self.config())
        self.assertFalse(strategy.option_entry_confirmed("HOLD", None, None))
        self.assertTrue(strategy.option_entry_confirmed("CALL", None, None))
        self.assertTrue(strategy.option_entry_confirmed("PUT", None, None))

    def test_option_entry_confirmed_checks_tick_alignment(self):
        strategy = TradingStrategy(self.config())
        self.assertTrue(strategy.option_entry_confirmed("CALL", Decimal("0.5"), None))
        self.assertFalse(strategy.option_entry_confirmed("CALL", Decimal("-0.5"), None))
        self.assertTrue(strategy.option_entry_confirmed("PUT", Decimal("-0.5"), None))
        self.assertFalse(strategy.option_entry_confirmed("PUT", Decimal("0.5"), None))

    def test_option_entry_confirmed_checks_obi_alignment(self):
        strategy = TradingStrategy(self.config())
        self.assertTrue(strategy.option_entry_confirmed("CALL", None, Decimal("0.70")))
        self.assertFalse(strategy.option_entry_confirmed("CALL", None, Decimal("0.30")))
        self.assertTrue(strategy.option_entry_confirmed("PUT", None, Decimal("0.30")))
        self.assertFalse(strategy.option_entry_confirmed("PUT", None, Decimal("0.70")))

    def test_option_delta_ok_rejects_outside_the_directional_band(self):
        self.assertTrue(TradingStrategy.option_delta_ok(None))
        self.assertTrue(TradingStrategy.option_delta_ok(Decimal("0.45")))
        self.assertTrue(TradingStrategy.option_delta_ok(Decimal("-0.45")))
        self.assertFalse(TradingStrategy.option_delta_ok(Decimal("0.05")))
        self.assertFalse(TradingStrategy.option_delta_ok(Decimal("0.95")))

    def test_option_iv_percentile_ok_passes_with_sparse_history(self):
        history = deque([Decimal("0.3")] * 3, maxlen=30)
        self.assertTrue(
            TradingStrategy.option_iv_percentile_ok(history, Decimal("0.9"))
        )

    def test_option_iv_percentile_ok_rejects_the_priciest_tail(self):
        history = deque(
            [Decimal(str(0.20 + 0.01 * i)) for i in range(20)], maxlen=30
        )
        self.assertFalse(
            TradingStrategy.option_iv_percentile_ok(history, Decimal("0.50"))
        )
        self.assertTrue(
            TradingStrategy.option_iv_percentile_ok(history, Decimal("0.20"))
        )

    def test_option_market_regime_ok_rejects_a_vixy_spike(self):
        history = deque([Decimal(str(15 + i)) for i in range(20)], maxlen=30)
        self.assertFalse(
            TradingStrategy.option_market_regime_ok(history, Decimal("40"))
        )
        self.assertTrue(
            TradingStrategy.option_market_regime_ok(history, Decimal("15"))
        )
        self.assertTrue(TradingStrategy.option_market_regime_ok(history, None))

    def test_option_order_quantity_applies_the_capital_fraction_cap(self):
        strategy = TradingStrategy(self.config(focus_mode_enabled=False))
        # $1 premium -> $100/contract. 5% of $500 buying power caps at 0
        # contracts even though option_quantity/max_order_notional would
        # otherwise allow one. focus_mode_enabled=False so this exercises
        # the generic (non-focus) caps this test is actually about - see
        # test_focus_mode_ignores_option_quantity_and_notional_caps below
        # for the focus-mode full-utilization behavior.
        quantity, contract_cost = strategy.option_order_quantity(
            Decimal("1.00"), Decimal("500")
        )
        self.assertEqual(contract_cost, Decimal("100"))
        self.assertEqual(quantity, 0)

        quantity, _ = strategy.option_order_quantity(
            Decimal("1.00"), Decimal("5000")
        )
        self.assertEqual(quantity, 1)

    def test_real_config_default_lets_the_risk_cap_size_multiple_contracts(self):
        # By request: "you can buy multiple contracts, it does not have
        # to be only 1" - option_quantity used to default to 1 and,
        # since option_order_quantity takes the MIN against it, that
        # was always the binding cap regardless of buying power. Using
        # the REAL Config (not the test fixture's hardcoded
        # option_quantity=1) confirms the risk/notional/affordability
        # caps now actually get to size more than one contract.
        from webull_bot.config import Settings

        config = Settings(_env_file=None, focus_mode_enabled=False)
        strategy = TradingStrategy(config)
        # $1 premium -> $100/contract. By explicit request ("i told you
        # it should spend all the money if needed"),
        # option_capital_fraction is now 1.0 - no sizing cap at all -
        # so MAX_ORDER_NOTIONAL ($1000) becomes the binding limit here
        # at 10 contracts, rather than the fraction. option_quantity's
        # default ceiling must not clamp this down to 1.
        # focus_mode_enabled=False here - it defaults True and exempts
        # both option_quantity and max_order_notional entirely (see the
        # focus-mode test below), which would make this specific
        # max_order_notional assertion moot.
        quantity, _ = strategy.option_order_quantity(
            Decimal("1.00"), Decimal("5000")
        )
        self.assertEqual(quantity, 10)

    def test_focus_mode_ignores_option_quantity_and_notional_caps(self):
        """option_quantity (20) and max_order_notional ($1000) are flat
        ceilings left over from the old multi-symbol strategy. They
        would silently undercut sizing on a cheap contract or a larger
        account, so focus mode exempts both - the only bound is
        option_capital_fraction.

        That fraction is now 0.4, NOT 1.0. It was 1.0 when focus mode
        meant ONE symbol and concentration was the point; with a
        cohort of ten it just meant the first contract evaluated ate
        the whole balance and the other nine never got funded. Live
        2026-09-23: BABA took $290 of a $369 account (79%) in one
        name, so a single wrong direction call was the entire
        account.

        This pins both halves: the flat ceilings must NOT clamp
        (20 contracts and 10 contracts respectively would both bind
        here), while the capital fraction MUST.
        """
        from webull_bot.config import Settings

        config = Settings(_env_file=None)
        self.assertTrue(config.focus_mode_enabled)  # the real default
        strategy = TradingStrategy(config)
        # $1 premium -> $100/contract, $5000 buying power. Pure
        # affordability would be 50 contracts; option_quantity=20 and
        # max_order_notional=$1000 (10 contracts) must NOT clamp it;
        # capital_fraction 0.4 must, at 20 contracts ($2000 of $5000).
        quantity, _ = strategy.option_order_quantity(
            Decimal("1.00"), Decimal("5000")
        )
        expected = int(Decimal("5000") * config.option_capital_fraction / 100)
        self.assertEqual(quantity, expected)
        self.assertLess(
            quantity, 50, "the capital fraction must bound the size"
        )
        self.assertGreater(
            quantity,
            10,
            "max_order_notional ($1000 = 10 contracts) must stay exempt",
        )

    def test_real_config_default_captures_a_realistic_quick_pop_as_profit(self):
        # By request: "if there is immediate profit after a buy, why
        # are you waiting to sell it, just capture the profit" / "it
        # just ends up going down then." option_take_profit_percent
        # used to default to 0.75 (a 75% premium gain) - rare enough
        # that a real, favorable quick pop routinely reversed before
        # ever hitting it. Using the REAL Settings default (not a
        # test fixture's hardcoded value) confirms a realistic ~20%
        # premium pop right after entry now actually triggers PROFIT.
        from webull_bot.config import Settings

        config = Settings(_env_file=None)
        strategy = TradingStrategy(config)
        decision = strategy.option_decision(
            price=Decimal("1.20"),
            quantity=1,
            average_cost=Decimal("1.00"),
            days_to_expiration=20,
        )
        self.assertEqual(decision.action, "PROFIT")

    def test_option_average_down_signal_widens_the_required_drop_per_level(self):
        # By request: "you can also use averaging down... for options
        # as well" - same widening-ladder shape as volatility_scalp_
        # average_down_signal, using the real Settings defaults
        # (20% base dip, 0.5x step multiplier).
        from webull_bot.config import Settings

        config = Settings(_env_file=None)
        strategy = TradingStrategy(config)
        # Level 0: needs >= 20% drop.
        self.assertFalse(
            strategy.option_average_down_signal(
                Decimal("0.85"), Decimal("1.00"), level=0
            )
        )
        self.assertTrue(
            strategy.option_average_down_signal(
                Decimal("0.79"), Decimal("1.00"), level=0
            )
        )
        # Level 1: needs >= 30% drop (20% * (1 + 0.5*1)) - the same 21%
        # drop that qualified at level 0 must NOT qualify at level 1.
        self.assertFalse(
            strategy.option_average_down_signal(
                Decimal("0.79"), Decimal("1.00"), level=1
            )
        )
        self.assertTrue(
            strategy.option_average_down_signal(
                Decimal("0.69"), Decimal("1.00"), level=1
            )
        )

    def test_option_average_down_signal_rejects_non_positive_inputs(self):
        strategy = TradingStrategy(self.config())
        self.assertFalse(
            strategy.option_average_down_signal(Decimal("1.00"), Decimal("0"))
        )
        self.assertFalse(
            strategy.option_average_down_signal(Decimal("0"), Decimal("1.00"))
        )


class StrategyTuningLeverTests(unittest.TestCase):
    """apply_lever_adjustment is pure - no file I/O, no config mutation
    itself (the caller is responsible for actually editing config.py and
    running the full verification suite before committing). See
    strategy_tuning.py's own module docstring/comments for why each
    lever maps to the field it does.
    """

    def test_safety_denylist_has_no_overlap_with_any_lever_field(self):
        lever_fields = {spec.field for spec in LEVER_SPECS.values()}
        lever_fields.update(
            spec.enabled_field
            for spec in LEVER_SPECS.values()
            if spec.enabled_field
        )
        lever_fields.update(
            {
                "stock_core_session_position_fraction",
                "stock_whole_share_core_session_fraction",
            }
        )
        self.assertEqual(lever_fields & SAFETY_DENYLIST, set())

    def test_increase_moves_toward_the_maximum_for_a_direct_lever(self):
        # profit-target distance: increase_raises=True.
        result = apply_lever_adjustment(
            "profit-target distance",
            "increase",
            {"stock_target_stop_multiple": Decimal("2.0")},
            Decimal("0.10"),
        )
        # span = 5 - 0.5 = 4.5; step = 0.45.
        self.assertEqual(result.field, "stock_target_stop_multiple")
        self.assertEqual(result.new_value, Decimal("2.45"))

    def test_increase_moves_toward_the_minimum_for_an_inverted_lever(self):
        # stop-loss tightness: increase_raises=False (tighter = smaller
        # multiplier), so "increase" (tightness) LOWERS the field.
        result = apply_lever_adjustment(
            "stop-loss tightness",
            "increase",
            {"stock_stop_loss_range_multiplier": Decimal("1.0")},
            Decimal("0.10"),
        )
        # span = 5 - 0 = 5; step = 0.5.
        self.assertEqual(result.new_value, Decimal("0.5"))

    def test_decrease_is_the_exact_opposite_of_increase(self):
        result = apply_lever_adjustment(
            "entry selectivity",
            "decrease",
            {"reenter_confirmation_polls": Decimal("10")},
            Decimal("0.10"),
        )
        # span = 20 - 1 = 19; step = 1.9.
        self.assertEqual(result.new_value, Decimal("8.1"))

    def test_clamps_at_the_maximum_instead_of_overshooting(self):
        result = apply_lever_adjustment(
            "profit-target distance",
            "increase",
            {"stock_target_stop_multiple": Decimal("4.9")},
            Decimal("0.10"),
        )
        self.assertEqual(result.new_value, Decimal("5"))

    def test_returns_none_when_already_at_the_bound(self):
        result = apply_lever_adjustment(
            "profit-target distance",
            "increase",
            {"stock_target_stop_multiple": Decimal("5")},
            Decimal("0.10"),
        )
        self.assertIsNone(result)

    def test_enable_disable_direction_never_produces_a_numeric_adjustment(self):
        result = apply_lever_adjustment(
            "time-aware-stop widen window",
            "enable",
            {"time_aware_stop_widen_seconds": Decimal("60")},
            Decimal("0.10"),
        )
        self.assertIsNone(result)

    def test_unknown_lever_is_a_safe_noop(self):
        result = apply_lever_adjustment(
            "made up lever", "increase", {}, Decimal("0.10")
        )
        self.assertIsNone(result)

    def test_fractional_whole_share_balance_shifts_both_fields_preserving_the_sum(self):
        result = apply_lever_adjustment(
            "fractional-vs-whole-share balance",
            "increase",
            {
                "stock_core_session_position_fraction": Decimal("0.30"),
                "stock_whole_share_core_session_fraction": Decimal("0.70"),
            },
            Decimal("0.10"),
        )
        self.assertEqual(result.new_value, Decimal("0.40"))
        self.assertEqual(result.paired_new_value, Decimal("0.60"))
        self.assertEqual(result.new_value + result.paired_new_value, Decimal("1"))

    def test_fractional_whole_share_balance_decrease_shifts_toward_whole_share(self):
        result = apply_lever_adjustment(
            "fractional-vs-whole-share balance",
            "decrease",
            {
                "stock_core_session_position_fraction": Decimal("0.30"),
                "stock_whole_share_core_session_fraction": Decimal("0.70"),
            },
            Decimal("0.10"),
        )
        self.assertEqual(result.new_value, Decimal("0.20"))
        self.assertEqual(result.paired_new_value, Decimal("0.80"))

    def test_fractional_whole_share_balance_clamps_and_still_sums_to_one(self):
        result = apply_lever_adjustment(
            "fractional-vs-whole-share balance",
            "increase",
            {
                "stock_core_session_position_fraction": Decimal("0.95"),
                "stock_whole_share_core_session_fraction": Decimal("0.05"),
            },
            Decimal("0.10"),
        )
        self.assertEqual(result.new_value, Decimal("1"))
        self.assertEqual(result.paired_new_value, Decimal("0"))


class StrategyTuningStateTests(unittest.TestCase):
    def test_a_never_adjusted_lever_is_ready_immediately(self):
        path = Path("tests/.generated_status/strategy_tuning.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            state = StrategyTuningState(str(path))
            self.assertTrue(state.ready("profit-target distance", 24))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_a_recently_adjusted_lever_is_not_ready_within_the_cooldown(self):
        path = Path("tests/.generated_status/strategy_tuning2.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            state = StrategyTuningState(str(path))
            state.record("profit-target distance")
            self.assertFalse(state.ready("profit-target distance", 24))
            # A different lever is unaffected.
            self.assertTrue(state.ready("entry selectivity", 24))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_ready_again_after_the_cooldown_elapses(self):
        path = Path("tests/.generated_status/strategy_tuning3.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            state = StrategyTuningState(str(path))
            state._last_applied["profit-target distance"] = (
                time.time() - 25 * 3600
            )
            state.path.parent.mkdir(parents=True, exist_ok=True)
            state.path.write_text(
                json.dumps(state._last_applied), encoding="utf-8"
            )
            reloaded = StrategyTuningState(str(path))
            self.assertTrue(reloaded.ready("profit-target distance", 24))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_state_persists_across_instances(self):
        path = Path("tests/.generated_status/strategy_tuning4.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            StrategyTuningState(str(path)).record("position size")
            reloaded = StrategyTuningState(str(path))
            self.assertFalse(reloaded.ready("position size", 24))
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)
