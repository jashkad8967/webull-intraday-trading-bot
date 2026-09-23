import time
import unittest
import unittest.mock
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from webull_bot.bot import AutoTrader
from webull_bot.strategy import TradingStrategy
from webull_bot.strategy_logic.decision.stock_option_decision import option_decision
from webull_bot.strategy_logic.regime.market_regime import option_delta_ok


def focus_config(**overrides):
    """Only the knobs the focus-mode code paths actually read."""
    base = dict(
        focus_mode_enabled=True,
        focus_lock_time="09:45",
        daily_batch_refresh_time="08:45",
        daily_batch_size=8,
        daily_batch_min_gap_percent=Decimal("2"),
        daily_batch_retry_minutes=20,
        daily_batch_require_established_symbols=True,
        option_candidates=lambda: ["AAPL", "MSFT", "MRNA", "AMD", "TSLA"],
        focus_min_price=Decimal("10"),
        focus_max_price=Decimal("600"),
        focus_repick_when_blocked=True,
        focus_cohort_size=10,
        focus_lock_discovery_per_pass=25,
        focus_consensus_fraction=Decimal("0.7"),
        focus_consensus_min_signals=4,
        focus_contract_discovery_max_failures=3,
        focus_daily_profit_target_fraction=Decimal("0.05"),
        profit_throttle_confirm_readings=3,
        popular_stock_min_volume=1_000_000,
        popular_stock_max_spread_percent=Decimal("0.50"),
        option_min_volatility_percent=Decimal("0.02"),
        profit_lock_enabled=True,
        profit_lock_arm_percent=Decimal("0.10"),
        profit_lock_giveback_fraction=Decimal("0.50"),
        profit_lock_min_gain_fraction=Decimal("0.5"),
        profit_lock_giveback_fraction_after_throttle=Decimal("0.25"),
        pressure_enabled=True,
        pressure_min_for_entry=Decimal("0.15"),
        pressure_flip_exit_enabled=True,
        pressure_flip_exit_threshold=Decimal("0.25"),
        pressure_history_seconds=300,
        sell_fee_dollars=Decimal("0.02"),
        option_sell_fee_per_contract=Decimal("0.07"),
        option_take_profit_percent=Decimal("0.15"),
        option_stop_loss_percent=Decimal("0.50"),
        option_min_dte=14,
        option_max_dte=45,
        option_type="BOTH",
        option_max_moneyness_percent=Decimal("0.15"),
        option_eod_close_time="15:50",
        option_min_hold_dte=7,
        option_max_positions_per_underlying=1,
        option_stale_exit_enabled=False,
        held_option_exit_enabled=False,
        option_time_aware_stop_enabled=False,
        held_option_exit_seconds=Decimal("2"),
        option_stale_exit_minutes=45,
        option_stale_exit_max_loss_percent=Decimal("0.08"),
        option_discovery_seconds=300,
        time_aware_stop_enabled=False,
        time_aware_stop_widen_seconds=60,
        time_aware_stop_widen_multiplier=Decimal("1.5"),
        volatility_scalp_micro_exhaustion_volume_ema_alpha=Decimal("0.2"),
        # Added for full _evaluate_option_entry/_evaluate_option_exit
        # integration coverage - see
        # tests/trading/test_focus_mode_entry_exit_integration.py.
        option_max_entry_spread_percent=Decimal("25"),
        option_smoke_test_mode=False,
        option_scalp_enabled=True,
        option_straddle_enabled=False,
        option_momentum_flip_window_seconds=180,
        option_min_premium_dollars=Decimal("0.50"),
        option_quantity=20,
        option_capital_fraction=Decimal("1.0"),
        max_order_notional=Decimal("1000"),
        max_open_positions=50,
        trade_cooldown_seconds=Decimal("0"),
        stock_reentry_cooldown_seconds=Decimal("180"),
        stock_max_trades_per_hour=0,
        price_sanity_cooldown_seconds=60,
        symbol_quarantine_enabled=False,
        symbol_quarantine_lookback_seconds=1800,
        symbol_quarantine_min_trades=3,
        symbol_quarantine_loss_dollars=Decimal("0.50"),
        symbol_quarantine_cooldown_seconds=900,
        option_averaging_down_dip_percent=Decimal("0.20"),
        option_averaging_step_multiplier=Decimal("0.5"),
        option_max_averaging_buys=2,
        option_averaging_reentry_cooldown_seconds=Decimal("60"),
    )
    base.update(overrides)
    cfg = SimpleNamespace(**base)
    # session_moment() calls through to this, same as the real
    # SessionScheduleSettings helper.
    cfg.session_time = lambda value: dt_time(
        *(int(part) for part in value.split(":"))
    )
    return cfg


class NetPressureTests(unittest.TestCase):
    """By request: "see the momentum by the buys and sells." The
    underlying volume feed is unsigned, so direction comes from price
    and conviction from volume - these pin both halves down.
    """

    def strategy(self, **overrides):
        strategy = SimpleNamespace(
            config=focus_config(**overrides),
            pressure_price_baseline={},
            pressure_history=defaultdict(lambda: deque(maxlen=50)),
            volume_delta_ema={},
            volume_delta_latest={},
        )
        strategy.update_net_pressure = (
            TradingStrategy.update_net_pressure.__get__(strategy)
        )
        strategy.net_pressure = TradingStrategy.net_pressure.__get__(strategy)
        strategy.pressure_supports_entry = (
            TradingStrategy.pressure_supports_entry.__get__(strategy)
        )
        strategy.pressure_flipped_against = (
            TradingStrategy.pressure_flipped_against.__get__(strategy)
        )
        return strategy

    def prime(self, strategy, symbol, first, second, ema, latest):
        """First call only seeds the baseline; the second produces a
        reading, mirroring update_volume_delta's own two-call warmup.
        """
        strategy.update_net_pressure(symbol, Decimal(first), time.monotonic())
        strategy.volume_delta_ema[symbol] = Decimal(ema)
        strategy.volume_delta_latest[symbol] = Decimal(latest)
        strategy.update_net_pressure(symbol, Decimal(second), time.monotonic())

    def test_price_up_on_a_volume_spike_reads_as_buyers_in_control(self):
        strategy = self.strategy()
        self.prime(strategy, "AAA", "10.00", "10.10", ema="100", latest="200")
        # ratio 2.0 -> conviction (2.0 - 1) clamped to 1.0, price up
        self.assertEqual(strategy.net_pressure("AAA"), Decimal("1"))

    def test_price_down_on_a_volume_spike_reads_as_sellers_in_control(self):
        strategy = self.strategy()
        self.prime(strategy, "AAA", "10.00", "9.90", ema="100", latest="200")
        self.assertEqual(strategy.net_pressure("AAA"), Decimal("-1"))

    def test_a_move_on_average_volume_carries_no_conviction(self):
        strategy = self.strategy()
        # ratio 1.0 -> conviction 0: the move has no participation
        # behind it no matter how large the price change looks.
        self.prime(strategy, "AAA", "10.00", "11.00", ema="100", latest="100")
        self.assertEqual(strategy.net_pressure("AAA"), Decimal("0"))

    def test_partial_conviction_scales_between_average_and_double(self):
        strategy = self.strategy()
        self.prime(strategy, "AAA", "10.00", "10.10", ema="100", latest="150")
        self.assertEqual(strategy.net_pressure("AAA"), Decimal("0.5"))

    def test_first_sample_produces_no_reading(self):
        strategy = self.strategy()
        strategy.volume_delta_ema["AAA"] = Decimal("100")
        strategy.volume_delta_latest["AAA"] = Decimal("200")
        strategy.update_net_pressure("AAA", Decimal("10"), time.monotonic())
        self.assertIsNone(strategy.net_pressure("AAA"))

    def test_missing_volume_data_produces_no_reading(self):
        strategy = self.strategy()
        strategy.update_net_pressure("AAA", Decimal("10.00"), time.monotonic())
        strategy.update_net_pressure("AAA", Decimal("10.10"), time.monotonic())
        self.assertIsNone(strategy.net_pressure("AAA"))

    def test_a_stale_reading_is_discarded_rather_than_returned(self):
        strategy = self.strategy(pressure_history_seconds=30)
        strategy.pressure_history["AAA"].append(
            (time.monotonic() - 600, Decimal("1"))
        )
        self.assertIsNone(strategy.net_pressure("AAA"))

    def test_entry_gate_requires_buyers_for_a_call_and_sellers_for_a_put(self):
        strategy = self.strategy()
        self.prime(strategy, "AAA", "10.00", "10.10", ema="100", latest="200")
        self.assertTrue(strategy.pressure_supports_entry("AAA", "CALL"))
        self.assertFalse(strategy.pressure_supports_entry("AAA", "PUT"))

    def test_entry_gate_fails_open_without_data(self):
        strategy = self.strategy()
        self.assertTrue(strategy.pressure_supports_entry("AAA", "CALL"))
        self.assertTrue(strategy.pressure_supports_entry("AAA", "PUT"))

    def test_average_volume_does_not_veto_either_direction(self):
        """The live 2026-09-22 blocker, in one assertion.

        conviction = clamp(latest/ema - 1, 0, 1) and ema is an EMA of
        that same series, so the ratio mean-reverts to 1.0 and
        conviction sits at EXACTLY zero most of the time. The gate used
        to demand pressure >= +minimum to buy a call, which meant
        demanding a volume spike in the same instant as the direction
        signal. A zero reading is also a real Decimal, not None, so the
        "no fresh reading -> fail open" convention never triggered and
        the gate blocked on ABSENCE of evidence.

        Measured: a cohort of 10 with signals reading CALL=2 PUT=6
        HOLD=2 produced "buy/sell pressure does not support this
        direction=19" every cycle, at 0.15 AND at 0.05.
        """
        strategy = self.strategy()
        # ratio exactly 1.0 -> conviction 0 -> pressure 0
        self.prime(strategy, "AAA", "10.00", "10.10", ema="100", latest="100")
        self.assertEqual(strategy.net_pressure("AAA"), Decimal("0"))
        self.assertTrue(
            strategy.pressure_supports_entry("AAA", "CALL"),
            "no participation evidence must not veto a call",
        )
        self.assertTrue(
            strategy.pressure_supports_entry("AAA", "PUT"),
            "no participation evidence must not veto a put",
        )

    def test_real_selling_still_vetoes_a_call(self):
        """What the gate is actually for - "a dip that nobody is
        buying is not a dip worth buying a call into". Sellers
        demonstrably in control, on real volume, must still block.
        """
        strategy = self.strategy()
        self.prime(strategy, "AAA", "10.00", "9.90", ema="100", latest="200")
        self.assertEqual(strategy.net_pressure("AAA"), Decimal("-1"))
        self.assertFalse(strategy.pressure_supports_entry("AAA", "CALL"))
        self.assertTrue(strategy.pressure_supports_entry("AAA", "PUT"))

    def test_weak_participation_does_not_veto_the_side_it_favours(self):
        strategy = self.strategy()
        # ratio 1.02 -> conviction 0.02, below the 0.15 fixture bar but
        # pointing the right way for a call.
        self.prime(strategy, "AAA", "10.00", "10.10", ema="100", latest="102")
        pressure = strategy.net_pressure("AAA")
        self.assertGreater(pressure, Decimal("0"))
        self.assertLess(pressure, strategy.config.pressure_min_for_entry)
        self.assertTrue(
            strategy.pressure_supports_entry("AAA", "CALL"),
            "weak buying must not block a call - it agrees with it",
        )

    def test_exit_signal_fires_when_pressure_turns_against_the_position(self):
        strategy = self.strategy()
        self.prime(strategy, "AAA", "10.00", "9.90", ema="100", latest="200")
        # Sellers now dominate: bad for a held CALL, fine for a PUT.
        self.assertTrue(strategy.pressure_flipped_against("AAA", "CALL"))
        self.assertFalse(strategy.pressure_flipped_against("AAA", "PUT"))

    def test_exit_signal_fails_closed_without_data(self):
        strategy = self.strategy()
        # Deliberately the opposite convention to the entry gate - an
        # exit trigger that fired on missing data would close real
        # positions on no evidence at all.
        self.assertFalse(strategy.pressure_flipped_against("AAA", "CALL"))


