import time
import unittest
import unittest.mock
from decimal import Decimal
from types import SimpleNamespace

from webull_bot.strategy import TradingStrategy

from support.fixtures import StrategyConfigMixin


class VolatilityScalpTests(StrategyConfigMixin, unittest.TestCase):
    """Buy small dips, sell small rips, repeatedly, on symbols whose own
    realized short-window volatility clears volatility_scalp_min_stdev_
    percent - see strategy.py's realized_volatility_percent/
    is_volatility_scalp_eligible/volatility_scalp_dip_signal/
    volatility_scalp_exit_override.
    """

    def _feed(self, strategy, symbol, prices):
        for price in prices:
            strategy.update_stock_snapshot(
                {"symbol": symbol, "volume": "1000", "price": str(price)},
                Decimal(str(price)),
            )

    def test_not_eligible_with_too_few_samples(self):
        strategy = TradingStrategy(self.config())
        self._feed(strategy, "THIN", [10, 10.1, 9.9])
        self.assertIsNone(strategy.realized_volatility_percent("THIN"))
        self.assertFalse(strategy.is_volatility_scalp_eligible("THIN"))

    def test_calm_symbol_is_not_eligible(self):
        strategy = TradingStrategy(self.config())
        # Tiny back-and-forth well under the 1.5% stdev threshold.
        self._feed(strategy, "CALM", [10.00, 10.01, 9.99, 10.00, 10.01, 9.99, 10.00])
        stdev = strategy.realized_volatility_percent("CALM")
        self.assertIsNotNone(stdev)
        self.assertLess(stdev, self.config().volatility_scalp_min_stdev_percent)
        self.assertFalse(strategy.is_volatility_scalp_eligible("CALM"))

    def test_choppy_symbol_is_eligible(self):
        strategy = TradingStrategy(self.config())
        # Swings of several percent each step - clears the 1.5% stdev bar.
        self._feed(strategy, "WILD", [10, 10.5, 9.6, 10.4, 9.7, 10.3, 9.8])
        stdev = strategy.realized_volatility_percent("WILD")
        self.assertIsNotNone(stdev)
        self.assertGreaterEqual(stdev, self.config().volatility_scalp_min_stdev_percent)
        self.assertTrue(strategy.is_volatility_scalp_eligible("WILD"))

    def test_high_stdev_low_dollar_volume_symbol_is_not_eligible(self):
        """By request: "the stocks being chosen have very low volume,
        thus they do not fluctuate much, we need high volume stocks
        for more volatility." A thin name can clear the stdev bar
        purely from a few small prints, without real tradeable volume
        behind the move.
        """
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_min_dollar_volume = Decimal("5000000")
        self._feed(strategy, "THINWILD", [10, 10.5, 9.6, 10.4, 9.7, 10.3, 9.8])
        stdev = strategy.realized_volatility_percent("THINWILD")
        self.assertIsNotNone(stdev)
        self.assertGreaterEqual(stdev, self.config().volatility_scalp_min_stdev_percent)
        # _feed only supplies "volume": "1000" per tick - ~$10k dollar
        # volume at this price, well under the $5M floor.
        self.assertFalse(strategy.is_volatility_scalp_eligible("THINWILD"))

    def test_high_stdev_high_dollar_volume_symbol_is_eligible(self):
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_min_dollar_volume = Decimal("5000000")
        for price in (10, 10.5, 9.6, 10.4, 9.7, 10.3, 9.8):
            strategy.update_stock_snapshot(
                {"symbol": "LIQUIDWILD", "volume": "600000", "price": str(price)},
                Decimal(str(price)),
            )
        # 600,000 shares * ~$9.80 =~ $5.88M - clears the $5M floor.
        self.assertTrue(strategy.is_volatility_scalp_eligible("LIQUIDWILD"))

    def test_high_share_volume_low_price_is_not_eligible(self):
        """Live incident (this bug, caught the same day it shipped): a
        raw SHARE-count floor doesn't scale with price. SOAR cleared
        500,000 shares of "volume" at ~$0.28/share - only ~$140k of
        real dollar liquidity, too thin to absorb this strategy's own
        repeated order flow. Its PROFIT exit failed to fill even after
        three escalation-and-reprice cycles, forcing a market-order
        exit at a loss. A high SHARE count at a low price must still
        fail the (now dollar-based) floor.
        """
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_min_dollar_volume = Decimal("5000000")
        for price in (0.28, 0.29, 0.27, 0.28, 0.275, 0.282, 0.278):
            strategy.update_stock_snapshot(
                {"symbol": "SOAR", "volume": "600000", "price": str(price)},
                Decimal(str(price)),
            )
        # 600,000 shares * ~$0.28 =~ $168k - nowhere near the $5M floor,
        # even though the raw share count alone would have cleared the
        # old, buggy 500,000-share threshold.
        self.assertFalse(strategy.is_volatility_scalp_eligible("SOAR"))

    def test_disabled_in_config_is_never_eligible_even_when_choppy(self):
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_enabled = False
        self._feed(strategy, "WILD", [10, 10.5, 9.6, 10.4, 9.7, 10.3, 9.8])
        self.assertFalse(strategy.is_volatility_scalp_eligible("WILD"))

    def test_dip_signal_fires_once_price_pulls_back_far_enough_from_the_local_high(self):
        strategy = TradingStrategy(self.config())
        self._feed(strategy, "WILD", [10, 10.5, 9.6, 10.4, 9.7, 10.3, 9.8])
        # Local high is over the last 5 samples only (9.6, 10.4, 9.7,
        # 10.3, 9.8) = 10.4, NOT the whole window's 10.5 - a stock making
        # new highs every sample (a strong trend) should still register a
        # real short pullback as a dip. 0.5% below 10.4 is 10.348.
        self.assertFalse(strategy.volatility_scalp_dip_signal("WILD", Decimal("10.36")))
        self.assertTrue(strategy.volatility_scalp_dip_signal("WILD", Decimal("10.30")))

    def test_dip_signal_reacts_to_recent_pullback_even_in_a_strong_uptrend(self):
        """Live incident: HOWL, up ~100% intraday, kept making new window
        highs almost every sample - a dip measured against the WHOLE
        window's high almost never fired. Measuring against only the
        last few samples keeps the signal reactive to real local
        pullbacks regardless of the larger trend.
        """
        strategy = TradingStrategy(self.config())
        # A strong uptrend where every new sample is a new all-time-
        # window high, then one small pullback (6.90), THEN a small
        # bounce off that pullback (6.95) - see the bounce-confirmation
        # tests below for why the entry now waits for the bounce tick.
        self._feed(strategy, "HOWL", [1, 2, 3, 4, 5, 5.5, 6, 6.5, 7, 6.90])
        self.assertFalse(strategy.volatility_scalp_dip_signal("HOWL", Decimal("6.99")))
        self.assertTrue(strategy.volatility_scalp_dip_signal("HOWL", Decimal("6.95")))

    def test_dip_signal_false_for_an_unseen_symbol(self):
        strategy = TradingStrategy(self.config())
        self.assertFalse(strategy.volatility_scalp_dip_signal("NEVERSEEN", Decimal("10")))

    def test_dip_signal_does_not_require_a_bounce_by_design(self):
        """By explicit request: keep buying the dip continuously, even
        while price is still actively declining tick to tick, rather
        than waiting for a confirmed reversal - the user wants constant,
        high-frequency trading on this cohort and accepts the resulting
        losses as the cost of that. An earlier version of this signal
        required an uptick from the immediately-preceding sample before
        firing; that requirement has been deliberately removed.
        """
        strategy = TradingStrategy(self.config())
        # 10.5 -> 10.4 -> 10.3 -> 10.2: still declining every tick.
        self._feed(strategy, "WILD", [10.5, 10.4, 10.3, 10.2])
        # Still falling relative to the last sample (10.2), but clears
        # the drop-from-local-high threshold - fires anyway.
        self.assertTrue(strategy.volatility_scalp_dip_signal("WILD", Decimal("10.15")))

    def test_dip_signal_excludes_the_current_price_when_already_appended(self):
        """In live usage (via update_stock_snapshot), the window's last
        element IS already this same price by the time this runs. If the
        local-high lookback naively included it as an extra sample, the
        real recent high can get pushed out of the last-N window and the
        signal would compare price against itself (0% drop, never a
        dip) instead of the actual recent high.
        """
        strategy = TradingStrategy(self.config())
        # True recent high is 10.5 (the oldest sample) - if the current
        # price (9.5) were double-counted as a 6th sample, the last-5
        # lookback would push 10.5 out and see only 9.0s and the current
        # price itself, masking a real ~9.5% dip.
        strategy.volatility_price_history["WILD"].extend(
            [10.5, 9.0, 9.0, 9.0, 9.0, 9.5]
        )
        self.assertTrue(strategy.volatility_scalp_dip_signal("WILD", Decimal("9.5")))

    def test_rip_signal_fires_once_price_runs_up_far_enough_from_the_local_low(self):
        # Mirror-image of the dip-signal test - by request: "it
        # doesn't buy puts while there is a dip, or a call on a dip
        # entry" - a PUT needs the mirror-image "rip" signal the same
        # way a CALL uses the dip signal.
        strategy = TradingStrategy(self.config())
        self._feed(strategy, "WILD", [10, 9.5, 10.4, 9.6, 10.3, 9.7, 10.2])
        # Local low over the last 5 samples (10.4, 9.6, 10.3, 9.7,
        # 10.2) is 9.6. 0.5% above 9.6 is 9.648.
        self.assertFalse(strategy.volatility_scalp_rip_signal("WILD", Decimal("9.64")))
        self.assertTrue(strategy.volatility_scalp_rip_signal("WILD", Decimal("9.70")))

    def test_rip_signal_false_for_an_unseen_symbol(self):
        strategy = TradingStrategy(self.config())
        self.assertFalse(strategy.volatility_scalp_rip_signal("NEVERSEEN", Decimal("10")))

    def test_rip_signal_excludes_the_current_price_when_already_appended(self):
        strategy = TradingStrategy(self.config())
        strategy.volatility_price_history["WILD"].extend(
            [9.5, 10.0, 10.0, 10.0, 10.0, 10.5]
        )
        # True recent low is 9.5 (the oldest sample) - if the current
        # price (10.5, already appended as the window's last element,
        # same as real live usage) were double-counted as a 6th
        # sample, the last-5 lookback would push 9.5 out and mask the
        # real ~10.5% rise.
        self.assertTrue(strategy.volatility_scalp_rip_signal("WILD", Decimal("10.5")))

    def test_target_price_is_cost_plus_the_configured_small_percent(self):
        strategy = TradingStrategy(self.config())
        target = strategy.volatility_scalp_target_price(Decimal("20.00"))
        self.assertEqual(target, Decimal("20.00") * Decimal("1.005"))

    def test_exit_override_promotes_hold_to_profit_once_the_quick_target_is_cleared(self):
        strategy = TradingStrategy(self.config())
        from webull_bot.strategy import Decision

        hold = Decision("HOLD", "position between target and stop", Decimal("20.50"))
        result = strategy.volatility_scalp_exit_override(
            hold, quantity=10, average_cost=Decimal("20.00"), price=Decimal("20.15")
        )
        self.assertEqual(result.action, "PROFIT")
        self.assertEqual(result.target_price, Decimal("20.00") * Decimal("1.005"))

    def test_exit_override_leaves_hold_alone_below_the_quick_target(self):
        strategy = TradingStrategy(self.config())
        from webull_bot.strategy import Decision

        hold = Decision("HOLD", "position between target and stop", Decimal("20.50"))
        result = strategy.volatility_scalp_exit_override(
            hold, quantity=10, average_cost=Decimal("20.00"), price=Decimal("20.05")
        )
        self.assertIs(result, hold)

    def test_exit_override_suppresses_a_loss_in_favor_of_averaging_down(self):
        """By explicit request ("focus less on the stop loss" /
        "average it out with another buy"): a LOSS decision on a
        volatility-scalp position is downgraded to HOLD instead of
        being allowed to stop the position out - the position is meant
        to be averaged into on a dip (see AutoTrader's averaging-buy
        entry path), not exited at a loss.
        """
        strategy = TradingStrategy(self.config())
        from webull_bot.strategy import Decision

        loss = Decision("LOSS", "percentage stop reached", Decimal("19.00"))
        result = strategy.volatility_scalp_exit_override(
            loss, quantity=10, average_cost=Decimal("20.00"), price=Decimal("20.20")
        )
        self.assertEqual(result.action, "HOLD")

    def test_exit_override_lets_the_stop_through_past_the_hard_stop_floor(self):
        """Research finding (compared against freqtrade's documented DCA
        pattern after "basically only taking losses" was reported live):
        a mature DCA implementation never fully suppresses the stop-loss
        during averaging - it keeps a wide-but-always-active hard stop
        as a catastrophic-loss backstop. VOLATILITY_SCALP_HARD_STOP_
        PERCENT (5% in the test config) restores that: a drop beyond it
        means a real breakdown, not a normal dip within the DCA ladder's
        own range, so the real stop-loss is let through.
        """
        strategy = TradingStrategy(self.config())
        from webull_bot.strategy import Decision

        loss = Decision("LOSS", "percentage stop reached", Decimal("18.90"))
        # (20.00 - 18.90) / 20.00 = 5.5% - past the 5% hard stop floor.
        result = strategy.volatility_scalp_exit_override(
            loss,
            quantity=10,
            average_cost=Decimal("20.00"),
            price=Decimal("18.90"),
            averaging_available=True,
        )
        self.assertIs(result, loss)

    def test_exit_override_still_suppresses_a_loss_within_the_hard_stop_floor(self):
        strategy = TradingStrategy(self.config())
        from webull_bot.strategy import Decision

        loss = Decision("LOSS", "percentage stop reached", Decimal("19.50"))
        # (20.00 - 19.50) / 20.00 = 2.5% - well within the 5% floor.
        result = strategy.volatility_scalp_exit_override(
            loss,
            quantity=10,
            average_cost=Decimal("20.00"),
            price=Decimal("19.50"),
            averaging_available=True,
        )
        self.assertEqual(result.action, "HOLD")

    def test_exit_override_leaves_a_loss_alone_when_flat_or_no_cost_basis(self):
        strategy = TradingStrategy(self.config())
        from webull_bot.strategy import Decision

        loss = Decision("LOSS", "percentage stop reached", Decimal("19.00"))
        self.assertIs(
            strategy.volatility_scalp_exit_override(
                loss, quantity=0, average_cost=Decimal("20.00"), price=Decimal("20.20")
            ),
            loss,
        )
        self.assertIs(
            strategy.volatility_scalp_exit_override(
                loss, quantity=10, average_cost=Decimal("0"), price=Decimal("20.20")
            ),
            loss,
        )

    def test_exit_override_leaves_a_loss_alone_when_averaging_is_not_available(self):
        """Sanity-check fix: a position that gets the fast profit-take
        purely because its symbol is in the cohort, but was never opened
        via the dip-buy path (e.g. a normal trend-strategy position),
        has no averaging-down recovery plan behind it. Suppressing its
        stop-loss with nothing else backing it up would leave it
        bleeding indefinitely with no path back to even - its normal
        stop-loss must stay fully in effect.
        """
        strategy = TradingStrategy(self.config())
        from webull_bot.strategy import Decision

        loss = Decision("LOSS", "percentage stop reached", Decimal("19.00"))
        result = strategy.volatility_scalp_exit_override(
            loss,
            quantity=10,
            average_cost=Decimal("20.00"),
            price=Decimal("20.20"),
            averaging_available=False,
        )
        self.assertIs(result, loss)

    def test_exit_override_bearish_divergence_never_sells_a_fee_thin_gain_as_profit(self):
        """Live incident (AMD): a nominal price > cost isn't enough to
        safely call something PROFIT - the flat sell fee can still eat
        the whole margin (bought and sold at the same $0.15 tick,
        recorded as PROFIT, actually a -$0.02 realized loss). By
        explicit request ("still make sure to try and make profit, not
        sell a loss for a profit"): the bearish-divergence early exit
        must require clearing cost by more than fee_per_share before
        it's allowed to fire, not just a bare price > average_cost.
        """
        strategy = TradingStrategy(self.config())
        strategy.rsi_divergence = lambda symbol, price, moment: "BEARISH"
        from webull_bot.strategy import Decision

        hold = Decision("HOLD", "position between target and stop", Decimal("20.50"))

        # fee_per_share = 0.02 / 10 = 0.002 - price only 0.001 above
        # cost, less than the fee margin, so this must NOT fire.
        thin = strategy.volatility_scalp_exit_override(
            hold,
            quantity=10,
            average_cost=Decimal("20.000"),
            price=Decimal("20.001"),
            symbol="AMD",
        )
        self.assertIs(thin, hold)

        # A real margin clearing the fee DOES fire - price is below
        # the quick target (20.10, so that path doesn't fire first)
        # but well above cost + fee_per_share.
        real = strategy.volatility_scalp_exit_override(
            hold,
            quantity=10,
            average_cost=Decimal("20.00"),
            price=Decimal("20.05"),
            symbol="AMD",
        )
        self.assertEqual(real.action, "PROFIT")
        self.assertEqual(real.reason, "bearish RSI divergence - locking in the gain")

    def test_exit_override_sells_into_resistance_at_todays_high(self):
        """By explicit request ("as a human I can see and make profit
        off of the swings... seeing when there is resistance so just
        sell off the profit"): a profitable position within
        resistance_exit_band_percent of today's high should lock in
        the gain there, rather than waiting for the flat quick-target
        percentage to be hit exactly.
        """
        strategy = TradingStrategy(self.config())
        strategy.metrics["AMD"] = {"high": "20.10"}
        from webull_bot.strategy import Decision

        hold = Decision("HOLD", "position between target and stop", Decimal("20.50"))

        # Within the default 1% band of today's high (20.10) and
        # clears the fee margin - should fire.
        result = strategy.volatility_scalp_exit_override(
            hold,
            quantity=10,
            average_cost=Decimal("20.00"),
            price=Decimal("20.05"),
            symbol="AMD",
        )
        self.assertEqual(result.action, "PROFIT")
        self.assertEqual(result.reason, "selling into resistance at today's high")

    def test_exit_override_does_not_sell_into_resistance_far_below_the_high(self):
        strategy = TradingStrategy(self.config())
        strategy.metrics["AMD"] = {"high": "25.00"}
        from webull_bot.strategy import Decision

        hold = Decision("HOLD", "position between target and stop", Decimal("20.50"))

        result = strategy.volatility_scalp_exit_override(
            hold,
            quantity=10,
            average_cost=Decimal("20.00"),
            price=Decimal("20.05"),
            symbol="AMD",
        )
        self.assertIs(result, hold)

    def test_average_down_signal_fires_once_price_clears_the_dip_threshold(self):
        strategy = TradingStrategy(self.config())
        # dip_entry_percent default 0.2% - 0.15% below cost doesn't
        # clear it, 0.5% below does.
        self.assertFalse(
            strategy.volatility_scalp_average_down_signal(
                price=Decimal("19.97"), average_cost=Decimal("20.00")
            )
        )
        self.assertTrue(
            strategy.volatility_scalp_average_down_signal(
                price=Decimal("19.90"), average_cost=Decimal("20.00")
            )
        )

    def test_average_down_signal_false_above_cost_or_with_no_cost_basis(self):
        strategy = TradingStrategy(self.config())
        self.assertFalse(
            strategy.volatility_scalp_average_down_signal(
                price=Decimal("20.10"), average_cost=Decimal("20.00")
            )
        )
        self.assertFalse(
            strategy.volatility_scalp_average_down_signal(
                price=Decimal("19.00"), average_cost=Decimal("0")
            )
        )

    def test_average_down_signal_widens_the_required_drop_per_level(self):
        """Structural fix (not a same-day band-aid): live incident, BTCT
        averaged down at 1.79 then 1.78 - essentially the same price,
        gaining no real risk reduction per add. Each successive
        averaging level now requires a proportionally bigger drop -
        level 0 uses the base 0.5% (this test's config), level 1 needs
        1.5x that (0.75%), level 2 needs 2x (1.0%), etc., via
        VOLATILITY_SCALP_AVERAGING_STEP_MULTIPLIER (0.5 here).
        """
        strategy = TradingStrategy(self.config())
        cost = Decimal("20.00")
        # Level 0: base 0.5% - 19.91 (0.45%) doesn't clear, 19.90 (0.5%)
        # does, matching the un-widened test above.
        self.assertFalse(
            strategy.volatility_scalp_average_down_signal(
                price=Decimal("19.91"), average_cost=cost, level=0
            )
        )
        self.assertTrue(
            strategy.volatility_scalp_average_down_signal(
                price=Decimal("19.90"), average_cost=cost, level=0
            )
        )
        # Level 1: requires 1.5x the base (0.75%) - the same 0.5% drop
        # that cleared level 0 is no longer enough.
        self.assertFalse(
            strategy.volatility_scalp_average_down_signal(
                price=Decimal("19.90"), average_cost=cost, level=1
            )
        )
        self.assertTrue(
            strategy.volatility_scalp_average_down_signal(
                price=Decimal("19.85"), average_cost=cost, level=1
            )
        )
        # Level 2: requires 2x the base (1.0%).
        self.assertFalse(
            strategy.volatility_scalp_average_down_signal(
                price=Decimal("19.85"), average_cost=cost, level=2
            )
        )
        self.assertTrue(
            strategy.volatility_scalp_average_down_signal(
                price=Decimal("19.80"), average_cost=cost, level=2
            )
        )

    def test_clear_market_state_resets_the_volatility_window(self):
        strategy = TradingStrategy(self.config())
        self._feed(strategy, "WILD", [10, 10.5, 9.6, 10.4, 9.7, 10.3, 9.8])
        strategy.clear_market_state()
        self.assertIsNone(strategy.realized_volatility_percent("WILD"))

    def test_seed_volatility_window_populates_an_empty_window(self):
        strategy = TradingStrategy(self.config())
        strategy.seed_volatility_window("NEW", [10.0, 10.2, 9.9, 10.1, 9.8, 10.3])
        self.assertIsNotNone(strategy.realized_volatility_percent("NEW"))

    def test_seed_volatility_window_never_overwrites_existing_live_history(self):
        strategy = TradingStrategy(self.config())
        self._feed(strategy, "WILD", [10, 10.5, 9.6, 10.4, 9.7])
        before = list(strategy.volatility_price_history["WILD"])
        strategy.seed_volatility_window("WILD", [1.0, 1.1, 1.2, 1.3, 1.4, 1.5])
        self.assertEqual(list(strategy.volatility_price_history["WILD"]), before)

    def test_seed_volatility_window_skips_non_positive_closes(self):
        strategy = TradingStrategy(self.config())
        strategy.seed_volatility_window("NEW", [10.0, 0, -1, 10.2, 9.9])
        self.assertEqual(list(strategy.volatility_price_history["NEW"]), [10.0, 10.2, 9.9])

    def test_share_count_targets_the_configured_notional_under_a_dollar(self):
        """Rounds to the nearest 100 shares (Webull's own lot-restricted-
        band minimum under $1) - by request, not flatly capped at 100
        anymore, this genuinely scales with the target notional.
        """
        strategy = TradingStrategy(self.config())
        # 400 / 0.89 = 449.4... -> floor to 449 -> round down to the
        # nearest 100 -> 400 shares, well over the old flat 100.
        self.assertEqual(
            strategy.volatility_scalp_share_count(Decimal("0.89")), 400
        )

    def test_share_count_targets_the_configured_notional_at_a_dollar_and_up(self):
        """Rounds to the nearest 10 shares at $1+ - "in the tens, if not
        the hundreds," scaling with both price and the target notional.
        """
        strategy = TradingStrategy(self.config())
        # 400 / 1.99 = 201.0... -> floor to 201 -> round down to 200.
        self.assertEqual(
            strategy.volatility_scalp_share_count(Decimal("1.99")), 200
        )
        # 400 / 5.00 = 80.
        self.assertEqual(strategy.volatility_scalp_share_count(Decimal("5.00")), 80)

    def test_share_count_under_a_dollar_always_reaches_the_exchange_minimum(self):
        """Sub-$1 has no smaller valid order than 100 shares (Webull's
        own lot-restricted-band rule) - even a tiny target still rounds
        UP to it, since there's no smaller legal alternative. The
        caller's own affordability/exposure checks are the real
        backstop on this, not this function.
        """
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_target_notional = Decimal("5")
        self.assertEqual(strategy.volatility_scalp_share_count(Decimal("0.50")), 100)

    def test_share_count_at_a_dollar_and_up_degrades_gracefully_below_one_lot(self):
        """$1+ has no exchange-mandated minimum - a target too small for
        a full 10-share lot still buys whatever whole-share quantity it
        can actually afford, rather than forcing a 10-share lot or
        skipping the trade entirely.
        """
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_target_notional = Decimal("5")
        self.assertEqual(strategy.volatility_scalp_share_count(Decimal("4.00")), 1)

    def test_share_count_is_zero_above_the_max_price_cap(self):
        strategy = TradingStrategy(self.config())
        self.assertEqual(strategy.volatility_scalp_share_count(Decimal("5.01")), 0)

    def test_share_count_is_zero_for_a_non_positive_price(self):
        strategy = TradingStrategy(self.config())
        self.assertEqual(strategy.volatility_scalp_share_count(Decimal("0")), 0)

    def test_buying_power_fraction_shrinks_the_target_on_a_small_account(self):
        """Live sanity check caught this: on a small account, a flat
        dollar target alone gets silently zeroed by the caller's
        affordability check on nearly every attempt. Passing buying_
        power scales the actual target down automatically instead.
        """
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_target_notional = Decimal("400")
        strategy.config.volatility_scalp_target_notional_buying_power_fraction = (
            Decimal("0.15")
        )
        # buying_power=$107.80 -> target = min(400, 107.80*0.15=16.17).
        # 16.17 / 3.00 = 5.39 -> floor 5, under 10 -> degrades to 5.
        self.assertEqual(
            strategy.volatility_scalp_share_count(
                Decimal("3.00"), buying_power=Decimal("107.80")
            ),
            5,
        )

    def test_buying_power_none_falls_back_to_the_flat_target(self):
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_target_notional = Decimal("400")
        self.assertEqual(
            strategy.volatility_scalp_share_count(Decimal("5.00"), buying_power=None),
            80,
        )

    def test_non_positive_buying_power_falls_back_to_the_flat_target(self):
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_target_notional = Decimal("400")
        self.assertEqual(
            strategy.volatility_scalp_share_count(
                Decimal("5.00"), buying_power=Decimal("0")
            ),
            80,
        )

    def test_intensity_scales_down_a_dollar_and_up_trade_size(self):
        """By request: "lessen the intensity" outside core hours without
        stopping trading - intensity dampens the target notional for
        $1+ trades, where it's the only lever that actually changes
        anything (see the sub-$1 test just below for why).
        """
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_target_notional = Decimal("400")
        # Full intensity: 400 / 5.00 = 80.
        self.assertEqual(
            strategy.volatility_scalp_share_count(
                Decimal("5.00"), intensity=Decimal("1")
            ),
            80,
        )
        # 40% intensity: 160 / 5.00 = 32.
        self.assertEqual(
            strategy.volatility_scalp_share_count(
                Decimal("5.00"), intensity=Decimal("0.4")
            ),
            30,
        )

    def test_intensity_has_no_effect_on_the_sub_dollar_exchange_floor(self):
        """Sub-$1 orders always round UP to at least 100 shares - Webull's
        own lot-restricted-band minimum leaves no smaller valid order,
        so dampening the soft target can't shrink this specific trade
        size regardless of intensity.
        """
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_target_notional = Decimal("400")
        self.assertEqual(
            strategy.volatility_scalp_share_count(
                Decimal("0.50"), intensity=Decimal("0.1")
            ),
            100,
        )

    def test_intensity_clamped_to_the_zero_to_one_range(self):
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_target_notional = Decimal("400")
        # Negative or >1 intensity should behave like the nearest valid
        # bound (0 or 1), not silently invert or amplify sizing.
        self.assertEqual(
            strategy.volatility_scalp_share_count(
                Decimal("5.00"), intensity=Decimal("-1")
            ),
            0,
        )
        self.assertEqual(
            strategy.volatility_scalp_share_count(
                Decimal("5.00"), intensity=Decimal("2")
            ),
            80,
        )


