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
        focus_contract_discovery_max_failures=3,
        focus_daily_profit_target_fraction=Decimal("0.05"),
        profit_throttle_confirm_readings=3,
        popular_stock_min_volume=1_000_000,
        popular_stock_max_spread_percent=Decimal("0.50"),
        option_min_volatility_percent=Decimal("0.02"),
        profit_lock_enabled=True,
        profit_lock_arm_percent=Decimal("0.10"),
        profit_lock_giveback_fraction=Decimal("0.50"),
        profit_lock_giveback_fraction_after_throttle=Decimal("0.25"),
        pressure_enabled=True,
        pressure_min_for_entry=Decimal("0.15"),
        pressure_flip_exit_enabled=True,
        pressure_flip_exit_threshold=Decimal("0.25"),
        pressure_history_seconds=300,
        sell_fee_dollars=Decimal("0.02"),
        option_take_profit_percent=Decimal("0.15"),
        option_stop_loss_percent=Decimal("0.50"),
        option_min_dte=14,
        option_max_dte=45,
        option_type="BOTH",
        option_max_moneyness_percent=Decimal("0.15"),
        option_eod_close_time="15:50",
        option_min_hold_dte=7,
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
        # Peak 1.40 -> floor = 1.00 + 0.40*0.5 = 1.20
        decision = self.decide(price="1.30", peak="1.40")
        self.assertEqual(decision.action, "HOLD")

    def test_locks_in_the_gain_once_price_falls_back_to_the_floor(self):
        decision = self.decide(price="1.20", peak="1.40")
        self.assertEqual(decision.action, "PROFIT")
        self.assertEqual(decision.target_price, Decimal("1.20"))
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
        decision = self.decide(
            price="1.00",
            peak="1.10",
            quantity=1,
            sell_fee_dollars=Decimal("20"),
        )
        self.assertNotEqual(decision.action, "PROFIT")

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
            daily_batch_first_attempt_at=None,
            daily_batch_first_attempt_date=None,
            daily_batch_logged_empty_date=None,
            focus_symbol=None,
            focus_symbol_date=None,
            focus_logged_empty_date=None,
            focus_symbol_no_chain=set(),
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
        bot.select_focus_symbol = AutoTrader.select_focus_symbol.__get__(bot)
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

    def test_the_eod_close_backstop_overrides_the_retry_window(self):
        # Even a first attempt gives up once there is no real session
        # left to trade a batch built now.
        bot = self.bot(self.moment(15, 55))
        bot.refresh_daily_batch(self.moment(15, 55))
        self.assertEqual(bot.daily_batch_date, self.moment(15, 55).date())

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
        bot.select_focus_symbol(self.moment(9, 50))
        self.assertIsNone(
            bot.focus_symbol_date,
            "an unwarmed field at the lock time must stay retryable",
        )

    def test_no_focus_pick_at_closeout_gives_up(self):
        bot = self.bot(self.moment(15, 50))
        bot.daily_batch = ["AAA"]
        bot.select_focus_symbol(self.moment(15, 50))
        self.assertEqual(bot.focus_symbol_date, self.moment(15, 50).date())


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

    def bot(self, price=Decimal("168"), existing_contracts=None, fail=False):
        calls = {"option_contracts": []}
        contracts = [
            _fake_contract("MRNA", "MRNAC", "CALL", 170),
            _fake_contract("MRNA", "MRNAP", "PUT", 166),
        ]
        quotes = {
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
            strategy=SimpleNamespace(
                prices={"MRNA": price} if price else {},
                option_order_quantity=order_quantity,
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
        """select_focus_symbol retries every cycle until something
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
        focus_symbol="MRNA",
        existing_contracts=None,
        price=Decimal("168"),
        contracts=None,
        quotes=None,
        fail=False,
        buying_power=Decimal("300"),
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

        def order_quantity(limit_price, bp):
            cost = limit_price * 100
            if cost <= 0:
                return 0, cost
            return int(bp // cost), cost

        bot = SimpleNamespace(
            config=focus_config(),
            focus_symbol=focus_symbol,
            option_contracts=existing_contracts or [],
            option_discovery_attempted=set(),
            option_average_down_count={},
            option_last_buy_price={},
            cached_option_buying_power=buying_power,
            api=FakeApi(),
            option_contracts_state=SimpleNamespace(save=lambda *a, **k: None),
            strategy=SimpleNamespace(
                prices={focus_symbol: price} if price else {},
                option_order_quantity=order_quantity,
            ),
            focus_symbol_no_chain=set(),
            focus_contract_discovery_failures=0,
            focus_symbol_affordability_checked=None,
            last_stale_contract_prune=None,
        )
        bot.ensure_focus_symbol_contracts = (
            AutoTrader.ensure_focus_symbol_contracts.__get__(bot)
        )
        return bot, calls

    def test_discovers_the_wide_board_for_a_newly_locked_symbol(self):
        bot, calls = self.bot()
        bot.ensure_focus_symbol_contracts()
        self.assertEqual(calls["option_contracts"], ["MRNA"])
        self.assertEqual(len(bot.option_contracts), 2)
        self.assertEqual(
            {c["symbol"] for c in bot.option_contracts}, {"MRNAC", "MRNAP"}
        )

    def test_is_a_no_op_when_the_chain_already_exists(self):
        bot, calls = self.bot(
            existing_contracts=[
                _fake_contract("MRNA", "MRNAC", "CALL", 170)
            ]
        )
        bot.ensure_focus_symbol_contracts()
        self.assertEqual(calls["option_contracts"], [])

    def test_does_nothing_without_a_focus_symbol(self):
        bot, calls = self.bot(focus_symbol=None)
        bot.ensure_focus_symbol_contracts()
        self.assertEqual(calls["option_contracts"], [])

    def test_retries_next_cycle_when_price_is_not_yet_known(self):
        bot, calls = self.bot(price=None)
        bot.ensure_focus_symbol_contracts()
        self.assertEqual(calls["option_contracts"], [])

    def test_disabled_focus_mode_never_calls_the_api(self):
        bot, calls = self.bot()
        bot.config = focus_config(focus_mode_enabled=False)
        bot.ensure_focus_symbol_contracts()
        self.assertEqual(calls["option_contracts"], [])

    def test_an_api_failure_does_not_raise(self):
        bot, calls = self.bot(fail=True)
        bot.ensure_focus_symbol_contracts()  # must not raise
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
            bot.ensure_focus_symbol_contracts()
            self.assertEqual(bot.focus_symbol, "MRNA")  # not yet disqualified
        bot.ensure_focus_symbol_contracts()
        self.assertIn("MRNA", bot.focus_symbol_no_chain)
        self.assertIsNone(bot.focus_symbol)

    def test_a_transient_failure_does_not_disqualify_on_its_own(self):
        bot, calls = self.bot(fail=True)
        bot.ensure_focus_symbol_contracts()
        self.assertNotIn("MRNA", bot.focus_symbol_no_chain)
        self.assertEqual(bot.focus_symbol, "MRNA")

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
        bot.ensure_focus_symbol_contracts()
        self.assertEqual(bot.focus_contract_discovery_failures, 1)
        bot.ensure_focus_symbol_contracts()
        self.assertEqual(bot.focus_contract_discovery_failures, 0)
        self.assertEqual(bot.focus_symbol, "MRNA")

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
        bot.ensure_focus_symbol_contracts()
        self.assertIn("MRNA", bot.focus_symbol_no_chain)
        self.assertIsNone(bot.focus_symbol)

    def test_stays_locked_when_at_least_one_contract_is_affordable(self):
        bot, calls = self.bot(buying_power=Decimal("300"))
        bot.ensure_focus_symbol_contracts()
        self.assertEqual(bot.focus_symbol, "MRNA")
        self.assertNotIn("MRNA", bot.focus_symbol_no_chain)

    def test_affordability_is_checked_once_per_lock_not_every_cycle(self):
        bot, calls = self.bot(buying_power=Decimal("300"))
        bot.ensure_focus_symbol_contracts()
        quote_calls_after_first = len(calls["option_quotes"])
        bot.ensure_focus_symbol_contracts()
        self.assertEqual(len(calls["option_quotes"]), quote_calls_after_first)

    def test_stale_persisted_contracts_are_dropped_and_rediscovered(self):
        """Live incident ("we both know something is not working
        here"): NFLX locked, direction signals fired repeatedly,
        nothing ever entered. Direct inspection found NFLX's already-
        discovered contracts (persisted from a prior session) expired
        in 4 days - inside the 14-day floor - so _evaluate_option_
        entry's own DTE gate silently rejected every attempt as "too
        close to expiration," while ensure_focus_symbol_contracts
        treated their mere presence as "chain already exists" and
        never rediscovered a fresh, tradeable one.
        """
        stale = _fake_contract("MRNA", "MRNAC-OLD", "CALL", 170, dte=4)
        bot, calls = self.bot(existing_contracts=[stale])
        bot.ensure_focus_symbol_contracts()
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
            bot.ensure_focus_symbol_contracts()
        self.assertNotIn(stale, bot.option_contracts)

    def test_a_fresh_persisted_contract_is_left_alone(self):
        fresh = _fake_contract("MRNA", "MRNAC-OLD", "CALL", 170, dte=20)
        bot, calls = self.bot(existing_contracts=[fresh])
        bot.ensure_focus_symbol_contracts()
        self.assertIn(fresh, bot.option_contracts)
        self.assertEqual(calls["option_contracts"], [])


class RepickOnNoChainTests(unittest.TestCase):
    """select_focus_symbol's side of the same incident: when
    ensure_focus_symbol_contracts disqualifies the locked symbol (sets
    focus_symbol to None but leaves focus_symbol_date stamped for
    today), the account must not get stuck with no symbol - it must
    fall through and pick a replacement, excluding the disqualified
    name.
    """

    def bot(self, now):
        tz = ZoneInfo("America/New_York")
        bot = SimpleNamespace(
            config=focus_config(),
            daily_batch=["GRML", "MRNA"],
            focus_symbol=None,
            focus_symbol_date=now.date(),
            focus_logged_empty_date=None,
            focus_symbol_no_chain={"GRML"},
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
        bot.select_focus_symbol = AutoTrader.select_focus_symbol.__get__(bot)
        return bot

    def moment(self, hh, mm):
        return datetime(2026, 9, 21, hh, mm, tzinfo=ZoneInfo("America/New_York"))

    def test_repicks_a_replacement_after_disqualification(self):
        bot = self.bot(self.moment(11, 0))
        bot.select_focus_symbol(self.moment(11, 0))
        self.assertEqual(bot.focus_symbol, "MRNA")

    def test_never_repicks_the_disqualified_symbol_even_though_it_scores_higher(self):
        bot = self.bot(self.moment(11, 0))
        bot.select_focus_symbol(self.moment(11, 0))
        self.assertNotEqual(bot.focus_symbol, "GRML")


class ProactiveAffordabilityInSelectionTests(unittest.TestCase):
    """By request: "we want affordable options only so the stocks
    should also be focused like that" - checked proactively inside
    select_focus_symbol's candidate loop, so an established/liquid
    name whose cheapest contract still exceeds buying power never
    locks in the first place (live incident: GOOGL locked, then had
    to be reactively disqualified, wasting real trading time).
    """

    def bot(self, now, affordable):
        tz = ZoneInfo("America/New_York")
        bot = SimpleNamespace(
            config=focus_config(),
            daily_batch=["GOOGL", "MRNA"],
            focus_symbol=None,
            focus_symbol_date=None,
            focus_logged_empty_date=None,
            focus_symbol_no_chain=set(),
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
        bot.select_focus_symbol = AutoTrader.select_focus_symbol.__get__(bot)
        return bot

    def moment(self, hh, mm):
        return datetime(2026, 9, 21, hh, mm, tzinfo=ZoneInfo("America/New_York"))

    def test_an_unaffordable_higher_scoring_symbol_is_skipped(self):
        bot = self.bot(self.moment(9, 45), affordable={"GOOGL": False, "MRNA": True})
        bot.select_focus_symbol(self.moment(9, 45))
        self.assertEqual(bot.focus_symbol, "MRNA")

    def test_an_affordable_symbol_still_locks_normally(self):
        bot = self.bot(self.moment(9, 45), affordable={"GOOGL": True, "MRNA": True})
        bot.select_focus_symbol(self.moment(9, 45))
        self.assertEqual(bot.focus_symbol, "GOOGL")  # higher score wins when both fit

    def test_nothing_locks_when_every_candidate_is_unaffordable(self):
        bot = self.bot(self.moment(9, 45), affordable={"GOOGL": False, "MRNA": False})
        bot.select_focus_symbol(self.moment(9, 45))
        self.assertIsNone(bot.focus_symbol)


class StockSuspensionTests(unittest.TestCase):
    def test_focus_mode_suspends_new_stock_entries(self):
        bot = SimpleNamespace(config=focus_config())
        suspended = AutoTrader.stock_entries_suspended.__get__(bot)
        self.assertTrue(suspended())

    def test_stock_entries_resume_when_focus_mode_is_off(self):
        bot = SimpleNamespace(config=focus_config(focus_mode_enabled=False))
        suspended = AutoTrader.stock_entries_suspended.__get__(bot)
        self.assertFalse(suspended())


if __name__ == "__main__":
    unittest.main()