class ProfitLockTrailTests(unittest.TestCase):
    """By request: "make sure when there is a profit to not let on too
    much loss" - a winner must not round-trip into a loser.
    """

    def decide(self, price, peak, cost="1.00", quantity=1, **overrides):
        # The fixed take-profit target is pushed far out of reach so
        # these cases isolate the TRAIL. At the real 15% default a
        # $1.00 position targets $1.15, which would fire first and
        # hide whether the trail works at all.
        overrides.setdefault("option_take_profit_percent", Decimal("5"))
        # Arm/giveback pinned to the values these cases were written
        # against, so they keep testing the TRAIL MECHANISM rather than
        # whatever the shipped defaults happen to be. The defaults
        # themselves are covered by
        # test_the_real_defaults_would_have_saved_the_live_gme_trade.
        overrides.setdefault("profit_lock_arm_percent", Decimal("0.10"))
        overrides.setdefault("profit_lock_giveback_fraction", Decimal("0.50"))
        strategy = SimpleNamespace(config=focus_config(**overrides))
        return option_decision(
            strategy,
            Decimal(price),
            quantity,
            Decimal(cost),
            30,
            peak_price=Decimal(peak) if peak is not None else None,
        )

    def test_does_not_arm_before_the_peak_clears_the_arm_threshold(self):
        # Peak only 5% up, arm bar is 10% - nothing to protect yet.
        decision = self.decide(price="1.02", peak="1.05")
        self.assertEqual(decision.action, "HOLD")

    def test_holds_while_price_stays_above_the_floor(self):
        # Peak 1.40 is a +40% run, so the >=20% tier applies: give
        # back 20%, floor = 1.00 + 0.40*0.80 = 1.32.
        decision = self.decide(price="1.35", peak="1.40")
        self.assertEqual(decision.action, "HOLD")

    def test_locks_in_the_gain_once_price_falls_back_to_the_floor(self):
        decision = self.decide(price="1.20", peak="1.40")
        self.assertEqual(decision.action, "PROFIT")
        self.assertEqual(decision.target_price, Decimal("1.3200"))

    def test_the_giveback_tightens_as_the_run_gets_bigger(self):
        """By request: "have the profit lock dynamic shift based on the
        amount of profit it is at." A fixed fraction is wrong at both
        ends - it surrenders a painful share of a large winner, and on
        a small one hands back so little that fees eat the rest.
        """
        # cost 1.00 throughout; floor = 1 + gain*(1-giveback)
        for peak, expected_floor, tier in (
            ("1.04", "1.0260", "under 5% -> config default 0.35"),
            ("1.08", "1.0560", "5-10%  -> 0.30"),
            ("1.15", "1.1125", "10-20% -> 0.25"),
            ("1.40", "1.3200", ">=20%  -> 0.20"),
        ):
            decision = self.decide(
                price="1.00",
                peak=peak,
                profit_lock_arm_percent=Decimal("0.025"),
                profit_lock_giveback_fraction=Decimal("0.35"),
            )
            self.assertEqual(
                decision.target_price,
                Decimal(expected_floor),
                f"peak {peak} ({tier})",
            )

    def test_an_armed_trail_never_books_less_than_the_minimum_gain(self):
        """By request: "but yeah never below 2.5%." A position that
        armed just over the bar must not trail down to a few cents and
        still call itself a PROFIT.
        """
        # Peak 1.03 (+3%): giveback 0.35 -> raw floor 1.0195, and the
        # scaled minimum is half the peak gain = +1.5% -> 1.015. The
        # raw floor already clears that, so it stands.
        decision = self.decide(
            price="1.00",
            peak="1.03",
            profit_lock_arm_percent=Decimal("0.025"),
            profit_lock_giveback_fraction=Decimal("0.35"),
        )
        self.assertEqual(decision.action, "PROFIT")
        self.assertGreaterEqual(decision.target_price, Decimal("1.015"))
        self.assertIn("profit-lock", decision.reason)

    def test_the_floor_always_sits_above_entry_cost(self):
        # The whole point: a position that ran to +40% cannot come
        # back and close at or below the 1.00 it was bought at.
        decision = self.decide(price="1.05", peak="1.40")
        self.assertEqual(decision.action, "PROFIT")
        self.assertGreater(decision.target_price, Decimal("1.00"))

    def test_never_fires_below_cost_plus_fee(self):
        # Peak barely over the arm bar on a large position, so the
        # floor lands inside the fee margin - taking it would book a
        # real loss labelled PROFIT, the exact LFUS/FIGR/MAGN failure.
        # Options are billed per contract now, so the knob that moves
        # an option's fee margin is option_sell_fee_per_contract - the
        # flat stock fee no longer reaches this path at all.
        decision = self.decide(
            price="1.00",
            peak="1.10",
            quantity=1,
            option_sell_fee_per_contract=Decimal("20"),
        )
        self.assertNotEqual(decision.action, "PROFIT")

    def test_the_real_defaults_would_have_saved_the_live_gme_trade(self):
        """Live 2026-09-22, GME 261009C23: bought 2 at $1.40, ran to
        $1.46 (+4.29%, +$12 open), round-tripped to $1.37 (-$6). The
        lock never armed because the arm bar was 10% and the whole
        move was 4.29% - the one mechanism meant to stop a winner
        becoming a loser sat disarmed the entire time.

        Uses the SHIPPED defaults deliberately: this is a test about
        whether the numbers we actually run are set correctly, not
        about whether the trail maths works.
        """
        from webull_bot.config import Settings

        real = Settings()
        strategy = SimpleNamespace(
            config=focus_config(
                option_take_profit_percent=real.option_take_profit_percent,
                option_stop_loss_percent=real.option_stop_loss_percent,
                profit_lock_arm_percent=real.profit_lock_arm_percent,
                profit_lock_giveback_fraction=real.profit_lock_giveback_fraction,
            )
        )
        # At the peak the trail must be armed.
        peak = Decimal("1.46")
        cost = Decimal("1.40")
        armed = (peak - cost) / cost >= real.profit_lock_arm_percent
        self.assertTrue(
            armed,
            f"a {((peak-cost)/cost*100):.2f}% run must arm the trail - "
            f"arm bar is {real.profit_lock_arm_percent*100:.1f}%",
        )
        # Falling back to 1.37 must exit as PROFIT, not ride to the stop.
        decision = option_decision(
            strategy,
            Decimal("1.37"),
            2,
            cost,
            30,
            peak_price=peak,
        )
        self.assertEqual(
            decision.action,
            "PROFIT",
            "the give-back from $1.46 to $1.37 must trigger the lock "
            "instead of letting a +$12 position close at -$6",
        )
        self.assertGreater(decision.target_price, cost)

    def test_a_tighter_giveback_locks_in_more_of_the_peak(self):
        strategy = SimpleNamespace(
            config=focus_config(option_take_profit_percent=Decimal("5"))
        )
        # Same peak, throttled giveback (0.25) -> floor 1.30, not 1.20
        decision = option_decision(
            strategy,
            Decimal("1.25"),
            1,
            Decimal("1.00"),
            30,
            peak_price=Decimal("1.40"),
            giveback_fraction=Decimal("0.25"),
        )
        self.assertEqual(decision.action, "PROFIT")
        self.assertEqual(decision.target_price, Decimal("1.30"))

    def test_a_real_stop_still_wins_over_the_trail(self):
        # Price has collapsed through the stop; that's a LOSS, and
        # the trail must not relabel it as a profit.
        decision = self.decide(price="0.40", peak="1.40")
        self.assertEqual(decision.action, "LOSS")

    def test_disabled_trail_changes_nothing(self):
        decision = self.decide(
            price="1.20", peak="1.40", profit_lock_enabled=False
        )
        self.assertEqual(decision.action, "HOLD")

    def test_legacy_callers_without_a_peak_are_unaffected(self):
        decision = self.decide(price="1.05", peak=None)
        self.assertEqual(decision.action, "HOLD")