class TrendEfficiencyRegimeTests(StrategyConfigMixin, unittest.TestCase):
    """trend_efficiency_ratio/symbol_regime - Kaufman's Efficiency
    Ratio, the regime input behind "momentum in trending conditions,
    mean-reversion in ranging ones." Reuses volatility_price_history,
    no new data source.
    """

    def _feed(self, strategy, symbol, prices):
        for price in prices:
            strategy.update_stock_snapshot(
                {"symbol": symbol, "volume": "1000", "price": str(price)},
                Decimal(str(price)),
            )

    def test_monotonic_move_is_fully_efficient_and_trending(self):
        strategy = TradingStrategy(self.config())
        self._feed(
            strategy, "TREND",
            [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9],
        )
        ratio = strategy.trend_efficiency_ratio("TREND")
        self.assertIsNotNone(ratio)
        self.assertAlmostEqual(float(ratio), 1.0, places=6)
        self.assertEqual(strategy.symbol_regime("TREND"), "TRENDING")

    def test_symmetric_oscillation_is_inefficient_and_ranging(self):
        strategy = TradingStrategy(self.config())
        # Ends exactly where it started (net movement 0) despite
        # substantial back-and-forth - the choppy/ranging shape.
        self._feed(
            strategy, "CHOP",
            [10, 10.2, 10, 10.2, 10, 10.2, 10, 10.2, 10, 10],
        )
        ratio = strategy.trend_efficiency_ratio("CHOP")
        self.assertIsNotNone(ratio)
        self.assertAlmostEqual(float(ratio), 0.0, places=6)
        self.assertEqual(strategy.symbol_regime("CHOP"), "RANGING")

    def test_unknown_with_insufficient_history(self):
        strategy = TradingStrategy(self.config())
        self._feed(strategy, "NEW", [10, 10.1, 10.2])
        self.assertIsNone(strategy.trend_efficiency_ratio("NEW"))
        self.assertEqual(strategy.symbol_regime("NEW"), "UNKNOWN")

    def test_unknown_for_a_never_seen_symbol(self):
        strategy = TradingStrategy(self.config())
        self.assertEqual(strategy.symbol_regime("NEVERSEEN"), "UNKNOWN")

    def test_unknown_when_every_tick_in_the_window_is_flat(self):
        """Zero total movement makes the ratio undefined (0/0), not a
        real "ranging" reading - must fail open (UNKNOWN), not crash.
        """
        strategy = TradingStrategy(self.config())
        self._feed(strategy, "FLAT", [10.0] * 10)
        self.assertIsNone(strategy.trend_efficiency_ratio("FLAT"))
        self.assertEqual(strategy.symbol_regime("FLAT"), "UNKNOWN")


class VolatilityScalpVwapGateTests(StrategyConfigMixin, unittest.TestCase):
    """volatility_scalp_vwap_supports_entry - by request, after an end-
    of-day retrospective ("we just kept buying at the wrong time"): the
    SMA trend filter only catches a multi-day downtrend, nothing for a
    stock simply having a bad DAY today specifically. Uses its own much
    wider band than the general vwap_supports_entry.
    """

    def test_blocks_when_price_sits_well_below_session_vwap(self):
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_vwap_band_percent = Decimal("0.05")
        # Feed volume so VWAP actually accumulates (cum_pv/cum_vol).
        strategy.update_stock_snapshot(
            {"symbol": "WEAK", "volume": "1000", "price": "10.00"},
            Decimal("10.00"),
        )
        strategy.update_stock_snapshot(
            {"symbol": "WEAK", "volume": "2000", "price": "10.00"},
            Decimal("10.00"),
        )
        # VWAP ~10.00, band 5% -> floor 9.50. Price 9.00 is well below.
        self.assertFalse(
            strategy.volatility_scalp_vwap_supports_entry("WEAK", Decimal("9.00"))
        )

    def test_allows_a_normal_dip_within_the_wider_band(self):
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_vwap_band_percent = Decimal("0.05")
        strategy.update_stock_snapshot(
            {"symbol": "OK", "volume": "1000", "price": "10.00"},
            Decimal("10.00"),
        )
        strategy.update_stock_snapshot(
            {"symbol": "OK", "volume": "2000", "price": "10.00"},
            Decimal("10.00"),
        )
        # 9.70 is only 3% below VWAP - within the 5% band, a normal
        # choppy-stock dip, not a real warning sign.
        self.assertTrue(
            strategy.volatility_scalp_vwap_supports_entry("OK", Decimal("9.70"))
        )

    def test_fails_open_with_no_vwap_data_yet(self):
        strategy = TradingStrategy(self.config())
        self.assertTrue(
            strategy.volatility_scalp_vwap_supports_entry(
                "NEVERSEEN", Decimal("10")
            )
        )