class OptionStopIsNotWidenedOnFreshPositionsTests(unittest.TestCase):
    """The stop must mean what it says from the first second.

    time_aware_stop widening (1.5x for the first 60s) made sense
    paired with the old 50% option stop. Against the 20% that replaced
    it, it turned the stop into 30% for the first minute - and focus
    mode sizes a position at nearly the whole balance, so a 2x$1.80
    position could lose $108 of a $369 account (29%) inside 60
    seconds, under a stop deliberately set to cap losses at 20%.
    """

    def decide(self, price, seconds, cost="1.00", **overrides):
        strategy = SimpleNamespace(config=focus_config(**overrides))
        return option_decision(
            strategy, Decimal(price), 2, Decimal(cost), 30,
            seconds_since_entry=seconds,
        )

    def test_a_fresh_position_stops_at_the_configured_percent(self):
        # -25%: inside the widened 30% bar, past the real 20% one.
        for age in (0, 30, 59, 61, 600):
            decision = self.decide(
                "0.75", age, option_stop_loss_percent=Decimal("0.20")
            )
            self.assertEqual(
                decision.action,
                "LOSS",
                f"at {age}s a -25% position must stop on a 20% stop",
            )

    def test_the_widening_still_works_when_explicitly_enabled(self):
        """Off by default, not deleted - the mechanism is intact for
        anyone who wants it back.
        """
        decision = self.decide(
            "0.75",
            0,
            option_stop_loss_percent=Decimal("0.20"),
            option_time_aware_stop_enabled=True,
            time_aware_stop_widen_seconds=60,
            time_aware_stop_widen_multiplier=Decimal("1.5"),
        )
        self.assertEqual(decision.action, "HOLD")

    def test_the_shared_stock_switch_no_longer_affects_options(self):
        decision = self.decide(
            "0.75",
            0,
            option_stop_loss_percent=Decimal("0.20"),
            time_aware_stop_enabled=True,
            time_aware_stop_widen_multiplier=Decimal("1.5"),
        )
        self.assertEqual(
            decision.action,
            "LOSS",
            "the stock-side switch must not widen an option stop",
        )


class MomentumExitFillsImmediatelyTests(unittest.TestCase):
    """By request: "it should exit green on its own before fading" and
    "execution is also important".

    A momentum exit fires because exhaustion was detected - RSI
    divergence, resistance, or participation flipping against the
    position. The whole point is to be OUT before the fade, so the
    order has to actually cross, not rest.

    decision.target_price for these is sell_realizable_price, the BID,
    which is already marketable. Quantizing it UP to the $0.05 option
    tick pushed it ABOVE the bid (1.43 -> 1.45), so the order rested
    while the premium kept falling - the exact failure being fixed.
    """

    def test_rounding_down_keeps_a_bid_priced_exit_marketable(self):
        from decimal import ROUND_DOWN, ROUND_UP

        from webull_bot.webull_api import WebullAPI

        api = WebullAPI.__new__(WebullAPI)
        bid = Decimal("1.43")
        up = api._quantize_to_option_tick(bid, ROUND_UP)
        down = api._quantize_to_option_tick(bid, ROUND_DOWN)
        self.assertGreater(
            up, bid, "ROUND_UP lifts the price above the bid - it rests"
        )
        self.assertLessEqual(
            down, bid, "ROUND_DOWN keeps it at or below the bid - it fills"
        )

    def test_one_tick_of_rounding_still_clears_cost(self):
        """The ROUND_UP existed to stop a target quantizing back onto
        cost (the NKE incident). A momentum exit is safe from that
        because min_margin already requires a full $0.05 tick of
        headroom over cost before it can fire at all.
        """
        from decimal import ROUND_DOWN

        from webull_bot.webull_api import WebullAPI

        api = WebullAPI.__new__(WebullAPI)
        cost = Decimal("1.40")
        # min_margin = max(fee_per_share, 0.05); the trigger needs
        # sell_realizable_price - cost > that, so the worst legal case
        # is a hair over one tick above cost.
        bid = cost + Decimal("0.051")
        placed = api._quantize_to_option_tick(bid, ROUND_DOWN)
        self.assertGreater(
            placed, cost,
            "even after rounding down a full tick the exit must be "
            "strictly above cost",
        )


class StaleOptionExitTests(unittest.TestCase):
    """By explicit request, after three positions sat open over two
    hours going nowhere: "if something is not going for much profit at
    all then make it sell for even cents" / "now it is in a loss, why
    didn't it sell".

    Live 2026-09-22: MARA (-8.3%), SOFI (-3.5%) and NFLX (-1.0%) all
    opened at 10:46 and were still open past 12:52. The target needs
    +10%, the trail must arm at +2.5%, the stop needs -20%, and
    boost_stalled_positions never sells at a loss - so a trade that
    merely does not work hits nothing at all.
    """

    def decide(self, price, cost="1.00", seconds=None, **overrides):
        overrides.setdefault("option_stale_exit_enabled", True)
        overrides.setdefault("option_stale_exit_minutes", 45)
        overrides.setdefault(
            "option_stale_exit_max_loss_percent", Decimal("0.08")
        )
        strategy = SimpleNamespace(config=focus_config(**overrides))
        return option_decision(
            strategy,
            Decimal(price),
            1,
            Decimal(cost),
            30,
            seconds_since_entry=seconds,
        )

    def test_a_stalled_small_loser_is_closed_to_free_the_capital(self):
        # -3.5%, the live SOFI case: inside the stop, past the timer.
        decision = self.decide("0.965", seconds=46 * 60)
        self.assertEqual(decision.action, "LOSS")
        self.assertIn("stale position", decision.reason)

    def test_a_stalled_tiny_winner_is_taken_rather_than_held(self):
        decision = self.decide("1.01", seconds=46 * 60)
        self.assertEqual(decision.action, "PROFIT")
        self.assertIn("stale position", decision.reason)

    def test_it_does_not_fire_before_the_timer(self):
        decision = self.decide("0.97", seconds=10 * 60)
        self.assertEqual(decision.action, "HOLD")

    def test_a_real_loser_is_left_to_the_stop_not_dumped_on_a_timer(self):
        # -15% is past option_stale_exit_max_loss_percent (8%): this is
        # not a stalled trade, it is a losing one, and the -20% stop
        # owns it. Without that bound the timer would dump every loser
        # at whatever the market offered the instant it expired.
        decision = self.decide("0.85", seconds=90 * 60)
        self.assertEqual(decision.action, "HOLD")

    def test_a_working_trade_is_never_cut_short_by_the_clock(self):
        """The timer sits BELOW the target and trail on purpose."""
        decision = self.decide("1.25", seconds=90 * 60)
        self.assertEqual(decision.action, "PROFIT")
        self.assertIn("profit target", decision.reason)

    def test_disabled_leaves_the_old_behaviour_untouched(self):
        decision = self.decide(
            "0.97", seconds=90 * 60, option_stale_exit_enabled=False
        )
        self.assertEqual(decision.action, "HOLD")

    def test_no_entry_timestamp_never_triggers_it(self):
        decision = self.decide("0.97", seconds=None)
        self.assertEqual(decision.action, "HOLD")


class ProfitThrottleTests(unittest.TestCase):
    """By request: "once you hit a certain profit slow down" - stop
    adding new risk, without halting exits.
    """

    def bot(self, **overrides):
        bot = SimpleNamespace(
            config=focus_config(**overrides),
            day_start_equity=None,
            day_start_equity_date=None,
            profit_throttle_armed=False,
            profit_throttle_streak=0,
            cached_total_equity=None,
            now=lambda: datetime(2026, 9, 21, 10, 0, tzinfo=ZoneInfo("UTC")),
        )
        bot.update_profit_throttle = (
            AutoTrader.update_profit_throttle.__get__(bot)
        )
        bot.new_entries_blocked = AutoTrader.new_entries_blocked.__get__(bot)
        return bot

    def test_captures_day_start_equity_on_the_first_reading(self):
        bot = self.bot()
        bot.update_profit_throttle(Decimal("400"))
        self.assertEqual(bot.day_start_equity, Decimal("400"))
        self.assertFalse(bot.new_entries_blocked())

    def test_does_not_arm_below_the_target(self):
        bot = self.bot()
        bot.update_profit_throttle(Decimal("400"))
        bot.update_profit_throttle(Decimal("415"))
        self.assertFalse(bot.new_entries_blocked())

    def test_arms_once_the_target_holds_for_enough_readings(self):
        bot = self.bot()
        bot.update_profit_throttle(Decimal("400"))
        for _ in range(3):
            bot.update_profit_throttle(Decimal("420"))
        self.assertTrue(bot.new_entries_blocked())

    def test_a_single_settlement_spike_does_not_arm_the_throttle(self):
        """Live incident, first session this shipped: the bot booked
        equity of $409.54 (+12.52%) four minutes into the open and
        armed the throttle, disabling new entries for the whole day,
        while the broker showed $363 flat with NO open positions.

        Right after a closing fill the sale proceeds are already in
        buying_power while the position is still in the positions
        list, so equity double-counts it for a reading or two - as
        the account owner put it, "when a trade goes through for a
        second the calculations occur and the account spikes, but
        that doesn't mean anything."
        """
        bot = self.bot()
        bot.update_profit_throttle(Decimal("363.96"))
        bot.update_profit_throttle(Decimal("409.54"))  # the phantom
        bot.update_profit_throttle(Decimal("363.96"))  # settled again
        self.assertFalse(
            bot.new_entries_blocked(),
            "a one-reading settlement artifact must not stop the day",
        )

    def test_a_broken_streak_has_to_start_over(self):
        bot = self.bot()
        bot.update_profit_throttle(Decimal("400"))
        bot.update_profit_throttle(Decimal("420"))
        bot.update_profit_throttle(Decimal("420"))
        bot.update_profit_throttle(Decimal("401"))  # back under target
        bot.update_profit_throttle(Decimal("420"))
        bot.update_profit_throttle(Decimal("420"))
        self.assertFalse(bot.new_entries_blocked())
        bot.update_profit_throttle(Decimal("420"))
        self.assertTrue(bot.new_entries_blocked())

    def test_stays_armed_after_a_pullback(self):
        # One-way once ARMED: disarming on a dip would re-open size
        # into the exact give-back the throttle exists to prevent.
        bot = self.bot()
        bot.update_profit_throttle(Decimal("400"))
        for _ in range(3):
            bot.update_profit_throttle(Decimal("420"))
        bot.update_profit_throttle(Decimal("405"))
        self.assertTrue(bot.new_entries_blocked())

    def test_an_implausible_zero_equity_reading_is_ignored(self):
        bot = self.bot()
        bot.update_profit_throttle(Decimal("400"))
        bot.update_profit_throttle(Decimal("0"))
        self.assertEqual(bot.day_start_equity, Decimal("400"))

    def test_disabled_focus_mode_never_blocks_entries(self):
        bot = self.bot(focus_mode_enabled=False)
        bot.update_profit_throttle(Decimal("400"))
        bot.update_profit_throttle(Decimal("500"))
        self.assertFalse(bot.new_entries_blocked())