class VolatilityScalpEntrySpreadGateTests(StrategyConfigMixin, unittest.TestCase):
    """volatility_scalp_entry_spread_ok - by request, "make sure the
    algo plays around in the spread while ensuring a profit, or a
    profitable entry." Entries had no spread-quality check at all until
    now; reuses the exit side's own VOLATILITY_SCALP_MAX_EXIT_SPREAD_
    PERCENT bound for symmetry.
    """

    def test_blocks_an_absurdly_wide_spread(self):
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_max_exit_spread_percent = Decimal("8")
        strategy.metrics["WIDE"] = {"spread_percent": "15"}
        self.assertFalse(strategy.volatility_scalp_entry_spread_ok("WIDE"))

    def test_allows_a_spread_within_the_bound(self):
        strategy = TradingStrategy(self.config())
        strategy.config.volatility_scalp_max_exit_spread_percent = Decimal("8")
        strategy.metrics["OK"] = {"spread_percent": "5"}
        self.assertTrue(strategy.volatility_scalp_entry_spread_ok("OK"))

    def test_no_spread_data_yet_does_not_block(self):
        strategy = TradingStrategy(self.config())
        self.assertTrue(strategy.volatility_scalp_entry_spread_ok("NEVERSEEN"))


class DualThrustBreakoutSignalTests(StrategyConfigMixin, unittest.TestCase):
    """dual_thrust_breakout_signal - an opening-range-breakout style
    entry trigger adapted from the classic Dual Thrust strategy, OR'd
    alongside the existing dip signal as an additional way to trigger a
    fresh entry (the mirror case - buy a fresh push to a new high,
    instead of buying a pullback).
    """

    def _feed(self, strategy, symbol, prices):
        for price in prices:
            strategy.update_stock_snapshot(
                {"symbol": symbol, "volume": "1000", "price": str(price)},
                Decimal(str(price)),
            )

    def test_fires_once_price_breaks_above_the_recent_range_by_k_times_its_size(self):
        strategy = TradingStrategy(self.config())
        # Last 5 samples: 9.6, 10.4, 9.7, 10.3, 9.8 -> range_high=10.4,
        # range_low=9.6, range=0.8. K=0.5 -> upper_band = 10.4 + 0.4 = 10.8.
        self._feed(strategy, "WILD", [10, 10.5, 9.6, 10.4, 9.7, 10.3, 9.8])
        self.assertFalse(
            strategy.dual_thrust_breakout_signal("WILD", Decimal("10.7"))
        )
        self.assertTrue(
            strategy.dual_thrust_breakout_signal("WILD", Decimal("10.9"))
        )

    def test_no_history_never_fires(self):
        strategy = TradingStrategy(self.config())
        self.assertFalse(
            strategy.dual_thrust_breakout_signal("NEVERSEEN", Decimal("10"))
        )

    def test_a_flat_range_never_fires(self):
        strategy = TradingStrategy(self.config())
        self._feed(strategy, "FLAT", [10, 10, 10, 10, 10, 10])
        self.assertFalse(
            strategy.dual_thrust_breakout_signal("FLAT", Decimal("10.5"))
        )


class HeikinAshiReversalSignalTests(StrategyConfigMixin, unittest.TestCase):
    """heikin_ashi_bullish_reversal_signal - a confirmed HA bullish
    reversal (red bar immediately followed by a green one with little/no
    lower wick) built from synthetic OHLC bars bucketed off the same
    rolling tick-price window, OR'd alongside the dip and breakout
    signals as a third independent entry trigger.
    """

    def _feed(self, strategy, symbol, prices):
        for price in prices:
            strategy.update_stock_snapshot(
                {"symbol": symbol, "volume": "1000", "price": str(price)},
                Decimal(str(price)),
            )

    def test_not_enough_history_never_fires(self):
        strategy = TradingStrategy(self.config())
        self._feed(strategy, "THIN", [10, 10.1, 9.9])
        self.assertFalse(strategy.heikin_ashi_bullish_reversal_signal("THIN"))

    def test_fires_on_a_confirmed_bullish_reversal(self):
        strategy = TradingStrategy(self.config())
        # 6 bars of 5 samples each: a steady decline (bars 1-5, each bar
        # closing lower than it opened) followed by one sharp, clean
        # rally (bar 6, closing well above its open with almost no
        # pullback) - a textbook red-then-green HA reversal.
        prices = []
        base = 10.0
        for _ in range(5):
            prices.extend([base, base - 0.02, base - 0.05, base - 0.08, base - 0.10])
            base -= 0.10
        prices.extend([base, base + 0.05, base + 0.15, base + 0.30, base + 0.50])
        self._feed(strategy, "REV", prices)
        self.assertTrue(strategy.heikin_ashi_bullish_reversal_signal("REV"))

    def test_a_continuing_decline_does_not_fire(self):
        strategy = TradingStrategy(self.config())
        prices = []
        base = 10.0
        for _ in range(6):
            prices.extend([base, base - 0.02, base - 0.05, base - 0.08, base - 0.10])
            base -= 0.10
        self._feed(strategy, "DOWN", prices)
        self.assertFalse(strategy.heikin_ashi_bullish_reversal_signal("DOWN"))


class ParabolicSarExitSignalTests(StrategyConfigMixin, unittest.TestCase):
    """parabolic_sar_exit_signal - an additional exit trigger for a held
    volatility-scalp position (alongside, not instead of, the existing
    quick profit target): fires once the trailing SAR level flips
    bearish over the synthetic bar series.
    """

    def _feed(self, strategy, symbol, prices):
        for price in prices:
            strategy.update_stock_snapshot(
                {"symbol": symbol, "volume": "1000", "price": str(price)},
                Decimal(str(price)),
            )

    def test_not_enough_history_never_fires(self):
        strategy = TradingStrategy(self.config())
        self._feed(strategy, "THIN", [10, 10.1, 9.9])
        self.assertFalse(
            strategy.parabolic_sar_exit_signal("THIN", Decimal("10"))
        )

    def test_fires_once_an_uptrend_reverses(self):
        strategy = TradingStrategy(self.config())
        # A steady 6-bar rally followed by a sharp reversal bar - SAR
        # should flip bearish once the reversal bar's low breaks the
        # trailing stop built up during the rally.
        prices = []
        base = 10.0
        for _ in range(6):
            prices.extend([base, base + 0.05, base + 0.02, base + 0.08, base + 0.10])
            base += 0.10
        prices.extend([base, base - 0.20, base - 0.40, base - 0.60, base - 0.80])
        self._feed(strategy, "TREND", prices)
        self.assertTrue(
            strategy.parabolic_sar_exit_signal("TREND", Decimal(str(base - 0.80)))
        )

    def test_a_continuing_uptrend_does_not_fire(self):
        strategy = TradingStrategy(self.config())
        prices = []
        base = 10.0
        for _ in range(6):
            prices.extend([base, base + 0.05, base + 0.02, base + 0.08, base + 0.10])
            base += 0.10
        self._feed(strategy, "TREND", prices)
        self.assertFalse(
            strategy.parabolic_sar_exit_signal("TREND", Decimal(str(base + 0.10)))
        )


class VolatilityScalpMomentumGateTests(StrategyConfigMixin, unittest.TestCase):
    """volatility_scalp_momentum_stalled_or_rising (entry side) and
    volatility_scalp_momentum_stalling (exit side) - by request: "we
    don't want to buy when there is downward momentum... buy when the
    dip is stalled or at the bottom, or even when the momentum starts
    to go up" and "if there is a profit and it doesn't seem to be going
    much higher, then sell it off... before the next dip." The entry
    gate requires a genuine two-step decline immediately beforehand,
    then fires on the very first tick that stops declining ("almost as
    the rise starts"); the exit gate requires two non-declining ticks.
    """

    def _feed(self, strategy, symbol, prices):
        for price in prices:
            strategy.update_stock_snapshot(
                {"symbol": symbol, "volume": "1000", "price": str(price)},
                Decimal(str(price)),
            )

    def test_entry_gate_blocks_while_still_falling(self):
        strategy = TradingStrategy(self.config())
        self._feed(strategy, "FALL", [10, 9.9, 9.8, 9.7])
        self.assertFalse(
            strategy.volatility_scalp_momentum_stalled_or_rising(
                "FALL", Decimal("9.6")
            )
        )

    def test_entry_gate_fires_the_instant_the_turn_happens(self):
        """By request: "the momentum stop being negative after
        consecutive downticks, then we buy, almost as the rise
        starts." A genuine two-step decline (10 -> 9.9 -> 9.75) just
        happened, and the very next tick merely stops falling (9.8,
        just above the last low) - fires immediately, not several
        ticks later.
        """
        strategy = TradingStrategy(self.config())
        self._feed(strategy, "TURN", [10, 9.9, 9.75])
        self.assertTrue(
            strategy.volatility_scalp_momentum_stalled_or_rising(
                "TURN", Decimal("9.8")
            )
        )

    def test_entry_gate_allows_once_momentum_turns_up(self):
        strategy = TradingStrategy(self.config())
        # Same genuine two-step decline, this time querying a clearer
        # rise (9.75) rather than just a flat stall.
        self._feed(strategy, "RISE", [10, 9.9, 9.7])
        self.assertTrue(
            strategy.volatility_scalp_momentum_stalled_or_rising(
                "RISE", Decimal("9.75")
            )
        )

    def test_entry_gate_blocks_without_any_net_decline(self):
        """By request: momentum must have actually shown a real net
        decline before the turn counts - flat/rising prices with no
        real dip is not "the rise starting after a dip," it's just
        noise, even though the current tick alone isn't declining.

        Recalibrated by request, after live evidence ("it is not
        averaging down at all"): this used to require EVERY
        intermediate step to be strictly decreasing, so a flat lead-in
        tick (10, 10, 9.9) blocked here too - live evidence showed that
        was too strict for real market data (one flat/noise tick
        anywhere in the window failed the whole check almost
        continuously). Now only a genuine net decline is required
        (tolerating an intermediate wobble within a real decline), so
        this test uses a window with NO net decline at all (10, 10,
        10.1) to still exercise the "no real dip happened" block.
        """
        strategy = TradingStrategy(self.config())
        self._feed(strategy, "FLATLEAD", [10, 10, 10.1])
        self.assertFalse(
            strategy.volatility_scalp_momentum_stalled_or_rising(
                "FLATLEAD", Decimal("10.2")
            )
        )

    def test_entry_gate_allows_a_decline_with_one_intermediate_wobble(self):
        """By request, after live evidence ("it is not averaging down
        at all"): a single flat/up tick within an otherwise real
        decline must not fail the whole "was this a genuine dip" check
        anymore - net decline across the window is what matters, not a
        strict staircase every single step.
        """
        strategy = TradingStrategy(self.config())
        # 10 -> 9.95 (up-wobble) -> 9.8: net decline (10 -> 9.8) despite
        # the middle tick not being lower than the first.
        self._feed(strategy, "WOBBLE", [10, 9.95, 9.8])
        self.assertTrue(
            strategy.volatility_scalp_momentum_stalled_or_rising(
                "WOBBLE", Decimal("9.85")
            )
        )

    def test_entry_gate_fails_open_with_no_history(self):
        strategy = TradingStrategy(self.config())
        self.assertTrue(
            strategy.volatility_scalp_momentum_stalled_or_rising(
                "NEVERSEEN", Decimal("10")
            )
        )

    def test_exit_gate_fires_once_upward_momentum_stalls(self):
        strategy = TradingStrategy(self.config())
        # THREE consecutive equal ticks (10.3, 10.3, 10.3) - upward
        # momentum has stopped making fresh highs for two ticks running
        # now (the stronger, recalibrated confirmation - a single flat
        # tick alone is no longer enough).
        self._feed(strategy, "RISE", [10, 10.1, 10.2, 10.3, 10.3, 10.3])
        self.assertTrue(
            strategy.volatility_scalp_momentum_stalling("RISE", Decimal("10.3"))
        )

    def test_exit_gate_does_not_fire_on_a_single_flat_tick(self):
        """Recalibrated by request - "too trigger happy to sell... not
        capturing the profits when it can" - a single flat tick is
        normal noise, not a real stall anymore.
        """
        strategy = TradingStrategy(self.config())
        self._feed(strategy, "RISE", [10, 10.1, 10.2, 10.3, 10.3])
        self.assertFalse(
            strategy.volatility_scalp_momentum_stalling("RISE", Decimal("10.3"))
        )

    def test_exit_gate_does_not_fire_while_still_climbing(self):
        strategy = TradingStrategy(self.config())
        self._feed(strategy, "RISE", [10, 10.1, 10.2, 10.3])
        self.assertFalse(
            strategy.volatility_scalp_momentum_stalling("RISE", Decimal("10.4"))
        )

    def test_exit_gate_fails_closed_with_no_history(self):
        strategy = TradingStrategy(self.config())
        self.assertFalse(
            strategy.volatility_scalp_momentum_stalling(
                "NEVERSEEN", Decimal("10")
            )
        )