class EmptyResultRetryTests(unittest.TestCase):
    """Regression: both once-daily routines used to stamp the date
    BEFORE producing a result, so an empty first attempt marked the
    day done permanently.

    That is not a rare edge case - it is every mid-session deploy.
    The container starts with no scan history, so strategy.metrics is
    empty and volume_delta (which RVOL needs) has no samples yet, the
    first attempt legitimately finds nothing, and the bot would then
    refuse to trade for the rest of the session.
    """

    def bot(self, now, metrics=None):
        tz = ZoneInfo("America/New_York")
        bot = SimpleNamespace(
            config=focus_config(option_eod_close_time="15:50"),
            daily_batch=[],
            daily_batch_date=None,
            daily_batch_logged=[],
            daily_batch_first_attempt_at=None,
            daily_batch_first_attempt_date=None,
            daily_batch_logged_empty_date=None,
            focus_cohort=[],
            focus_cohort_date=None,
            focus_logged_empty_date=None,
            focus_symbol_no_chain=set(),
            focus_contract_discovery_failures=defaultdict(int),
            focus_symbol_affordability_checked=set(),
            focus_wide_discovered=set(),
            focus_cohort_growth_attempt_at=None,
            premarket_gainers=set(),
            agent_predicted_gainers=set(),
            seed_popular_symbols=set(),
            agent_popular_symbols=set(),
            market_pulse_cache={},
            wash_sales=SimpleNamespace(blocked_until=lambda key: None),
            agent_assessment=lambda symbol: None,
            strategy=SimpleNamespace(
                metrics=metrics or {},
                prices={},
                volume_delta_ema={},
                volume_delta_latest={},
                priority_score=lambda s, a: 1.0,
                realized_volatility_percent=lambda s: None,
            ),
            timezone=tz,
        )
        bot.session_moment = AutoTrader.session_moment.__get__(bot)
        bot.refresh_daily_batch = AutoTrader.refresh_daily_batch.__get__(bot)
        bot.select_focus_cohort = AutoTrader.select_focus_cohort.__get__(bot)
        bot._now = now
        return bot

    def moment(self, hh, mm):
        return datetime(2026, 9, 21, hh, mm, tzinfo=ZoneInfo("America/New_York"))

    def test_empty_batch_on_the_first_attempt_never_gives_up_immediately(self):
        # The actual live bug this regresses: give-up used to compare
        # wall-clock `moment` directly against focus_lock_time, so a
        # restart landing after 09:45 - this account's actual restart
        # landed at 10:16 - gave up on its very FIRST attempt with
        # zero real retries. Give-up is now elapsed-attempt-time
        # based, so a single call, no matter how late in the morning,
        # must never give up immediately.
        bot = self.bot(self.moment(10, 30))
        bot.refresh_daily_batch(self.moment(10, 30))
        self.assertIsNone(
            bot.daily_batch_date,
            "a first attempt must always get a real retry window, "
            "regardless of wall-clock time",
        )

    def test_gives_up_after_the_retry_window_elapses(self):
        bot = self.bot(self.moment(10, 30))
        with unittest.mock.patch("time.monotonic", return_value=1000.0):
            bot.refresh_daily_batch(self.moment(10, 30))
        self.assertIsNone(bot.daily_batch_date)
        retry_seconds = bot.config.daily_batch_retry_minutes * 60
        with unittest.mock.patch(
            "time.monotonic", return_value=1000.0 + retry_seconds + 1
        ):
            bot.refresh_daily_batch(self.moment(10, 31))
        self.assertEqual(bot.daily_batch_date, self.moment(10, 31).date())

    def test_a_quiet_premarket_never_kills_the_day_before_the_lock(self):
        # The batch's first attempt is a full hour before focus_lock_
        # time, and the retry window is 20 minutes, so an elapsed-only
        # give-up marked the whole day done ~40 minutes BEFORE the lock
        # - and ~25 minutes before the opening bell had even printed
        # the regular-session gaps this screens on. Nothing could trade
        # for the rest of that session. "Nothing qualifies yet at
        # 08:45" is the normal quiet-pre-market state, not a verdict.
        bot = self.bot(self.moment(8, 45))
        with unittest.mock.patch("time.monotonic", return_value=1000.0):
            bot.refresh_daily_batch(self.moment(8, 45))
        retry_seconds = bot.config.daily_batch_retry_minutes * 60
        with unittest.mock.patch(
            "time.monotonic", return_value=1000.0 + retry_seconds + 1
        ):
            bot.refresh_daily_batch(self.moment(9, 6))
        self.assertIsNone(
            bot.daily_batch_date,
            "the retry window must not be able to end the day before "
            "focus_lock_time - the batch exists to feed that lock",
        )

    def test_the_retry_window_is_anchored_at_the_lock_not_the_first_try(self):
        # An hour of pre-market attempts must not spend the window, or
        # the batch quits at the very instant the post-bell data it
        # needs arrives. The clock starts at the first at-or-after-lock
        # attempt, so a full window is still available then.
        bot = self.bot(self.moment(8, 45))
        with unittest.mock.patch("time.monotonic", return_value=1000.0):
            bot.refresh_daily_batch(self.moment(8, 45))
        retry_seconds = bot.config.daily_batch_retry_minutes * 60
        with unittest.mock.patch(
            "time.monotonic", return_value=1000.0 + retry_seconds + 1
        ):
            bot.refresh_daily_batch(self.moment(9, 46))
        self.assertIsNone(
            bot.daily_batch_date,
            "the first attempt after the lock must still get a full "
            "retry window, not one already spent pre-market",
        )
        with unittest.mock.patch(
            "time.monotonic", return_value=1000.0 + (retry_seconds * 2) + 2
        ):
            bot.refresh_daily_batch(self.moment(10, 7))
        self.assertEqual(
            bot.daily_batch_date,
            self.moment(10, 7).date(),
            "the elapsed timer's real job - stopping a mid-session "
            "restart retrying forever - must still work",
        )

    def test_the_eod_close_backstop_overrides_the_retry_window(self):
        # Even a first attempt gives up once there is no real session
        # left to trade a batch built now.
        bot = self.bot(self.moment(15, 55))
        bot.refresh_daily_batch(self.moment(15, 55))
        self.assertEqual(bot.daily_batch_date, self.moment(15, 55).date())

    def test_the_batch_keeps_rebuilding_until_the_cohort_locks(self):
        """Live, 2026-09-22: the 07:45 CT pass saw 11 candidates and
        exactly one cleared, so the cohort would have locked a single
        name - the behaviour the cohort exists to replace. The pool is
        thin that early because its sources are still pre-market; by
        the bell the same screen sees 65-84. Freezing on the first
        non-empty result capped the cohort at the morning's thinnest
        reading.
        """
        thin = {"AAPL": {"volume": 5_000_000, "change_ratio": 0.03, "spread_percent": "0.1"}}
        bot = self.bot(self.moment(8, 45), metrics=thin)
        bot.strategy.prices = {"AAPL": Decimal("100"), "MSFT": Decimal("100")}
        bot.seed_popular_symbols = {"AAPL", "MSFT"}
        bot.refresh_daily_batch(self.moment(8, 45))
        self.assertEqual(bot.daily_batch, ["AAPL"])
        # Post-bell: MSFT now qualifies too and must be picked up.
        bot.strategy.metrics["MSFT"] = {
            "volume": 9_000_000,
            "change_ratio": 0.05,
            "spread_percent": "0.1",
        }
        bot.refresh_daily_batch(self.moment(9, 35))
        self.assertEqual(sorted(bot.daily_batch), ["AAPL", "MSFT"])

    def test_a_later_thin_pass_never_wipes_an_already_good_batch(self):
        metrics = {
            "AAPL": {"volume": 5_000_000, "change_ratio": 0.03, "spread_percent": "0.1"}
        }
        bot = self.bot(self.moment(8, 45), metrics=metrics)
        bot.strategy.prices = {"AAPL": Decimal("100")}
        bot.seed_popular_symbols = {"AAPL"}
        bot.refresh_daily_batch(self.moment(8, 45))
        self.assertEqual(bot.daily_batch, ["AAPL"])
        # Gap fades - a catalyst that already fired is not undone.
        bot.strategy.metrics["AAPL"]["change_ratio"] = 0.001
        bot.refresh_daily_batch(self.moment(9, 30))
        self.assertEqual(bot.daily_batch, ["AAPL"])

    def test_the_batch_keeps_learning_new_names_after_the_lock(self):
        """Freezing at the lock meant a mid-session restart rebuilt
        once on cold metrics and kept that thin batch all day, so a
        redeploy could permanently shrink the tradeable universe. The
        batch also feeds cohort backfill after the lock, which is
        useless if it can never learn a name that started qualifying
        later in the session.
        """
        metrics = {
            "AAPL": {"volume": 5_000_000, "change_ratio": 0.03, "spread_percent": "0.1"}
        }
        bot = self.bot(self.moment(8, 45), metrics=metrics)
        bot.strategy.prices = {"AAPL": Decimal("100"), "MSFT": Decimal("100")}
        bot.seed_popular_symbols = {"AAPL", "MSFT"}
        bot.refresh_daily_batch(self.moment(8, 45))
        self.assertEqual(bot.daily_batch, ["AAPL"])
        bot.strategy.metrics["MSFT"] = {
            "volume": 9_000_000,
            "change_ratio": 0.05,
            "spread_percent": "0.1",
        }
        bot.refresh_daily_batch(self.moment(10, 30))
        self.assertEqual(sorted(bot.daily_batch), ["AAPL", "MSFT"])

    def test_a_new_day_clears_the_previous_session_s_disqualifications(self):
        """Disqualifications are scoped to one session by design, but
        the sets live for the life of the process - so a long-running
        container would bar a name forever over a single bad day. The
        cohort retires up to focus_cohort_size names a session instead
        of one, so this leaks an order of magnitude faster.
        """
        bot = self.bot(self.moment(10, 30))
        bot.focus_symbol_no_chain.add("MRNA")
        bot.focus_contract_discovery_failures["MRNA"] = 2
        bot.focus_symbol_affordability_checked.add("MRNA")
        tomorrow = datetime(2026, 9, 22, 10, 30, tzinfo=ZoneInfo("America/New_York"))
        bot.refresh_daily_batch(tomorrow)
        self.assertEqual(bot.focus_symbol_no_chain, set())
        self.assertEqual(dict(bot.focus_contract_discovery_failures), {})
        self.assertEqual(bot.focus_symbol_affordability_checked, set())

    def test_disqualifications_survive_within_the_same_session(self):
        bot = self.bot(self.moment(10, 30))
        bot.refresh_daily_batch(self.moment(10, 30))
        bot.focus_symbol_no_chain.add("MRNA")
        bot.refresh_daily_batch(self.moment(10, 31))
        self.assertIn("MRNA", bot.focus_symbol_no_chain)

    def test_a_stale_attempt_timestamp_does_not_bleed_into_a_new_day(self):
        # time.monotonic() never resets, so a leftover timestamp from
        # a prior day's give-up must not make day 2 look instantly
        # expired.
        bot = self.bot(self.moment(10, 30))
        bot.daily_batch_first_attempt_date = self.moment(10, 30).date()
        bot.daily_batch_first_attempt_at = 100.0
        tomorrow = datetime(2026, 9, 22, 10, 30, tzinfo=ZoneInfo("America/New_York"))
        with unittest.mock.patch("time.monotonic", return_value=100000.0):
            bot.refresh_daily_batch(tomorrow)
        self.assertIsNone(bot.daily_batch_date)

    def test_a_real_batch_stamps_the_day(self):
        metrics = {
            "MRNA": {
                "volume": 5_000_000,
                "spread_percent": 0.1,
                "change_ratio": 0.05,
            }
        }
        bot = self.bot(self.moment(9, 0), metrics=metrics)
        bot.premarket_gainers = {"MRNA"}
        bot.strategy.prices = {"MRNA": Decimal("50")}
        bot.refresh_daily_batch(self.moment(9, 0))
        self.assertEqual(bot.daily_batch, ["MRNA"])
        self.assertEqual(bot.daily_batch_date, self.moment(9, 0).date())

    def test_excludes_a_non_established_symbol_even_with_a_huge_gap(self):
        """Live incident: GRML (286.7% gap) and GRAL both cleared
        gap/volume/spread and locked as the focus symbol, but neither
        carries a real options market - "the stocks we pick should be
        like fortune 500, or snp, or dow stocks, popular, known,
        established."
        """
        metrics = {
            "GRML": {
                "volume": 5_000_000,
                "spread_percent": 0.19,
                "change_ratio": 2.767,
            },
            "MRNA": {
                "volume": 5_000_000,
                "spread_percent": 0.1,
                "change_ratio": 0.05,
            },
        }
        bot = self.bot(self.moment(9, 0), metrics=metrics)
        bot.premarket_gainers = {"GRML", "MRNA"}
        bot.strategy.prices = {"GRML": Decimal("12"), "MRNA": Decimal("50")}
        bot.refresh_daily_batch(self.moment(9, 0))
        self.assertEqual(bot.daily_batch, ["MRNA"])

    def test_the_established_filter_can_be_turned_off(self):
        metrics = {
            "GRML": {
                "volume": 5_000_000,
                "spread_percent": 0.19,
                "change_ratio": 2.767,
            },
        }
        bot = self.bot(self.moment(9, 0), metrics=metrics)
        bot.config = focus_config(
            option_eod_close_time="15:50",
            daily_batch_require_established_symbols=False,
        )
        bot.premarket_gainers = {"GRML"}
        bot.strategy.prices = {"GRML": Decimal("12")}
        bot.refresh_daily_batch(self.moment(9, 0))
        self.assertEqual(bot.daily_batch, ["GRML"])

    def test_no_focus_pick_mid_session_retries(self):
        bot = self.bot(self.moment(9, 50))
        bot.daily_batch = ["AAA"]
        bot.select_focus_cohort(self.moment(9, 50))
        self.assertIsNone(
            bot.focus_cohort_date,
            "an unwarmed field at the lock time must stay retryable",
        )

    def test_no_focus_pick_at_closeout_gives_up(self):
        bot = self.bot(self.moment(15, 50))
        bot.daily_batch = ["AAA"]
        bot.select_focus_cohort(self.moment(15, 50))
        self.assertEqual(bot.focus_cohort_date, self.moment(15, 50).date())