class VolatilityScalpExitOverrideMomentumStallTests(
    StrategyConfigMixin, unittest.TestCase
):
    """volatility_scalp_exit_override's fourth exit path: any real
    profit combined with stalling upward momentum sells immediately,
    ahead of the fixed quick target or a full SAR reversal - "sell it
    off before the next dip."
    """

    def _feed(self, strategy, symbol, prices):
        for price in prices:
            strategy.update_stock_snapshot(
                {"symbol": symbol, "volume": "1000", "price": str(price)},
                Decimal(str(price)),
            )

    def test_fires_once_most_of_the_target_is_covered_and_momentum_stalls(self):
        strategy = TradingStrategy(self.config())
        # cost=10.29, target = 10.29 * 1.005 = 10.34145 (under the mixin's
        # 0.5% target_percent). min_stall_price (60% of the way from
        # cost to target) = 10.29 + (10.34145-10.29)*0.6 = 10.3209 -
        # price=10.33 clears that but stays under the full target, and
        # ends with TWO consecutive equal ticks (the recalibrated,
        # stronger stall confirmation).
        self._feed(strategy, "RISE", [10.1, 10.2, 10.29, 10.32, 10.33, 10.33, 10.33])
        from webull_bot.strategy import Decision

        result = strategy.volatility_scalp_exit_override(
            Decision("HOLD", "between target and stop", Decimal("10.33")),
            quantity=100,
            average_cost=Decimal("10.29"),
            price=Decimal("10.33"),
            averaging_available=True,
            symbol="RISE",
        )
        self.assertEqual(result.action, "PROFIT")
        self.assertIn("momentum stalling", result.reason)

    def test_does_not_fire_on_a_tiny_profit_even_if_momentum_stalls(self):
        """Recalibrated by request - "too trigger happy to sell... not
        capturing the profits when it can" - a tiny profit (well under
        VOLATILITY_SCALP_MOMENTUM_STALL_MIN_PROFIT_FRACTION of the way
        to the real target) no longer triggers an early exit just
        because momentum stalled for a couple ticks.
        """
        strategy = TradingStrategy(self.config())
        # cost=10.29, target=10.34145, min_stall_price=10.3209 - price
        #=10.30 is a real profit but well under that fraction.
        self._feed(strategy, "RISE", [10.1, 10.2, 10.29, 10.30, 10.30, 10.30])
        from webull_bot.strategy import Decision

        held = Decision("HOLD", "between target and stop", Decimal("10.30"))
        result = strategy.volatility_scalp_exit_override(
            held,
            quantity=100,
            average_cost=Decimal("10.29"),
            price=Decimal("10.30"),
            averaging_available=True,
            symbol="RISE",
        )
        self.assertIs(result, held)

    def test_never_fires_at_exactly_cost_no_real_profit(self):
        strategy = TradingStrategy(self.config())
        self._feed(strategy, "RISE", [10.1, 10.2, 10.3, 10.3])
        from webull_bot.strategy import Decision

        held = Decision("HOLD", "between target and stop", Decimal("10.3"))
        result = strategy.volatility_scalp_exit_override(
            held,
            quantity=100,
            average_cost=Decimal("10.30"),
            price=Decimal("10.3"),
            averaging_available=True,
            symbol="RISE",
        )
        self.assertIs(result, held)

    def test_does_not_fire_while_still_climbing(self):
        strategy = TradingStrategy(self.config())
        # cost=10.38 keeps this under the 0.5% quick target
        # (10.38 * 1.005 = 10.4319) and price=10.40 is still a fresh
        # high above the prior tick (10.35) - genuinely still climbing,
        # not stalled.
        self._feed(strategy, "RISE", [10.1, 10.2, 10.3, 10.35])
        from webull_bot.strategy import Decision

        held = Decision("HOLD", "between target and stop", Decimal("10.40"))
        result = strategy.volatility_scalp_exit_override(
            held,
            quantity=100,
            average_cost=Decimal("10.38"),
            price=Decimal("10.40"),
            averaging_available=True,
            symbol="RISE",
        )
        self.assertIs(result, held)


class VolatilityScalpExitOverrideSarTests(StrategyConfigMixin, unittest.TestCase):
    """volatility_scalp_exit_override's third exit path: a Parabolic SAR
    trend reversal, but only once price has at least cleared cost - it
    locks in a reversal early, without ever becoming a second, backdoor
    stop-loss (the LOSS suppression above deliberately disables the real
    one for this cohort).
    """

    def _feed(self, strategy, symbol, prices):
        for price in prices:
            strategy.update_stock_snapshot(
                {"symbol": symbol, "volume": "1000", "price": str(price)},
                Decimal(str(price)),
            )

    def _reversed_series(self, strategy, symbol):
        prices = []
        base = 10.0
        for _ in range(6):
            prices.extend([base, base + 0.05, base + 0.02, base + 0.08, base + 0.10])
            base += 0.10
        prices.extend([base, base - 0.20, base - 0.40, base - 0.60, base - 0.80])
        self._feed(strategy, symbol, prices)
        return Decimal(str(base - 0.80))

    def test_sar_reversal_triggers_profit_once_price_clears_cost(self):
        strategy = TradingStrategy(self.config())
        price = self._reversed_series(strategy, "TREND")
        from webull_bot.strategy import Decision

        result = strategy.volatility_scalp_exit_override(
            Decision("HOLD", "between target and stop", price),
            quantity=100,
            average_cost=price - Decimal("0.01"),
            price=price,
            averaging_available=True,
            symbol="TREND",
        )
        self.assertEqual(result.action, "PROFIT")
        self.assertIn("parabolic SAR", result.reason)

    def test_sar_reversal_never_fires_below_cost(self):
        """A SAR flip must never act as a backdoor stop-loss - this
        cohort's real stop-loss is deliberately suppressed while
        averaging is available, and SAR isn't meant to reintroduce it
        by another name.
        """
        strategy = TradingStrategy(self.config())
        price = self._reversed_series(strategy, "TREND")
        from webull_bot.strategy import Decision

        held = Decision("HOLD", "between target and stop", price)
        result = strategy.volatility_scalp_exit_override(
            held,
            quantity=100,
            average_cost=price + Decimal("1"),
            price=price,
            averaging_available=True,
            symbol="TREND",
        )
        self.assertIs(result, held)

    def test_no_symbol_passed_skips_the_sar_check_entirely(self):
        strategy = TradingStrategy(self.config())
        from webull_bot.strategy import Decision

        # cost just barely below price - well under the quick target
        # (0.5% default), so PROFIT can only come from a SAR check that
        # (with no symbol/history available) must never fire.
        held = Decision("HOLD", "between target and stop", Decimal("10"))
        result = strategy.volatility_scalp_exit_override(
            held,
            quantity=100,
            average_cost=Decimal("9.999"),
            price=Decimal("10"),
            averaging_available=True,
        )
        self.assertIs(result, held)


class VolatilityScalpReentryCooldownTests(unittest.TestCase):
    def test_ready_when_never_exited(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            last_exit_at={},
            config=SimpleNamespace(volatility_scalp_reentry_cooldown_seconds=5),
        )
        ready = AutoTrader.volatility_scalp_reentry_ready.__get__(fake_bot)
        self.assertTrue(ready("STOCK:WILD"))

    def test_blocked_immediately_after_an_exit(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            last_exit_at={"STOCK:WILD": time.monotonic()},
            config=SimpleNamespace(volatility_scalp_reentry_cooldown_seconds=5),
        )
        ready = AutoTrader.volatility_scalp_reentry_ready.__get__(fake_bot)
        self.assertFalse(ready("STOCK:WILD"))

    def test_ready_again_once_the_short_cooldown_elapses(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            last_exit_at={"STOCK:WILD": time.monotonic() - 10},
            config=SimpleNamespace(volatility_scalp_reentry_cooldown_seconds=5),
        )
        ready = AutoTrader.volatility_scalp_reentry_ready.__get__(fake_bot)
        self.assertTrue(ready("STOCK:WILD"))


class VolatilityScalpEntryPriceTests(unittest.TestCase):
    """volatility_scalp_entry_price - by request, "a lot of the orders
    are being cancelled... ensure the initial order itself is likely to
    be filled." Crosses at the (tick-quantized) ask instead of the
    passive bid/ask midpoint every other entry uses, guaranteeing a
    real chance to fill immediately instead of sitting for the full
    ORDER_TIMEOUT_SECONDS waiting for the market to fall back to a
    passive mid-price.
    """

    @staticmethod
    def _fake_bot():
        from webull_bot.bot import AutoTrader
        from webull_bot.webull_api import WebullAPI

        fake_bot = SimpleNamespace(
            api=SimpleNamespace(
                quote_ask=lambda q: (
                    Decimal(str(q["ask"])) if q.get("ask") else None
                ),
                price_tick_size=WebullAPI.price_tick_size,
            )
        )
        return AutoTrader.volatility_scalp_entry_price.__get__(fake_bot)

    def test_crosses_at_the_ask_for_a_dollar_plus_stock(self):
        entry_price = self._fake_bot()
        result = entry_price({"bid": "9.90", "ask": "10.05"})
        self.assertEqual(result, Decimal("10.05"))

    def test_crosses_at_the_ask_with_sub_penny_precision_under_a_dollar(self):
        entry_price = self._fake_bot()
        result = entry_price({"bid": "0.4590", "ask": "0.4600"})
        self.assertEqual(result, Decimal("0.4600"))

    def test_returns_none_with_no_valid_ask(self):
        entry_price = self._fake_bot()
        self.assertIsNone(entry_price({"bid": "9.90", "ask": None}))


class VolatilityScalpPositionValueCapTests(unittest.TestCase):
    """Live incident: GAUZ alone grew to ~66% of a small account's total
    value. volatility_scalp_position_value_ok caps any single cohort
    symbol's total position value (existing + a prospective new buy) to
    VOLATILITY_SCALP_MAX_POSITION_FRACTION of total account value.
    """

    @staticmethod
    def _fake_bot(account_value, max_fraction=Decimal("0.35")):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            cached_account_value=account_value,
            config=SimpleNamespace(volatility_scalp_max_position_fraction=max_fraction),
        )
        return AutoTrader.volatility_scalp_position_value_ok.__get__(fake_bot)

    def test_blocks_a_buy_that_would_exceed_the_fraction_of_account_value(self):
        check = self._fake_bot(account_value=Decimal("200"))
        # 300 shares * 0.44 = 132, well over 35% of 200 (=70).
        self.assertFalse(check(200, 100, Decimal("0.44")))

    def test_allows_a_buy_that_stays_within_the_fraction(self):
        check = self._fake_bot(account_value=Decimal("200"))
        # 100 shares * 0.44 = 44, under 35% of 200 (=70).
        self.assertTrue(check(0, 100, Decimal("0.44")))

    def test_fails_open_when_account_value_is_unknown(self):
        check = self._fake_bot(account_value=None)
        self.assertTrue(check(1000, 1000, Decimal("100")))

    def test_fails_open_when_account_value_is_non_positive(self):
        check = self._fake_bot(account_value=Decimal("0"))
        self.assertTrue(check(1000, 1000, Decimal("100")))

    def test_considers_the_existing_position_value_too(self):
        """An averaging-down buy must account for what's already held,
        not just the new clip - the cap is on the TOTAL resulting
        position, not each individual buy in isolation.
        """
        check = self._fake_bot(account_value=Decimal("200"))
        # Already holding 100 shares (44 worth) - adding another 100
        # (another 44) totals 88, over 35% of 200 (=70).
        self.assertFalse(check(100, 100, Decimal("0.44")))


class VolatilityScalpTotalExposureCapTests(unittest.TestCase):
    """Per-symbol caps alone don't bound worst case: several cohort
    symbols could each individually satisfy
    volatility_scalp_max_position_fraction while the account as a whole
    is almost entirely concentrated in the cohort during a correlated
    selloff. volatility_scalp_total_exposure_ok caps the WHOLE cohort's
    combined value instead.
    """

    @staticmethod
    def _fake_bot(account_value, symbols, max_fraction=Decimal("0.60")):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            cached_account_value=account_value,
            volatility_scalp_positions=set(symbols),
            config=SimpleNamespace(
                volatility_scalp_max_total_exposure_fraction=max_fraction
            ),
        )
        return AutoTrader.volatility_scalp_total_exposure_ok.__get__(fake_bot)

    def test_blocks_when_existing_cohort_positions_plus_the_new_buy_exceed_the_cap(
        self,
    ):
        check = self._fake_bot(
            account_value=Decimal("200"), symbols={"AAA", "BBB", "CCC"}
        )
        positions = [
            {"symbol": "AAA", "quantity": "100", "cost_price": "0.50"},
            {"symbol": "BBB", "quantity": "100", "cost_price": "0.50"},
        ]
        # 50 + 50 already held; a 20 more pushes total to 120, over 60% of 200 (=120 is exactly the boundary)
        self.assertFalse(check(positions, Decimal("20.01")))

    def test_allows_when_combined_cohort_value_stays_within_the_cap(self):
        check = self._fake_bot(
            account_value=Decimal("200"), symbols={"AAA", "BBB", "CCC"}
        )
        positions = [
            {"symbol": "AAA", "quantity": "100", "cost_price": "0.50"},
        ]
        # 50 already held; adding 20 totals 70, under 60% of 200 (=120).
        self.assertTrue(check(positions, Decimal("20")))

    def test_ignores_positions_outside_the_cohort(self):
        check = self._fake_bot(account_value=Decimal("200"), symbols={"AAA"})
        positions = [
            {"symbol": "NOTSCALP", "quantity": "1000", "cost_price": "50"},
        ]
        self.assertTrue(check(positions, Decimal("20")))

    def test_fails_open_when_account_value_is_unknown(self):
        check = self._fake_bot(account_value=None, symbols={"AAA"})
        self.assertTrue(check([], Decimal("1000")))

    def test_fails_open_when_account_value_is_non_positive(self):
        check = self._fake_bot(account_value=Decimal("0"), symbols={"AAA"})
        self.assertTrue(check([], Decimal("1000")))