def _fake_contract(underlying, symbol, option_type, strike, dte=20):
    expiration = (date.today() + timedelta(days=dte)).isoformat()
    return {
        "underlying_symbol": underlying,
        "symbol": symbol,
        "option_type": option_type,
        "tradable_status": "OC",
        "expiration_date": expiration,
        "strike_price": str(strike),
    }


class FocusSymbolIsAffordableDiscoveryTests(unittest.TestCase):
    """By explicit request ("its options should all be discovered and
    analyzed... I want the first order to go out at 9:45, not
    later"): the wide contract set fetched here to answer the
    affordability question is persisted for every candidate checked,
    not just the eventual winner - so the whole daily batch is
    genuinely discovered before the lock, and the winner needs zero
    post-lock discovery latency.
    """

    def bot(
        self,
        price=Decimal("168"),
        existing_contracts=None,
        fail=False,
        wide_discovered=None,
        quotes=None,
    ):
        calls = {"option_contracts": []}
        contracts = [
            _fake_contract("MRNA", "MRNAC", "CALL", 170),
            _fake_contract("MRNA", "MRNAP", "PUT", 166),
        ]
        quotes = quotes or {
            "MRNAC": {"symbol": "MRNAC", "bid": "1.90", "ask": "2.00"},
            "MRNAP": {"symbol": "MRNAP", "bid": "1.80", "ask": "1.90"},
        }

        class FakeApi:
            @staticmethod
            def option_contracts(underlying=None, option_symbol=None):
                calls["option_contracts"].append(underlying)
                if fail:
                    raise RuntimeError("no chain listed")
                return [c for c in contracts if c["underlying_symbol"] == underlying]

            @staticmethod
            def option_quotes(symbols):
                return [quotes[s] for s in symbols if s in quotes]

            @staticmethod
            def option_limit_price(quote, side):
                bid, ask = quote.get("bid"), quote.get("ask")
                if bid is None or ask is None:
                    return None
                return (Decimal(str(bid)) + Decimal(str(ask))) / 2

            @staticmethod
            def option_delta(quote):
                delta = quote.get("delta")
                return None if delta is None else Decimal(str(delta))

        def order_quantity(limit_price, bp):
            cost = limit_price * 100
            return (int(bp // cost), cost) if cost > 0 else (0, cost)

        bot = SimpleNamespace(
            config=focus_config(),
            option_contracts=existing_contracts or [],
            option_discovery_attempted=set(),
            option_average_down_count={},
            option_last_buy_price={},
            cached_option_buying_power=Decimal("300"),
            api=FakeApi(),
            option_contracts_state=SimpleNamespace(save=lambda *a, **k: None),
            focus_wide_discovered=set(wide_discovered or ()),
            strategy=SimpleNamespace(
                prices={"MRNA": price} if price else {},
                option_order_quantity=order_quantity,
                option_delta_ok=option_delta_ok,
            ),
        )
        bot.focus_symbol_is_affordable = AutoTrader.focus_symbol_is_affordable.__get__(bot)
        return bot, calls

    def test_discovers_and_persists_contracts_for_a_candidate(self):
        bot, calls = self.bot()
        result = bot.focus_symbol_is_affordable("MRNA")
        self.assertTrue(result)
        self.assertEqual(calls["option_contracts"], ["MRNA"])
        self.assertEqual(
            {c["symbol"] for c in bot.option_contracts}, {"MRNAC", "MRNAP"}
        )

    def test_a_second_check_of_the_same_candidate_does_not_refetch(self):
        """select_focus_cohort retries every cycle until something
        locks - re-fetching an already-discovered candidate's chain
        from the API on every retry would be pure repeated cost.
        """
        bot, calls = self.bot()
        bot.focus_symbol_is_affordable("MRNA")
        bot.focus_symbol_is_affordable("MRNA")
        bot.focus_symbol_is_affordable("MRNA")
        self.assertEqual(calls["option_contracts"], ["MRNA"])

    def test_does_not_duplicate_contracts_already_known_from_elsewhere(self):
        bot, calls = self.bot(
            existing_contracts=[_fake_contract("MRNA", "MRNAC", "CALL", 170)]
        )
        bot.focus_symbol_is_affordable("MRNA")
        symbols = [c["symbol"] for c in bot.option_contracts]
        self.assertEqual(symbols.count("MRNAC"), 1)

    def test_a_cheap_contract_outside_the_delta_window_is_not_affordable(self):
        """Live 2026-09-23, the reason the account sat out most of a
        session: 8 of the 10 locked cohort members produced ZERO
        entries all day, showing up as "sizing produced zero
        contracts=9 | delta out of range=9" every cycle - the same
        wall seen from two sides.

        Those names DID have contracts under the $0.99 per-entry
        budget, which is all this check used to ask about, but they
        carried delta 0.08-0.16: far-OTM lottery tickets that
        option_delta_ok rejects at entry. Their cheapest strike inside
        the delta window ran $1.25-$5.40, well out of reach. So each
        name passed affordability on a contract it could never
        actually enter, locked into the cohort, and then died at the
        delta gate on every single attempt.

        "Can the account buy something" is the wrong question. The
        question is "can the account buy something it is allowed to
        enter".
        """
        bot, _ = self.bot(
            quotes={
                # Affordable at $300 buying power, but 0.11 delta -
                # exactly the shape that fooled the old check.
                "MRNAC": {
                    "symbol": "MRNAC", "bid": "0.20", "ask": "0.30",
                    "delta": "0.11",
                },
                "MRNAP": {
                    "symbol": "MRNAP", "bid": "0.20", "ask": "0.30",
                    "delta": "-0.09",
                },
            }
        )
        self.assertFalse(bot.focus_symbol_is_affordable("MRNA"))

    def test_an_affordable_contract_inside_the_delta_window_still_passes(self):
        bot, _ = self.bot(
            quotes={
                "MRNAC": {
                    "symbol": "MRNAC", "bid": "0.20", "ask": "0.30",
                    "delta": "0.45",
                },
                "MRNAP": {
                    "symbol": "MRNAP", "bid": "0.20", "ask": "0.30",
                    "delta": "-0.40",
                },
            }
        )
        self.assertTrue(bot.focus_symbol_is_affordable("MRNA"))

    def test_a_missing_delta_still_passes(self):
        """option_delta_ok's own convention: an unavailable delta is
        not evidence against the contract, same as every other
        best-effort gate here. The default fixture quotes carry no
        delta at all.
        """
        bot, _ = self.bot()
        self.assertTrue(bot.focus_symbol_is_affordable("MRNA"))

    def test_fails_open_when_no_price_is_known_yet(self):
        bot, calls = self.bot(price=None)
        self.assertTrue(bot.focus_symbol_is_affordable("MRNA"))
        self.assertEqual(calls["option_contracts"], [])

    def test_fails_open_on_a_discovery_error(self):
        bot, calls = self.bot(fail=True)
        self.assertTrue(bot.focus_symbol_is_affordable("MRNA"))

    def test_correctly_reports_unaffordable_without_losing_the_discovery(self):
        bot, calls = self.bot()
        bot.cached_option_buying_power = Decimal("1")  # can't afford even 1 contract
        result = bot.focus_symbol_is_affordable("MRNA")
        self.assertFalse(result)
        # Still discovered and persisted, even though it's unaffordable -
        # "all discovered and analyzed" doesn't mean "only the
        # affordable ones."
        self.assertEqual(len(bot.option_contracts), 2)


class EnsureFocusSymbolContractsTests(unittest.TestCase):
    """By request: "you should be able to request contract by stock
    in webull openapi" - the locked symbol's option chain must exist
    immediately, not depend on the generic discovery rotation ever
    reaching it. By request ("it should have found a lot more...
    regardless of affordability"): discovery pulls the WIDE board
    (every valid strike/expiration), not select_atm_options' single
    best-guess CALL and PUT.
    """

    def bot(
        self,
        focus_cohort=("MRNA",),
        existing_contracts=None,
        price=Decimal("168"),
        contracts=None,
        quotes=None,
        fail=False,
        buying_power=Decimal("300"),
        open_positions=None,
        wide_discovered=None,
    ):
        calls = {"option_contracts": [], "option_quotes": []}
        default_contracts = contracts or [
            _fake_contract("MRNA", "MRNAC", "CALL", 170),
            _fake_contract("MRNA", "MRNAP", "PUT", 166),
        ]
        default_quotes = quotes or {
            "MRNAC": {"symbol": "MRNAC", "bid": "1.90", "ask": "2.00"},
            "MRNAP": {"symbol": "MRNAP", "bid": "1.80", "ask": "1.90"},
        }

        class FakeApi:
            @staticmethod
            def option_contracts(underlying=None, option_symbol=None):
                calls["option_contracts"].append(underlying)
                if fail:
                    raise RuntimeError("no chain listed")
                return [c for c in default_contracts if c["underlying_symbol"] == underlying]

            @staticmethod
            def option_quotes(symbols):
                calls["option_quotes"].append(list(symbols))
                return [default_quotes[s] for s in symbols if s in default_quotes]

            @staticmethod
            def option_limit_price(quote, side):
                bid, ask = quote.get("bid"), quote.get("ask")
                if bid is None or ask is None:
                    return None
                return (Decimal(str(bid)) + Decimal(str(ask))) / 2

            @staticmethod
            def option_delta(quote):
                delta = quote.get("delta")
                return None if delta is None else Decimal(str(delta))

        def order_quantity(limit_price, bp):
            cost = limit_price * 100
            if cost <= 0:
                return 0, cost
            return int(bp // cost), cost

        bot = SimpleNamespace(
            config=focus_config(),
            focus_cohort=list(focus_cohort),
            option_contracts=existing_contracts or [],
            option_discovery_attempted=set(),
            option_average_down_count={},
            option_last_buy_price={},
            cached_option_buying_power=buying_power,
            cached_positions=list(open_positions or []),
            api=FakeApi(),
            option_contracts_state=SimpleNamespace(save=lambda *a, **k: None),
            strategy=SimpleNamespace(
                prices={focus_cohort[0]: price} if price and focus_cohort else {},
                option_order_quantity=order_quantity,
                option_delta_ok=option_delta_ok,
            ),
            focus_symbol_no_chain=set(),
            focus_contract_discovery_failures=defaultdict(int),
            focus_symbol_affordability_checked=set(),
            focus_wide_discovered=set(wide_discovered or ()),
            last_stale_contract_prune=None,
        )
        bot.ensure_focus_cohort_contracts = (
            AutoTrader.ensure_focus_cohort_contracts.__get__(bot)
        )
        return bot, calls

    def test_discovers_the_wide_board_for_a_newly_locked_symbol(self):
        bot, calls = self.bot()
        bot.ensure_focus_cohort_contracts()
        self.assertEqual(calls["option_contracts"], ["MRNA"])
        self.assertEqual(len(bot.option_contracts), 2)
        self.assertEqual(
            {c["symbol"] for c in bot.option_contracts}, {"MRNAC", "MRNAP"}
        )

    def test_is_a_no_op_when_the_wide_chain_already_exists(self):
        bot, calls = self.bot(
            existing_contracts=[
                _fake_contract("MRNA", "MRNAC", "CALL", 170)
            ],
            wide_discovered={"MRNA"},
        )
        bot.ensure_focus_cohort_contracts()
        self.assertEqual(calls["option_contracts"], [])

    def test_two_leftover_contracts_do_not_count_as_a_discovered_chain(self):
        """Live 2026-09-22: discover_option_contracts' background
        rotation leaves exactly 2 contracts per name (one CALL, one
        PUT, a single strike). Treating that as "the chain exists"
        meant cohort members kept ONE ATM strike all session while
        NVDA - which happened to have none when first evaluated - got
        the full 180-contract board. Affordability was then judged off
        the most expensive point on the board.
        """
        bot, calls = self.bot(
            existing_contracts=[
                _fake_contract("MRNA", "MRNAC", "CALL", 170),
                _fake_contract("MRNA", "MRNAP", "PUT", 166),
            ],
        )
        bot.ensure_focus_cohort_contracts()
        self.assertEqual(
            calls["option_contracts"],
            ["MRNA"],
            "a cohort member without the wide sweep must still be "
            "fully discovered, not skipped because 2 stale contracts "
            "happen to be present",
        )

    def test_does_nothing_without_a_cohort(self):
        bot, calls = self.bot(focus_cohort=[])
        bot.ensure_focus_cohort_contracts()
        self.assertEqual(calls["option_contracts"], [])

    def test_retries_next_cycle_when_price_is_not_yet_known(self):
        bot, calls = self.bot(price=None)
        bot.ensure_focus_cohort_contracts()
        self.assertEqual(calls["option_contracts"], [])

    def test_disabled_focus_mode_never_calls_the_api(self):
        bot, calls = self.bot()
        bot.config = focus_config(focus_mode_enabled=False)
        bot.ensure_focus_cohort_contracts()
        self.assertEqual(calls["option_contracts"], [])

    def test_an_api_failure_does_not_raise(self):
        bot, calls = self.bot(fail=True)
        bot.ensure_focus_cohort_contracts()  # must not raise
        self.assertEqual(bot.option_contracts, [])

    def test_repeated_failure_disqualifies_and_clears_the_symbol(self):
        """Live incident ("grml has no contracts why is it in the
        batch" / "if discovery failed why is it still on that
        stock"): GRML locked with no listed option chain and the
        account sat stuck on it, retrying forever. A symbol with no
        chain will not develop one later today, so repeated failure
        must disqualify it and free the account to re-pick.
        """
        bot, calls = self.bot(fail=True)
        max_failures = bot.config.focus_contract_discovery_max_failures
        for _ in range(max_failures - 1):
            bot.ensure_focus_cohort_contracts()
            self.assertEqual(bot.focus_cohort, ["MRNA"])  # not yet disqualified
        bot.ensure_focus_cohort_contracts()
        self.assertIn("MRNA", bot.focus_symbol_no_chain)
        self.assertEqual(bot.focus_cohort, [])

    def test_a_transient_failure_does_not_disqualify_on_its_own(self):
        bot, calls = self.bot(fail=True)
        bot.ensure_focus_cohort_contracts()
        self.assertNotIn("MRNA", bot.focus_symbol_no_chain)
        self.assertEqual(bot.focus_cohort, ["MRNA"])

    def test_a_success_resets_the_failure_streak(self):
        bot, calls = self.bot()
        real_fetch = bot.api.option_contracts
        state = {"n": 0}

        def flaky(underlying=None, option_symbol=None):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("temporary blip")
            return real_fetch(underlying=underlying)

        bot.api.option_contracts = flaky
        bot.ensure_focus_cohort_contracts()
        self.assertEqual(bot.focus_contract_discovery_failures["MRNA"], 1)
        bot.ensure_focus_cohort_contracts()
        self.assertEqual(bot.focus_contract_discovery_failures["MRNA"], 0)
        self.assertEqual(bot.focus_cohort, ["MRNA"])

    def test_disqualifies_when_nothing_discovered_is_affordable(self):
        """Live incident: GOOGL locked, its own liquid $7.55/$7.90
        quote confirmed real, but at ~$790/contract against a $363
        account nothing was ever affordable and the account sat
        stuck. option_order_quantity sizing to 0 for every discovered
        contract must disqualify the symbol exactly like no chain.
        """
        bot, calls = self.bot(
            contracts=[_fake_contract("MRNA", "MRNAC", "CALL", 170)],
            quotes={"MRNAC": {"symbol": "MRNAC", "bid": "7.55", "ask": "7.90"}},
            buying_power=Decimal("363"),
        )
        bot.ensure_focus_cohort_contracts()
        self.assertIn("MRNA", bot.focus_symbol_no_chain)
        self.assertEqual(bot.focus_cohort, [])

    def test_stays_locked_when_at_least_one_contract_is_affordable(self):
        bot, calls = self.bot(buying_power=Decimal("300"))
        bot.ensure_focus_cohort_contracts()
        self.assertEqual(bot.focus_cohort, ["MRNA"])
        self.assertNotIn("MRNA", bot.focus_symbol_no_chain)

    def test_capital_tied_up_in_a_position_never_disqualifies_the_rest(self):
        """The cohort's defining hazard. Capital is first-come-first-
        served, so the moment one member's contract is bought the
        remaining buying power drops below what the others cost.
        Treating that as "unaffordable" would delete the rest of the
        cohort as a side effect of successfully trading - collapsing
        it to nothing after the very first fill.
        """
        bot, calls = self.bot(
            contracts=[_fake_contract("MRNA", "MRNAC", "CALL", 170)],
            quotes={"MRNAC": {"symbol": "MRNAC", "bid": "7.55", "ask": "7.90"}},
            buying_power=Decimal("5"),
            open_positions=[{"instrument_type": "OPTION", "symbol": "NVDAC"}],
        )
        bot.ensure_focus_cohort_contracts()
        self.assertEqual(bot.focus_cohort, ["MRNA"])
        self.assertNotIn("MRNA", bot.focus_symbol_no_chain)
        self.assertNotIn(
            "MRNA",
            bot.focus_symbol_affordability_checked,
            "must stay re-checkable so it is re-evaluated for real "
            "once the open position closes and capital returns",
        )

    def test_one_dead_name_does_not_drop_the_rest_of_the_cohort(self):
        """Only MRNA has a listed chain in this fixture. AAPL must burn
        its own streak and leave, while MRNA keeps trading - the whole
        reason for holding more than one name.
        """
        bot, calls = self.bot(focus_cohort=("MRNA", "AAPL"))
        bot.strategy.prices = {"MRNA": Decimal("168"), "AAPL": Decimal("200")}
        for _ in range(bot.config.focus_contract_discovery_max_failures):
            bot.ensure_focus_cohort_contracts()
        self.assertEqual(bot.focus_cohort, ["MRNA"])
        self.assertIn("AAPL", bot.focus_symbol_no_chain)
        self.assertNotIn("MRNA", bot.focus_symbol_no_chain)

    def test_failure_streaks_are_counted_per_symbol(self):
        """A single shared counter let one bad name spend the retry
        budget of every other member.
        """
        bot, calls = self.bot(focus_cohort=("MRNA", "AAPL"))
        bot.strategy.prices = {"MRNA": Decimal("168"), "AAPL": Decimal("200")}
        bot.ensure_focus_cohort_contracts()
        self.assertEqual(bot.focus_contract_discovery_failures["AAPL"], 1)
        self.assertEqual(bot.focus_contract_discovery_failures["MRNA"], 0)
        self.assertEqual(bot.focus_cohort, ["MRNA", "AAPL"])

    def test_affordability_is_checked_once_per_lock_not_every_cycle(self):
        bot, calls = self.bot(buying_power=Decimal("300"))
        bot.ensure_focus_cohort_contracts()
        quote_calls_after_first = len(calls["option_quotes"])
        bot.ensure_focus_cohort_contracts()
        self.assertEqual(len(calls["option_quotes"]), quote_calls_after_first)

    def test_stale_persisted_contracts_are_dropped_and_rediscovered(self):
        """Live incident ("we both know something is not working
        here"): NFLX locked, direction signals fired repeatedly,
        nothing ever entered. Direct inspection found NFLX's already-
        discovered contracts (persisted from a prior session) expired
        in 4 days - inside the 14-day floor - so _evaluate_option_
        entry's own DTE gate silently rejected every attempt as "too
        close to expiration," while ensure_focus_cohort_contracts
        treated their mere presence as "chain already exists" and
        never rediscovered a fresh, tradeable one.
        """
        stale = _fake_contract("MRNA", "MRNAC-OLD", "CALL", 170, dte=4)
        bot, calls = self.bot(existing_contracts=[stale])
        bot.ensure_focus_cohort_contracts()
        self.assertNotIn(stale, bot.option_contracts)
        self.assertEqual(calls["option_contracts"], ["MRNA"])
        self.assertTrue(
            any(c["underlying_symbol"] == "MRNA" for c in bot.option_contracts)
        )

    def test_pruning_still_runs_on_a_low_uptime_machine(self):
        """Regression: time.monotonic() is seconds since an arbitrary
        epoch (system/process boot), not wall-clock time - it can
        genuinely be small on a fresh CI runner or this bot's own
        deploy host right after a restart. The old 0.0 sentinel for
        "never pruned yet" was indistinguishable from "pruned moments
        ago" on such a machine, so the very first prune attempt
        silently skipped. Caught by a real CI run that failed the
        exact same test the local (long-uptime) machine passed.
        """
        stale = _fake_contract("MRNA", "MRNAC-OLD", "CALL", 170, dte=4)
        bot, calls = self.bot(existing_contracts=[stale])
        with unittest.mock.patch("time.monotonic", return_value=5.0):
            bot.ensure_focus_cohort_contracts()
        self.assertNotIn(stale, bot.option_contracts)

    def test_a_fresh_persisted_contract_is_left_alone(self):
        fresh = _fake_contract("MRNA", "MRNAC-OLD", "CALL", 170, dte=20)
        bot, calls = self.bot(
            existing_contracts=[fresh], wide_discovered={"MRNA"}
        )
        bot.ensure_focus_cohort_contracts()
        self.assertIn(fresh, bot.option_contracts)
        self.assertEqual(calls["option_contracts"], [])


class RepickOnNoChainTests(unittest.TestCase):
    """select_focus_cohort's side of the same incident: when
    ensure_focus_cohort_contracts disqualifies the locked symbol (sets
    the cohort empty but leaves focus_cohort_date stamped for
    today), the account must not get stuck with no symbol - it must
    fall through and pick a replacement, excluding the disqualified
    name.
    """

    def bot(self, now):
        tz = ZoneInfo("America/New_York")
        bot = SimpleNamespace(
            config=focus_config(),
            daily_batch=["GRML", "MRNA"],
            focus_cohort=[],
            focus_cohort_date=now.date(),
            focus_logged_empty_date=None,
            focus_symbol_no_chain={"GRML"},
            focus_wide_discovered=set(),
            wash_sales=SimpleNamespace(blocked_until=lambda key: None),
            agent_assessment=lambda symbol: None,
            strategy=SimpleNamespace(
                metrics={
                    "GRML": {"volume": 5_000_000},
                    "MRNA": {"volume": 5_000_000},
                },
                prices={"GRML": Decimal("2"), "MRNA": Decimal("168")},
                priority_score=lambda s, a: 999.0 if s == "GRML" else 1.0,
            ),
            timezone=tz,
            # Affordability is exercised separately (see
            # EnsureFocusSymbolContractsTests /
            # FocusSymbolIsAffordableTests) - this class is about the
            # wash-sale/no-chain re-pick path, so every candidate is
            # affordable here.
            focus_symbol_is_affordable=lambda symbol: True,
        )
        bot.session_moment = AutoTrader.session_moment.__get__(bot)
        bot.select_focus_cohort = AutoTrader.select_focus_cohort.__get__(bot)
        return bot

    def moment(self, hh, mm):
        return datetime(2026, 9, 21, hh, mm, tzinfo=ZoneInfo("America/New_York"))

    def test_repicks_a_replacement_after_disqualification(self):
        bot = self.bot(self.moment(11, 0))
        bot.select_focus_cohort(self.moment(11, 0))
        self.assertEqual(bot.focus_cohort, ["MRNA"])

    def test_never_repicks_the_disqualified_symbol_even_though_it_scores_higher(self):
        bot = self.bot(self.moment(11, 0))
        bot.select_focus_cohort(self.moment(11, 0))
        self.assertNotIn("GRML", bot.focus_cohort)


class ProactiveAffordabilityInSelectionTests(unittest.TestCase):
    """By request: "we want affordable options only so the stocks
    should also be focused like that" - checked proactively inside
    select_focus_cohort's candidate loop, so an established/liquid
    name whose cheapest contract still exceeds buying power never
    locks in the first place (live incident: GOOGL locked, then had
    to be reactively disqualified, wasting real trading time).
    """

    def bot(self, now, affordable):
        tz = ZoneInfo("America/New_York")
        bot = SimpleNamespace(
            config=focus_config(),
            daily_batch=["GOOGL", "MRNA"],
            focus_cohort=[],
            focus_cohort_date=None,
            focus_logged_empty_date=None,
            focus_symbol_no_chain=set(),
            focus_wide_discovered=set(),
            focus_cohort_growth_attempt_at=None,
            wash_sales=SimpleNamespace(blocked_until=lambda key: None),
            agent_assessment=lambda symbol: None,
            strategy=SimpleNamespace(
                metrics={
                    "GOOGL": {"volume": 20_000_000},
                    "MRNA": {"volume": 5_000_000},
                },
                prices={"GOOGL": Decimal("256"), "MRNA": Decimal("168")},
                # A big real-money mega-cap should naturally score
                # higher than a mid-cap - the point of this test is
                # that affordability overrides that anyway.
                priority_score=lambda s, a: 500.0 if s == "GOOGL" else 1.0,
            ),
            timezone=tz,
            focus_symbol_is_affordable=lambda symbol: affordable.get(symbol, True),
        )
        bot.session_moment = AutoTrader.session_moment.__get__(bot)
        bot.select_focus_cohort = AutoTrader.select_focus_cohort.__get__(bot)
        return bot

    def moment(self, hh, mm):
        return datetime(2026, 9, 21, hh, mm, tzinfo=ZoneInfo("America/New_York"))

    def test_an_unaffordable_higher_scoring_symbol_is_skipped(self):
        bot = self.bot(self.moment(9, 45), affordable={"GOOGL": False, "MRNA": True})
        bot.select_focus_cohort(self.moment(9, 45))
        self.assertEqual(bot.focus_cohort, ["MRNA"])

    def test_an_affordable_symbol_still_locks_normally(self):
        bot = self.bot(self.moment(9, 45), affordable={"GOOGL": True, "MRNA": True})
        bot.select_focus_cohort(self.moment(9, 45))
        self.assertEqual(bot.focus_cohort[0], "GOOGL")  # higher score ranks first

    def test_nothing_locks_when_every_candidate_is_unaffordable(self):
        bot = self.bot(self.moment(9, 45), affordable={"GOOGL": False, "MRNA": False})
        bot.select_focus_cohort(self.moment(9, 45))
        self.assertEqual(bot.focus_cohort, [])


class CohortSelectionTests(unittest.TestCase):
    """By explicit request: "allow a cohort of 5-10 stocks then, that
    all fit the criteria so that there are more options to play with."
    The single-symbol version made a whole session contingent on one
    name happening to have an affordable contract.
    """

    def bot(self, batch, affordable=None, blocked=(), size=10, scores=None,
            discovered=(), per_pass=25):
        tz = ZoneInfo("America/New_York")
        scores = scores or {
            symbol: float(len(batch) - i) for i, symbol in enumerate(batch)
        }
        affordable = affordable or {}
        bot = SimpleNamespace(
            config=focus_config(
                focus_cohort_size=size,
                focus_lock_discovery_per_pass=per_pass,
            ),
            daily_batch=list(batch),
            focus_cohort=[],
            focus_cohort_date=None,
            focus_logged_empty_date=None,
            focus_symbol_no_chain=set(),
            focus_wide_discovered=set(discovered),
            focus_cohort_growth_attempt_at=None,
            wash_sales=SimpleNamespace(
                blocked_until=lambda key: True
                if key.split(":")[0] in blocked
                else None
            ),
            agent_assessment=lambda symbol: None,
            strategy=SimpleNamespace(
                metrics={symbol: {"volume": 5_000_000} for symbol in batch},
                prices={symbol: Decimal("100") for symbol in batch},
                priority_score=lambda s, a: scores[s],
            ),
            timezone=tz,
            focus_symbol_is_affordable=lambda symbol: affordable.get(symbol, True),
        )
        bot.session_moment = AutoTrader.session_moment.__get__(bot)
        bot.select_focus_cohort = AutoTrader.select_focus_cohort.__get__(bot)
        return bot

    def moment(self, hh, mm):
        return datetime(2026, 9, 21, hh, mm, tzinfo=ZoneInfo("America/New_York"))

    def test_every_qualifying_name_joins_the_cohort_ranked_by_score(self):
        batch = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
        bot = self.bot(batch)
        bot.select_focus_cohort(self.moment(9, 45))
        self.assertEqual(bot.focus_cohort, batch)

    def test_the_cohort_is_capped_at_focus_cohort_size(self):
        batch = [f"S{i}" for i in range(20)]
        bot = self.bot(batch, size=10)
        bot.select_focus_cohort(self.moment(9, 45))
        self.assertEqual(len(bot.focus_cohort), 10)
        self.assertEqual(bot.focus_cohort, batch[:10])

    def test_verified_affordable_names_outrank_unverified_ones(self):
        """Live 2026-09-23 - the reason the account sat out most of a
        session with signals firing the whole time.

        focus_lock_discovery_per_pass is 2, so against a 16-name batch
        only 2 candidates get their affordability actually checked per
        pass; the other 14 are admitted by a deliberate fail-open
        branch and settled later. Ranking purely on priority_score
        then let those UNCHECKED names take the cohort slots, because
        the fail-open branch is silent - a name nobody looked at
        scores exactly like a name that passed, and score rewards
        volatility and volume, which mega-caps win.

        The cohort locked as NVDA, AAPL, ABNB, BABA, PLTR, AMZN,
        AVGO, MRNA, MARA, SOFI. Probing all 6717 discovered contracts
        against the real entry gates found 8 of those 10 had ZERO
        contracts both affordable and inside the delta window -
        cheapest in-delta strikes of $1.25-$5.40 against a $0.99
        per-entry budget. Only MARA and SOFI could ever trade.

        Verified names take slots first; unverified ones fill what is
        left.
        """
        batch = ["AAA", "BBB", "CCC", "DDD"]
        bot = self.bot(
            batch,
            # CCC and DDD are the high scorers, but they are past the
            # 2-name discovery budget so nothing has verified them.
            scores={"AAA": 1.0, "BBB": 2.0, "CCC": 9.0, "DDD": 8.0},
            size=2,
            per_pass=2,
        )
        bot.select_focus_cohort(self.moment(9, 45))
        self.assertEqual(bot.focus_cohort, ["BBB", "AAA"])

    def test_score_still_orders_within_the_verified_group(self):
        batch = ["AAA", "BBB", "CCC"]
        bot = self.bot(
            batch,
            scores={"AAA": 1.0, "BBB": 5.0, "CCC": 9.0},
            discovered=("AAA", "BBB", "CCC"),
            size=3,
        )
        bot.select_focus_cohort(self.moment(9, 45))
        self.assertEqual(bot.focus_cohort, ["CCC", "BBB", "AAA"])

    def test_members_still_have_to_clear_every_gate_individually(self):
        # The cohort is MORE candidates, not weaker ones.
        bot = self.bot(
            ["AAA", "BBB", "CCC", "DDD"],
            affordable={"BBB": False},
            blocked={"CCC"},
        )
        bot.select_focus_cohort(self.moment(9, 45))
        self.assertEqual(bot.focus_cohort, ["AAA", "DDD"])

    def test_a_disqualified_member_is_backfilled_without_disturbing_the_rest(self):
        batch = ["AAA", "BBB", "CCC"]
        bot = self.bot(batch, size=2)
        bot.select_focus_cohort(self.moment(9, 45))
        self.assertEqual(bot.focus_cohort, ["AAA", "BBB"])
        # BBB turns out to have no chain - it leaves, CCC takes the slot.
        bot.focus_symbol_no_chain.add("BBB")
        bot.select_focus_cohort(self.moment(10, 0))
        self.assertEqual(bot.focus_cohort, ["AAA", "CCC"])

    def test_an_unchanged_cohort_is_not_relocked_every_cycle(self):
        bot = self.bot(["AAA", "BBB"])
        bot.select_focus_cohort(self.moment(9, 45))
        locked = list(bot.focus_cohort)
        bot.select_focus_cohort(self.moment(9, 46))
        self.assertEqual(bot.focus_cohort, locked)

    def test_nothing_locks_when_no_candidate_clears_the_gates(self):
        bot = self.bot(["AAA", "BBB"], affordable={"AAA": False, "BBB": False})
        bot.select_focus_cohort(self.moment(9, 45))
        self.assertEqual(bot.focus_cohort, [])


class StockSuspensionTests(unittest.TestCase):
    def test_focus_mode_suspends_new_stock_entries(self):
        bot = SimpleNamespace(config=focus_config())
        suspended = AutoTrader.stock_entries_suspended.__get__(bot)
        self.assertTrue(suspended())

    def test_stock_entries_resume_when_focus_mode_is_off(self):
        bot = SimpleNamespace(config=focus_config(focus_mode_enabled=False))
        suspended = AutoTrader.stock_entries_suspended.__get__(bot)
        self.assertFalse(suspended())


class RealSessionScheduleAlignmentTests(unittest.TestCase):
    """The shipped defaults must land on the REAL market session.

    Every session field is a bare HH:MM read in trading_timezone, so a
    zone change that doesn't shift all of them moves every boundary by
    the zone offset - the bot would start trading options an hour late
    and flatten an hour after the close. These assertions are written
    against absolute instants (anchored to Eastern, where the exchange
    actually is) rather than against the literal strings, so they hold
    for whatever zone the config names and fail the moment the zone
    and the times disagree.
    """

    EXCHANGE = ZoneInfo("America/New_York")
    # A normal Tuesday, both zones on daylight time.
    TRADING_DAY = date(2026, 9, 22)

    def setUp(self):
        from webull_bot.config import Settings

        self.config = Settings()
        self.zone = ZoneInfo(self.config.trading_timezone)

    def instant(self, value: str) -> datetime:
        """The absolute moment a config HH:MM names, as the bot reads it."""
        return datetime.combine(
            self.TRADING_DAY,
            self.config.session_time(value),
            tzinfo=self.zone,
        )

    def exchange_instant(self, hour: int, minute: int) -> datetime:
        return datetime(
            self.TRADING_DAY.year,
            self.TRADING_DAY.month,
            self.TRADING_DAY.day,
            hour,
            minute,
            tzinfo=self.EXCHANGE,
        )

    def assert_lands_on(self, value: str, hour: int, minute: int, label: str):
        self.assertEqual(
            self.instant(value),
            self.exchange_instant(hour, minute),
            f"{label} resolves to {self.instant(value).astimezone(self.EXCHANGE)} "
            f"Eastern, expected {hour:02d}:{minute:02d} Eastern - the configured "
            f"zone ({self.config.trading_timezone}) and the HH:MM values disagree",
        )

    def test_option_session_matches_the_real_opening_and_closing_bell(self):
        self.assert_lands_on(
            self.config.option_market_open_time, 9, 30, "option_market_open_time"
        )
        self.assert_lands_on(
            self.config.option_market_close_time, 16, 0, "option_market_close_time"
        )
        self.assert_lands_on(
            self.config.option_eod_close_time, 15, 50, "option_eod_close_time"
        )

    def test_extended_hours_stock_session_matches_the_real_window(self):
        self.assert_lands_on(self.config.market_open_time, 4, 0, "market_open_time")
        self.assert_lands_on(self.config.market_close_time, 20, 0, "market_close_time")
        self.assert_lands_on(self.config.eod_close_time, 19, 50, "eod_close_time")

    def test_focus_funnel_lands_on_its_intended_eastern_moments(self):
        # By explicit request: the batch is built inside the 08:30-09:30
        # ET pre-market window, and the focus symbol locks at 09:45 ET
        # ("at 9:45am the stock needs to be selected... I want the first
        # order to go out at 9:45, not later").
        self.assert_lands_on(
            self.config.daily_batch_refresh_time, 8, 45, "daily_batch_refresh_time"
        )
        self.assert_lands_on(self.config.focus_lock_time, 9, 45, "focus_lock_time")

    def test_the_funnel_runs_in_the_right_order_within_the_session(self):
        batch = self.instant(self.config.daily_batch_refresh_time)
        opening = self.instant(self.config.option_market_open_time)
        lock = self.instant(self.config.focus_lock_time)
        flatten = self.instant(self.config.option_eod_close_time)
        close = self.instant(self.config.option_market_close_time)
        self.assertLess(batch, opening, "batch must be built before the bell")
        self.assertLess(opening, lock, "focus locks after the opening range")
        self.assertLess(lock, flatten, "a locked symbol needs session left to trade")
        self.assertLess(flatten, close, "closeout must begin before the close")

    def test_the_configured_zone_is_a_real_zone_the_clock_can_use(self):
        moment = datetime.now(self.zone)
        self.assertIsNotNone(moment.tzinfo)
        self.assertIsNotNone(moment.utcoffset())


if __name__ == "__main__":
    unittest.main()