class VolatilityScalpBarSeedTests(unittest.TestCase):
    """Warm-starts a symbol's volatility window from real M1 bars instead
    of waiting on several live snapshot polls - see
    AutoTrader.seed_volatility_windows and WebullAPI.recent_minute_closes.
    """

    def test_seeds_only_symbols_with_an_empty_window(self):
        from webull_bot.bot import AutoTrader

        requested = []

        class FakeApi:
            def recent_minute_closes(self, symbols, category, count):
                requested.append((tuple(sorted(symbols)), category, count))
                return {s: [10.0, 10.1, 9.9] for s in symbols}

        seeded = []
        fake_bot = SimpleNamespace(
            api=FakeApi(),
            stock_categories={"AAA": "US_STOCK", "BBB": "US_ETF"},
            config=SimpleNamespace(volatility_scalp_lookback_samples=20),
            strategy=SimpleNamespace(
                volatility_price_history={"BBB": [5.0]},
                seed_volatility_window=lambda symbol, closes: seeded.append(
                    (symbol, closes)
                ),
                prices={},
            ),
        )
        seed = AutoTrader.seed_volatility_windows.__get__(fake_bot)
        seed(["AAA", "BBB"])
        # BBB already has a window - only AAA should ever be fetched/seeded.
        self.assertEqual(requested, [(("AAA",), "US_STOCK", 20)])
        self.assertEqual(seeded, [("AAA", [10.0, 10.1, 9.9])])
        # Also seeds self.strategy.prices, so a bar-seeded (but not yet
        # live-scanned) symbol is already visible to cohort selection.
        self.assertEqual(fake_bot.strategy.prices, {"AAA": Decimal("9.9")})

    def test_never_overwrites_an_already_live_price(self):
        from webull_bot.bot import AutoTrader

        class FakeApi:
            def recent_minute_closes(self, symbols, category, count):
                return {s: [10.0, 10.1, 9.9] for s in symbols}

        fake_bot = SimpleNamespace(
            api=FakeApi(),
            stock_categories={"AAA": "US_STOCK"},
            config=SimpleNamespace(volatility_scalp_lookback_samples=20),
            strategy=SimpleNamespace(
                volatility_price_history={},
                seed_volatility_window=lambda *a, **k: None,
                # Already has a live-scanned price for AAA - the stale
                # bar close (9.9) must not clobber it.
                prices={"AAA": Decimal("11.25")},
            ),
        )
        seed = AutoTrader.seed_volatility_windows.__get__(fake_bot)
        seed(["AAA"])
        self.assertEqual(fake_bot.strategy.prices, {"AAA": Decimal("11.25")})

    def test_noop_when_everything_is_already_seeded(self):
        from webull_bot.bot import AutoTrader

        class FakeApi:
            def recent_minute_closes(self, *a, **k):
                raise AssertionError("must not fetch bars for an already-seeded symbol")

        fake_bot = SimpleNamespace(
            api=FakeApi(),
            stock_categories={},
            config=SimpleNamespace(volatility_scalp_lookback_samples=20),
            strategy=SimpleNamespace(
                volatility_price_history={"AAA": [10.0]},
                seed_volatility_window=lambda *a, **k: None,
            ),
        )
        seed = AutoTrader.seed_volatility_windows.__get__(fake_bot)
        seed(["AAA"])  # must not raise

    def test_a_failed_category_fetch_does_not_block_other_categories(self):
        from webull_bot.bot import AutoTrader

        class FakeApi:
            def recent_minute_closes(self, symbols, category, count):
                if category == "US_STOCK":
                    raise RuntimeError("boom")
                return {s: [1.0, 1.1] for s in symbols}

        seeded = []
        fake_bot = SimpleNamespace(
            api=FakeApi(),
            stock_categories={"AAA": "US_STOCK", "BBB": "US_ETF"},
            config=SimpleNamespace(volatility_scalp_lookback_samples=20),
            strategy=SimpleNamespace(
                volatility_price_history={},
                seed_volatility_window=lambda symbol, closes: seeded.append(
                    (symbol, closes)
                ),
                prices={},
            ),
        )
        seed = AutoTrader.seed_volatility_windows.__get__(fake_bot)
        with self.assertLogs("webull-bot", level="WARNING"):
            seed(["AAA", "BBB"])
        self.assertEqual(seeded, [("BBB", [1.0, 1.1])])


class VolatilityScalpCohortSelectionTests(unittest.TestCase):
    """select_volatility_scalp_symbols - the daily curated cohort (a
    handful of the cheapest, most volatile names), re-ranked
    periodically from data already collected during normal scanning.
    """

    @staticmethod
    def _fake_bot(prices, stdev_by_symbol, **config_overrides):
        from webull_bot.bot import AutoTrader

        config = dict(
            volatility_scalp_enabled=True,
            volatility_scalp_symbol_count=3,
            volatility_scalp_max_price=Decimal("1.50"),
            volatility_scalp_reselect_seconds=1800,
        )
        config.update(config_overrides)
        fake_bot = SimpleNamespace(
            config=SimpleNamespace(**config),
            last_volatility_symbol_selection=float("-inf"),
            volatility_scalp_symbols=set(),
            volatility_scalp_positions=set(),
            strategy=SimpleNamespace(
                prices=prices,
                realized_volatility_percent=lambda symbol: stdev_by_symbol.get(symbol),
            ),
        )
        fake_bot.select_volatility_scalp_symbols = (
            AutoTrader.select_volatility_scalp_symbols.__get__(fake_bot)
        )
        return fake_bot

    def test_picks_the_top_n_by_volatility_among_symbols_under_the_price_cap(self):
        prices = {
            "WILD": Decimal("0.89"),
            "CALM": Decimal("0.50"),
            "PRICEY": Decimal("50.00"),  # over the cap - excluded regardless of stdev
            "MID": Decimal("1.20"),
            "OTHER": Decimal("0.30"),
        }
        stdev = {
            "WILD": Decimal("0.05"),
            "CALM": Decimal("0.001"),
            "PRICEY": Decimal("0.20"),
            "MID": Decimal("0.03"),
            "OTHER": Decimal("0.02"),
        }
        fake_bot = self._fake_bot(prices, stdev)
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            fake_bot.select_volatility_scalp_symbols()
        self.assertEqual(fake_bot.volatility_scalp_symbols, {"WILD", "MID", "OTHER"})

    def test_excludes_symbols_with_no_volatility_reading_yet(self):
        prices = {"NEW": Decimal("0.80"), "WILD": Decimal("0.89")}
        stdev = {"WILD": Decimal("0.05")}  # NEW has no reading yet
        fake_bot = self._fake_bot(prices, stdev)
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            fake_bot.select_volatility_scalp_symbols()
        self.assertEqual(fake_bot.volatility_scalp_symbols, {"WILD"})

    def test_disabled_in_config_never_selects_anything(self):
        prices = {"WILD": Decimal("0.89")}
        stdev = {"WILD": Decimal("0.05")}
        fake_bot = self._fake_bot(prices, stdev, volatility_scalp_enabled=False)
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            fake_bot.select_volatility_scalp_symbols()
        self.assertEqual(fake_bot.volatility_scalp_symbols, set())

    def test_respects_its_own_reselect_throttle(self):
        prices = {"WILD": Decimal("0.89")}
        stdev = {"WILD": Decimal("0.05")}
        fake_bot = self._fake_bot(prices, stdev)
        fake_bot.last_volatility_symbol_selection = 99.0
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            fake_bot.select_volatility_scalp_symbols()
        # Still 30 minutes (default) away from the next reselect - the
        # cohort should stay untouched (empty, since it started empty).
        self.assertEqual(fake_bot.volatility_scalp_symbols, set())

    def test_an_empty_result_does_not_consume_the_throttle(self):
        """Live incident: the very first call on every startup runs
        before any symbol has been scanned (self.strategy.prices is
        still empty), finds zero candidates, but used to stamp last_
        volatility_symbol_selection anyway - "spending" the throttle on
        a result with no real data behind it and leaving the cohort
        empty for the full 30-minute reselect window before ever trying
        again. An empty-candidates call must leave the throttle
        untouched so the very next cycle (once real data exists) can
        actually select something.
        """
        fake_bot = self._fake_bot({}, {})  # nothing scanned yet
        with unittest.mock.patch("time.monotonic", return_value=0.001):
            fake_bot.select_volatility_scalp_symbols()
        self.assertEqual(fake_bot.last_volatility_symbol_selection, float("-inf"))

        # Now real data exists - must not be throttled out just because
        # almost no time has passed since the (empty) first call.
        fake_bot.strategy.prices = {"WILD": Decimal("0.89")}
        fake_bot.strategy.realized_volatility_percent = lambda symbol: Decimal("0.05")
        with unittest.mock.patch("time.monotonic", return_value=0.002):
            fake_bot.select_volatility_scalp_symbols()
        self.assertEqual(fake_bot.volatility_scalp_symbols, {"WILD"})

    def test_reselecting_can_drop_a_cooled_off_symbol_and_add_a_new_one(self):
        prices = {"OLD": Decimal("0.50"), "NEW": Decimal("0.80")}
        fake_bot = self._fake_bot(
            prices, {"OLD": Decimal("0.05")}, volatility_scalp_symbol_count=1
        )
        with unittest.mock.patch("time.monotonic", return_value=100.0):
            fake_bot.select_volatility_scalp_symbols()
        self.assertEqual(fake_bot.volatility_scalp_symbols, {"OLD"})

        # OLD cools off, NEW heats up, and enough time passes for reselection.
        fake_bot.strategy.realized_volatility_percent = lambda symbol: {
            "OLD": Decimal("0.001"),
            "NEW": Decimal("0.08"),
        }.get(symbol)
        with unittest.mock.patch("time.monotonic", return_value=2000.0):
            fake_bot.select_volatility_scalp_symbols()
        self.assertEqual(fake_bot.volatility_scalp_symbols, {"NEW"})
