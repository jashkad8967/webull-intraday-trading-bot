import time
import unittest
import unittest.mock
from collections import defaultdict, deque
from decimal import Decimal
from types import SimpleNamespace

from webull_bot.config import Settings
from webull_bot.strategy import TradingStrategy

from support.fixtures import StrategyConfigMixin


class LateCoreSessionTransitionTests(StrategyConfigMixin, unittest.TestCase):
    """Recalibrated stock_decision fresh-entry gating - by request:
    "start transitioning away from core hours strategy around 30
    minutes before end of core hours." Verifies profit_target_
    multiplier actually reaches stock_decision's target computation.
    """

    def test_profit_target_multiplier_widens_the_general_path_target(self):
        strategy = TradingStrategy(self.config())
        key = "STOCK:WIDE"
        strategy.metrics[key.split(":", 1)[1]] = {"spread_percent": 0.0}
        normal = strategy.stock_decision(
            key, Decimal("100"), 10, Decimal("90"),
        )
        widened = strategy.stock_decision(
            key, Decimal("100"), 10, Decimal("90"),
            profit_target_multiplier=Decimal("1.5"),
        )
        self.assertGreaterEqual(widened.target_price, normal.target_price)

    def test_stop_tighten_multiplier_moves_the_stop_price_closer_to_cost(self):
        """By request: "when we have a certain profit we should also
        not allow stops to be too low." A long position's stop is
        below cost - tightening (< 1) should move it UP, closer to
        cost, not further away.
        """
        strategy = TradingStrategy(self.config())
        key = "STOCK:TIGHT"
        strategy.metrics[key.split(":", 1)[1]] = {"spread_percent": 0.0}
        # Normal stop (0.15% floor) = 100*(1-0.0015) = 99.85; tightened
        # (0.5x) = 100*(1-0.00075) = 99.925 - a price between the two
        # only breaches the tightened stop.
        price = Decimal("99.9")
        normal = strategy.stock_decision(key, price, 10, Decimal("100"))
        tightened = strategy.stock_decision(
            key, price, 10, Decimal("100"),
            stop_tighten_multiplier=Decimal("0.5"),
        )
        self.assertNotEqual(normal.action, "LOSS")
        self.assertEqual(tightened.action, "LOSS")

    def test_tightening_the_stop_does_not_shrink_the_widened_target(self):
        """By request: "when we have a certain profit... not allow
        stops to be too low" combined with "let winners run further" -
        both apply simultaneously once significantly ahead. Regression:
        target used to be computed FROM the already-tightened stop_
        percent, so tightening (0.7x) and widening (1.5x) partially
        canceled each other out (0.7*1.5=1.05, barely wider than
        normal) instead of being independent effects.
        """
        strategy = TradingStrategy(self.config())
        key = "STOCK:BOTH"
        strategy.metrics[key.split(":", 1)[1]] = {"spread_percent": 0.0}
        normal = strategy.stock_decision(key, Decimal("100"), 10, Decimal("90"))
        both = strategy.stock_decision(
            key, Decimal("100"), 10, Decimal("90"),
            profit_target_multiplier=Decimal("1.5"),
            stop_tighten_multiplier=Decimal("0.7"),
        )
        widen_only = strategy.stock_decision(
            key, Decimal("100"), 10, Decimal("90"),
            profit_target_multiplier=Decimal("1.5"),
        )
        # The target with both effects active must match the target
        # with ONLY widening active - tightening the stop must not
        # quietly shrink it back down.
        self.assertEqual(both.target_price, widen_only.target_price)
        self.assertGreater(both.target_price, normal.target_price)


class BotOvertradingCapTests(unittest.TestCase):
    def test_rate_capped_blocks_after_configured_trades_per_hour(self):
        from collections import defaultdict, deque

        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(stock_max_trades_per_hour=2),
            trade_times=defaultdict(deque),
        )
        rate_capped = AutoTrader.rate_capped.__get__(fake_bot)
        key = "STOCK:CAPPED"
        # rate_capped prunes anything more than an hour old against the real
        # time.monotonic() clock, so timestamps must be pinned relative to a
        # frozen "now" - a literal 0.0 only stayed "recent" by coincidence of
        # how long this process/container had been up.
        with unittest.mock.patch("time.monotonic", return_value=0.0):
            self.assertFalse(rate_capped(key))
            fake_bot.trade_times[key].append(0.0)
            self.assertFalse(rate_capped(key))
            fake_bot.trade_times[key].append(0.0)
            self.assertTrue(rate_capped(key))

    def test_rate_cap_disabled_when_limit_is_zero(self):
        from collections import defaultdict, deque

        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(stock_max_trades_per_hour=0),
            trade_times=defaultdict(deque),
        )
        rate_capped = AutoTrader.rate_capped.__get__(fake_bot)
        key = "STOCK:UNCAPPED"
        for _ in range(50):
            fake_bot.trade_times[key].append(0.0)
        self.assertFalse(rate_capped(key))

    def test_record_realized_exit_tracks_pnl_and_loss_separately(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(sell_fee_dollars=Decimal("0.02")),
            daily_realized_pnl=Decimal("0"),
            daily_realized_loss=Decimal("0"),
            daily_pnl=SimpleNamespace(record=lambda *a, **k: None),
        )
        record = AutoTrader.record_realized_exit.__get__(fake_bot)
        record(Decimal("100"), Decimal("101"), 10)
        self.assertEqual(fake_bot.daily_realized_pnl, Decimal("9.98"))
        self.assertEqual(fake_bot.daily_realized_loss, Decimal("0"))
        record(Decimal("50"), Decimal("49"), 5)
        self.assertEqual(fake_bot.daily_realized_pnl, Decimal("4.96"))
        self.assertEqual(fake_bot.daily_realized_loss, Decimal("5.02"))

    def test_daily_loss_breaker_triggers_once_threshold_is_reached(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                daily_loss_circuit_breaker_enabled=True,
                daily_max_loss_fraction=Decimal("0.05"),
            ),
            daily_loss_breaker_triggered=False,
            daily_realized_loss=Decimal("10"),
            cached_account_value=Decimal("1000"),
            api=SimpleNamespace(close_all_positions=lambda loss_callback=None: []),
            wash_sales=SimpleNamespace(block=lambda *a, **k: None),
            last_account_refresh=0.0,
        )
        handle = AutoTrader.handle_daily_loss_breaker.__get__(fake_bot)
        # 5% of $1000 = $50 threshold.
        self.assertFalse(handle())
        fake_bot.daily_realized_loss = Decimal("60")
        self.assertTrue(handle())
        self.assertTrue(fake_bot.daily_loss_breaker_triggered)
        # Stays tripped even if realized loss is later read as lower.
        fake_bot.daily_realized_loss = Decimal("0")
        self.assertTrue(handle())

    def test_daily_loss_breaker_respects_the_enabled_flag(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                daily_loss_circuit_breaker_enabled=False,
                daily_max_loss_fraction=Decimal("0.05"),
            ),
            daily_loss_breaker_triggered=False,
            daily_realized_loss=Decimal("999"),
            cached_account_value=Decimal("1000"),
        )
        handle = AutoTrader.handle_daily_loss_breaker.__get__(fake_bot)
        self.assertFalse(handle())

    def test_daily_loss_breaker_is_disabled_by_default(self):
        """Was briefly enabled by default this session, then reverted
        by explicit request ("we do not want the circuit breaker to
        stop all trading") after it tripped live and halted the whole
        day's trading - see daily_loss_circuit_breaker_enabled's own
        config.py comment. The threshold field itself is left in place
        (still a valid fraction) in case it's re-enabled later.
        """
        settings = Settings()
        self.assertFalse(settings.daily_loss_circuit_breaker_enabled)
        self.assertGreater(settings.daily_max_loss_fraction, Decimal("0"))
        self.assertLessEqual(settings.daily_max_loss_fraction, Decimal("1"))

    def test_daily_loss_breaker_does_not_trip_without_a_cached_account_value(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                daily_loss_circuit_breaker_enabled=True,
                daily_max_loss_fraction=Decimal("0.05"),
            ),
            daily_loss_breaker_triggered=False,
            daily_realized_loss=Decimal("999"),
            cached_account_value=None,
        )
        handle = AutoTrader.handle_daily_loss_breaker.__get__(fake_bot)
        self.assertFalse(handle())


class AccountStateCashReserveTests(unittest.TestCase):
    def test_fresh_refresh_subtracts_the_reserve_once(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                account_refresh_seconds=Decimal("5"),
                min_cash_reserve_dollars=Decimal("10"),
            ),
            api=SimpleNamespace(
                balance=lambda: {},
                buying_power_from_balance=lambda balance: Decimal("120.00"),
                option_buying_power_from_balance=lambda balance: Decimal("0"),
                account_day_pnl_from_balance=lambda balance: Decimal("-1.50"),
                account_value_from_balance=lambda balance: Decimal("500.00"),
                positions=lambda: [{"symbol": "AAPL"}],
            ),
            cached_buying_power=Decimal("0"),
            cached_raw_buying_power=Decimal("0"),
            cached_account_day_pnl=None,
            cached_positions=[],
            last_account_refresh=0.0,
            short_selling_supported=True,
        )
        account_state = AutoTrader.account_state.__get__(fake_bot)

        with self.assertLogs("webull-bot", level="WARNING"):
            buying_power, positions = account_state()

        self.assertEqual(buying_power, Decimal("110.00"))
        self.assertEqual(positions, [{"symbol": "AAPL"}])
        self.assertEqual(fake_bot.cached_account_day_pnl, Decimal("-1.50"))
        self.assertEqual(fake_bot.cached_account_value, Decimal("500.00"))
        # The reserve reduces cached_buying_power (used for trading
        # sizing) but not cached_raw_buying_power (used for the
        # dashboard's displayed figures) - showing the reserved-down
        # number there reads as a silent gap against Webull's own app.
        self.assertEqual(fake_bot.cached_raw_buying_power, Decimal("120.00"))

    def test_cache_hit_does_not_subtract_the_reserve_again(self):
        """Regression test: account_state() only re-fetches from the
        broker every ACCOUNT_REFRESH_SECONDS - subtracting the reserve at
        every call site that reads the cached value (instead of once,
        right at the fresh fetch) would compound on every poll cycle
        within that window and drive spendable capital toward zero almost
        immediately.
        """
        from webull_bot.bot import AutoTrader

        calls = []
        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                account_refresh_seconds=Decimal("5"),
                min_cash_reserve_dollars=Decimal("10"),
            ),
            api=SimpleNamespace(
                buying_power=lambda: (calls.append(1), Decimal("120.00"))[1],
                positions=lambda: [],
            ),
            cached_buying_power=Decimal("0"),
            cached_positions=[],
            last_account_refresh=time.monotonic(),
        )
        account_state = AutoTrader.account_state.__get__(fake_bot)

        buying_power, _ = account_state()

        self.assertEqual(buying_power, Decimal("0"))
        self.assertEqual(calls, [])

    def test_reserve_never_takes_buying_power_below_zero(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                account_refresh_seconds=Decimal("5"),
                min_cash_reserve_dollars=Decimal("10"),
            ),
            api=SimpleNamespace(
                balance=lambda: {},
                buying_power_from_balance=lambda balance: Decimal("3.00"),
                option_buying_power_from_balance=lambda balance: Decimal("0"),
                account_day_pnl_from_balance=lambda balance: None,
                account_value_from_balance=lambda balance: None,
                positions=lambda: [],
            ),
            cached_buying_power=Decimal("0"),
            cached_raw_buying_power=Decimal("0"),
            cached_account_day_pnl=None,
            cached_positions=[],
            last_account_refresh=0.0,
            short_selling_supported=True,
        )
        account_state = AutoTrader.account_state.__get__(fake_bot)

        buying_power, _ = account_state()

        self.assertEqual(buying_power, Decimal("0"))


class ShortSellingEquityGateTests(unittest.TestCase):
    """account_state proactively disables short selling once cached
    account equity is seen under Webull's own $2,000 minimum, instead of
    spending a live order attempt (certain to be rejected) to discover
    it - see SHORT_SELLING_MIN_EQUITY.
    """

    @staticmethod
    def _fake_bot(account_value, short_selling_supported=True):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                account_refresh_seconds=Decimal("5"),
                min_cash_reserve_dollars=Decimal("10"),
            ),
            api=SimpleNamespace(
                balance=lambda: {},
                buying_power_from_balance=lambda balance: Decimal("120.00"),
                option_buying_power_from_balance=lambda balance: Decimal("0"),
                account_day_pnl_from_balance=lambda balance: Decimal("0"),
                account_value_from_balance=lambda balance: account_value,
                positions=lambda: [],
            ),
            cached_buying_power=Decimal("0"),
            cached_raw_buying_power=Decimal("0"),
            cached_account_day_pnl=None,
            cached_positions=[],
            last_account_refresh=0.0,
            short_selling_supported=short_selling_supported,
        )
        fake_bot.account_state = AutoTrader.account_state.__get__(fake_bot)
        return fake_bot

    def test_disables_short_selling_when_equity_is_under_the_minimum(self):
        fake_bot = self._fake_bot(Decimal("500.00"))
        with self.assertLogs("webull-bot", level="WARNING") as logs:
            fake_bot.account_state()
        self.assertFalse(fake_bot.short_selling_supported)
        self.assertIn("500.00", logs.output[0])

    def test_leaves_short_selling_enabled_when_equity_clears_the_minimum(self):
        fake_bot = self._fake_bot(Decimal("5000.00"))
        from webull_bot import bot as bot_module

        with unittest.mock.patch.object(bot_module.log, "warning") as warn:
            fake_bot.account_state()
        warn.assert_not_called()
        self.assertTrue(fake_bot.short_selling_supported)

    def test_does_not_relog_once_already_disabled(self):
        fake_bot = self._fake_bot(Decimal("500.00"), short_selling_supported=False)
        from webull_bot import bot as bot_module

        with unittest.mock.patch.object(bot_module.log, "warning") as warn:
            fake_bot.account_state()
        warn.assert_not_called()

    def test_no_crash_when_account_value_is_unavailable(self):
        fake_bot = self._fake_bot(None)
        fake_bot.account_state()  # must not raise
        self.assertTrue(fake_bot.short_selling_supported)


class IdleCashRelaxationTests(unittest.TestCase):
    def test_ramp_progress_is_zero_within_the_grace_period(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                idle_cash_relaxation_enabled=True,
                idle_cash_grace_seconds=300,
                idle_cash_ramp_seconds=1800,
            ),
            last_capital_deployed_at=time.monotonic() - 60,
        )
        progress = AutoTrader.idle_cash_ramp_progress.__get__(fake_bot)

        self.assertEqual(progress(Decimal("50")), Decimal("0"))

    def test_ramp_progress_is_zero_with_no_spendable_cash(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                idle_cash_relaxation_enabled=True,
                idle_cash_grace_seconds=300,
                idle_cash_ramp_seconds=1800,
            ),
            last_capital_deployed_at=time.monotonic() - 99999,
        )
        progress = AutoTrader.idle_cash_ramp_progress.__get__(fake_bot)

        self.assertEqual(progress(Decimal("0")), Decimal("0"))

    def test_ramp_progress_climbs_linearly_after_grace_then_caps_at_one(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                idle_cash_relaxation_enabled=True,
                idle_cash_grace_seconds=300,
                idle_cash_ramp_seconds=1800,
            ),
            last_capital_deployed_at=time.monotonic() - 300 - 900,
        )
        progress = AutoTrader.idle_cash_ramp_progress.__get__(fake_bot)

        self.assertAlmostEqual(float(progress(Decimal("50"))), 0.5, places=2)

        fake_bot.last_capital_deployed_at = time.monotonic() - 300 - 999999
        self.assertEqual(progress(Decimal("50")), Decimal("1"))

    def test_ramp_progress_disabled_by_config(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                idle_cash_relaxation_enabled=False,
                idle_cash_grace_seconds=0,
                idle_cash_ramp_seconds=1800,
            ),
            last_capital_deployed_at=time.monotonic() - 999999,
        )
        progress = AutoTrader.idle_cash_ramp_progress.__get__(fake_bot)

        self.assertEqual(progress(Decimal("50")), Decimal("0"))

    def test_record_trade_resets_the_idle_cash_timer_on_a_new_entry(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            last_trade={},
            last_exit_at={},
            trade_times=defaultdict(deque),
            working_orders={},
            status=SimpleNamespace(record_trade=lambda *a, **k: None),
            last_capital_deployed_at=time.monotonic() - 999999,
            position_opened_at={},
            symbol_pnl_history=defaultdict(deque),
            consecutive_exit_failures=defaultdict(int),
            submitted_order_ids_today=set(),
        )
        record_trade = AutoTrader.record_trade.__get__(fake_bot)

        record_trade("STOCK:AAPL", "order-1", "BUY")

        self.assertGreater(fake_bot.last_capital_deployed_at, time.monotonic() - 1)

    def test_a_volatility_scalp_buy_does_not_reset_the_general_strategys_idle_timer(self):
        """By request, after finding buying power sitting idle: the
        idle-cash ramp only ever loosens the GENERAL strategy's own
        entry gates, but a volatility-scalp fill (firing every few
        minutes) was resetting this same clock anyway, starving the
        general strategy's gates from ever relaxing even while its own
        capital pool sat unused for hours.
        """
        from webull_bot.bot import AutoTrader

        stale = time.monotonic() - 999999
        fake_bot = SimpleNamespace(
            last_trade={},
            last_exit_at={},
            trade_times=defaultdict(deque),
            working_orders={},
            status=SimpleNamespace(record_trade=lambda *a, **k: None),
            last_capital_deployed_at=stale,
            position_opened_at={},
            symbol_pnl_history=defaultdict(deque),
            consecutive_exit_failures=defaultdict(int),
            submitted_order_ids_today=set(),
        )
        record_trade = AutoTrader.record_trade.__get__(fake_bot)

        record_trade(
            "STOCK:GAUZ",
            "order-1",
            "BUY",
            counts_toward_idle_cash_ramp=False,
        )

        self.assertEqual(fake_bot.last_capital_deployed_at, stale)

    def test_record_trade_marks_a_fresh_option_buy_as_occurred_today(self):
        """By request: "first we want an option trade to occur, and
        the stock trading should start later" - see options_priority_
        window_active, which reads this flag to release the fresh-
        stock-entry hold as soon as a real option entry lands.
        """
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            last_trade={},
            last_exit_at={},
            trade_times=defaultdict(deque),
            working_orders={},
            status=SimpleNamespace(record_trade=lambda *a, **k: None),
            last_capital_deployed_at=time.monotonic(),
            position_opened_at={},
            symbol_pnl_history=defaultdict(deque),
            consecutive_exit_failures=defaultdict(int),
            submitted_order_ids_today=set(),
            option_entry_occurred_today=False,
        )
        record_trade = AutoTrader.record_trade.__get__(fake_bot)

        record_trade("OPTION:AAPL", "order-1", "BUY")

        self.assertTrue(fake_bot.option_entry_occurred_today)

    def test_record_trade_does_not_mark_a_stock_buy_as_an_option_entry(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            last_trade={},
            last_exit_at={},
            trade_times=defaultdict(deque),
            working_orders={},
            status=SimpleNamespace(record_trade=lambda *a, **k: None),
            last_capital_deployed_at=time.monotonic(),
            position_opened_at={},
            symbol_pnl_history=defaultdict(deque),
            consecutive_exit_failures=defaultdict(int),
            submitted_order_ids_today=set(),
            option_entry_occurred_today=False,
        )
        record_trade = AutoTrader.record_trade.__get__(fake_bot)

        record_trade("STOCK:AAPL", "order-1", "BUY")

        self.assertFalse(fake_bot.option_entry_occurred_today)

    def test_record_trade_does_not_mark_an_option_exit_as_an_entry(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            last_trade={},
            last_exit_at={},
            trade_times=defaultdict(deque),
            working_orders={},
            status=SimpleNamespace(record_trade=lambda *a, **k: None),
            last_capital_deployed_at=time.monotonic(),
            position_opened_at={},
            symbol_pnl_history=defaultdict(deque),
            consecutive_exit_failures=defaultdict(int),
            submitted_order_ids_today=set(),
            option_entry_occurred_today=False,
        )
        record_trade = AutoTrader.record_trade.__get__(fake_bot)

        record_trade("OPTION:AAPL", "order-1", "PROFIT")

        self.assertFalse(fake_bot.option_entry_occurred_today)

    def test_record_trade_does_not_reset_the_timer_on_an_exit(self):
        from webull_bot.bot import AutoTrader

        stale = time.monotonic() - 999999
        fake_bot = SimpleNamespace(
            last_trade={},
            last_exit_at={},
            trade_times=defaultdict(deque),
            working_orders={},
            status=SimpleNamespace(record_trade=lambda *a, **k: None),
            last_capital_deployed_at=stale,
            position_opened_at={},
            symbol_pnl_history=defaultdict(deque),
            submitted_order_ids_today=set(),
        )
        record_trade = AutoTrader.record_trade.__get__(fake_bot)

        record_trade("STOCK:AAPL", "order-1", "PROFIT", Decimal("10"), Decimal("1"), Decimal("9"))

        self.assertEqual(fake_bot.last_capital_deployed_at, stale)

    def test_record_trade_zeroes_a_stale_option_position_matched_by_full_symbol(self):
        """Live incident (RIVN): boost_stalled_positions read a stale,
        still-positive cached_positions quantity for a contract this
        exact exit had just closed, then tried to sell that stale
        amount, rejected by Webull ("excess of current holding
        quantity"). Only the STOCK side self-corrected cached_
        positions in place - this is the OPTION equivalent.
        """
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            last_trade={},
            last_exit_at={},
            trade_times=defaultdict(deque),
            working_orders={},
            status=SimpleNamespace(record_trade=lambda *a, **k: None),
            last_capital_deployed_at=0.0,
            recent_stop_losses=deque(),
            last_volatility_stop_loss_at={},
            position_opened_at={},
            symbol_pnl_history=defaultdict(deque),
            submitted_order_ids_today=set(),
            option_contracts=[],
            cached_positions=[
                {
                    "instrument_type": "OPTION",
                    "symbol": "RIVN260925P00015000",
                    "quantity": "3",
                }
            ],
        )
        record_trade = AutoTrader.record_trade.__get__(fake_bot)

        record_trade(
            "OPTION:RIVN260925P00015000", "order-1", "PROFIT", Decimal("0.30"),
            pnl=Decimal("1"), entry_price=Decimal("0.27"), quantity=Decimal("3"),
        )

        self.assertEqual(fake_bot.cached_positions[0]["quantity"], "0")

    def test_record_trade_zeroes_a_stale_option_position_matched_via_legs(self):
        """Same incident as above, but exercising the case where the
        broker's top-level "symbol" is the bare underlying (a
        documented Webull quirk) - only the legs identify the real
        contract, matched here against the cached, already-discovered
        option_contracts list (no live network call)."""
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            last_trade={},
            last_exit_at={},
            trade_times=defaultdict(deque),
            working_orders={},
            status=SimpleNamespace(record_trade=lambda *a, **k: None),
            last_capital_deployed_at=0.0,
            recent_stop_losses=deque(),
            last_volatility_stop_loss_at={},
            position_opened_at={},
            symbol_pnl_history=defaultdict(deque),
            submitted_order_ids_today=set(),
            option_contracts=[
                {
                    "symbol": "RIVN260925P00015000",
                    "underlying_symbol": "RIVN",
                    "option_type": "PUT",
                    "expiration_date": "2026-09-25",
                    "strike_price": "15",
                }
            ],
            cached_positions=[
                {
                    "instrument_type": "OPTION",
                    "symbol": "RIVN",
                    "quantity": "3",
                    "legs": [
                        {
                            "symbol": "RIVN",
                            "option_type": "PUT",
                            "option_expire_date": "2026-09-25",
                            "option_exercise_price": "15",
                        }
                    ],
                }
            ],
        )
        record_trade = AutoTrader.record_trade.__get__(fake_bot)

        record_trade(
            "OPTION:RIVN260925P00015000", "order-1", "PROFIT", Decimal("0.30"),
            pnl=Decimal("1"), entry_price=Decimal("0.27"), quantity=Decimal("3"),
        )

        self.assertEqual(fake_bot.cached_positions[0]["quantity"], "0")

    def test_record_trade_leaves_an_unrelated_option_position_untouched(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            last_trade={},
            last_exit_at={},
            trade_times=defaultdict(deque),
            working_orders={},
            status=SimpleNamespace(record_trade=lambda *a, **k: None),
            last_capital_deployed_at=0.0,
            recent_stop_losses=deque(),
            last_volatility_stop_loss_at={},
            position_opened_at={},
            symbol_pnl_history=defaultdict(deque),
            submitted_order_ids_today=set(),
            option_contracts=[
                {
                    "symbol": "RIVN260925P00015000",
                    "underlying_symbol": "RIVN",
                    "option_type": "PUT",
                    "expiration_date": "2026-09-25",
                    "strike_price": "15",
                }
            ],
            cached_positions=[
                {
                    "instrument_type": "OPTION",
                    "symbol": "AAPL260925C00200000",
                    "quantity": "1",
                }
            ],
        )
        record_trade = AutoTrader.record_trade.__get__(fake_bot)

        record_trade(
            "OPTION:RIVN260925P00015000", "order-1", "PROFIT", Decimal("0.30"),
            pnl=Decimal("1"), entry_price=Decimal("0.27"), quantity=Decimal("3"),
        )

        self.assertEqual(fake_bot.cached_positions[0]["quantity"], "1")


class OvernightHoldTests(unittest.TestCase):
    def test_overnight_hold_symbols_excludes_intraday_only_buckets(self):
        # By request: overnight hold is disabled by default now ("sell
        # all before eod" - see OVERNIGHT_HOLD_ENABLED's own comment),
        # but the bucket-filtering logic underneath is still there and
        # still worth covering - temporarily re-enable it, same pattern
        # test_overnight_hold_disabled_returns_empty_set already uses
        # for the opposite direction.
        import webull_bot.trading.universe.overnight_hold as overnight_hold_module
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            position_buckets={
                "AAPL": "popular",
                "KO": "PAIRS_LONG",
                "PEP": "PAIRS_SHORT",
                "GME": "MANUAL",
            },
            short_symbols=set(),
        )
        original = overnight_hold_module.OVERNIGHT_HOLD_ENABLED
        overnight_hold_module.OVERNIGHT_HOLD_ENABLED = True
        try:
            held = AutoTrader.overnight_hold_symbols.__get__(fake_bot)()
        finally:
            overnight_hold_module.OVERNIGHT_HOLD_ENABLED = original
        self.assertEqual(held, {"AAPL", "GME"})

    def test_overnight_hold_symbols_excludes_main_strategy_shorts(self):
        import webull_bot.trading.universe.overnight_hold as overnight_hold_module
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            position_buckets={"AAPL": "popular", "GME": "popular"},
            short_symbols={"GME"},
        )
        original = overnight_hold_module.OVERNIGHT_HOLD_ENABLED
        overnight_hold_module.OVERNIGHT_HOLD_ENABLED = True
        try:
            held = AutoTrader.overnight_hold_symbols.__get__(fake_bot)()
        finally:
            overnight_hold_module.OVERNIGHT_HOLD_ENABLED = original
        self.assertEqual(held, {"AAPL"})

    def test_overnight_hold_disabled_returns_empty_set(self):
        import webull_bot.trading.universe.overnight_hold as overnight_hold_module
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            position_buckets={"AAPL": "popular"}, short_symbols=set()
        )
        original = overnight_hold_module.OVERNIGHT_HOLD_ENABLED
        overnight_hold_module.OVERNIGHT_HOLD_ENABLED = False
        try:
            held = AutoTrader.overnight_hold_symbols.__get__(fake_bot)()
        finally:
            overnight_hold_module.OVERNIGHT_HOLD_ENABLED = original
        self.assertEqual(held, set())

    def test_exclude_pairs_symbols_removes_pairs_tickers(self):
        from webull_bot.bot import AutoTrader
        from webull_bot.pairs import PAIRS

        universe = ["AAPL", "MSFT"] + [symbol for pair in PAIRS for symbol in pair]
        remaining, excluded = AutoTrader.exclude_pairs_symbols(universe)
        self.assertEqual(set(excluded), {symbol for pair in PAIRS for symbol in pair})
        self.assertEqual(remaining, ["AAPL", "MSFT"])


class FractionalPreCloseSweepTests(unittest.TestCase):
    @staticmethod
    def _fake_bot(positions, quotes=None, config=None):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=config or SimpleNamespace(eod_retry_seconds=Decimal("10")),
            # Not 0.0 - the real time.monotonic() isn't guaranteed to
            # already be past eod_retry_seconds on every CI runner (same
            # class of flaky-clock bug fixed earlier this session).
            last_fractional_sweep=time.monotonic() - 999999,
            pending_stock_exits={"AAPL", "MSFT"},
            wash_sales=SimpleNamespace(block=lambda *a, **k: None),
            is_fractional_quantity=AutoTrader.is_fractional_quantity,
        )
        quotes = quotes or {}
        fake_bot.api = SimpleNamespace(
            positions=lambda: positions,
            stock_quote=lambda symbol: {"symbol": symbol, "price": str(quotes.get(symbol, "0"))},
            quote_price=lambda quote: Decimal(str(quote["price"])),
        )
        return fake_bot

    def test_closes_only_profitable_fractional_positions(self):
        """MSFT is a fractional position sitting at a profit (must close);
        BABA is fractional but underwater (must NOT be force-sold - it's
        already undefendable either way, and forcing a realized loss here
        isn't necessary the way capturing a gain is); FPE is whole-share
        (excluded regardless of P&L); the OPTION leg is never touched.
        """
        from webull_bot.bot import AutoTrader

        positions = [
            {"instrument_type": "EQUITY", "symbol": "MSFT", "quantity": "0.011", "cost_price": "493.23"},
            {"instrument_type": "EQUITY", "symbol": "BABA", "quantity": "0.109", "cost_price": "128.93"},
            {"instrument_type": "EQUITY", "symbol": "FPE", "quantity": "1", "cost_price": "17.81"},
            {"instrument_type": "OPTION", "symbol": "AAPL260918C00200000", "quantity": "0.5", "cost_price": "1.00"},
        ]
        quotes = {"MSFT": "497.33", "BABA": "122.20"}
        fake_bot = self._fake_bot(positions, quotes)
        calls = {}

        def fake_close_all_positions(instrument_types, loss_callback=None, exclude_symbols=None):
            calls["instrument_types"] = instrument_types
            calls["exclude_symbols"] = exclude_symbols
            return ["order-1"]

        fake_bot.api.close_all_positions = fake_close_all_positions
        sweep = AutoTrader.close_fractional_positions_before_core_close.__get__(fake_bot)

        sweep()

        self.assertEqual(calls["instrument_types"], {"EQUITY"})
        # Every EQUITY position except the profitable fractional one
        # (MSFT) is excluded - the losing fractional one (BABA) and the
        # whole-share one (FPE). The option leg is never considered at
        # all (not an EQUITY position).
        self.assertEqual(calls["exclude_symbols"], {"BABA", "FPE"})
        self.assertNotIn("MSFT", fake_bot.pending_stock_exits)
        self.assertIn("AAPL", fake_bot.pending_stock_exits)

    def test_noop_when_nothing_is_fractional(self):
        from webull_bot.bot import AutoTrader

        positions = [
            {"instrument_type": "EQUITY", "symbol": "FPE", "quantity": "1", "cost_price": "17.81"},
        ]
        fake_bot = self._fake_bot(positions)

        def fail_if_called(*a, **k):
            raise AssertionError("must not attempt to close anything")

        fake_bot.api.close_all_positions = fail_if_called
        sweep = AutoTrader.close_fractional_positions_before_core_close.__get__(fake_bot)

        sweep()  # must not raise

    def test_noop_when_every_fractional_position_is_underwater(self):
        from webull_bot.bot import AutoTrader

        positions = [
            {"instrument_type": "EQUITY", "symbol": "BABA", "quantity": "0.109", "cost_price": "128.93"},
        ]
        fake_bot = self._fake_bot(positions, {"BABA": "122.20"})

        def fail_if_called(*a, **k):
            raise AssertionError("must not attempt to close anything")

        fake_bot.api.close_all_positions = fail_if_called
        sweep = AutoTrader.close_fractional_positions_before_core_close.__get__(fake_bot)

        sweep()  # must not raise

    def test_throttled_within_eod_retry_seconds(self):
        from webull_bot.bot import AutoTrader

        positions = [
            {"instrument_type": "EQUITY", "symbol": "MSFT", "quantity": "0.011", "cost_price": "493.23"},
        ]
        fake_bot = self._fake_bot(positions, {"MSFT": "497.33"})
        fake_bot.last_fractional_sweep = time.monotonic()
        calls = []
        fake_bot.api.close_all_positions = lambda *a, **k: (calls.append(1), [])[1]
        sweep = AutoTrader.close_fractional_positions_before_core_close.__get__(fake_bot)

        sweep()

        self.assertEqual(calls, [])

    def test_survives_a_broker_failure(self):
        from webull_bot.bot import AutoTrader

        positions = [
            {"instrument_type": "EQUITY", "symbol": "MSFT", "quantity": "0.011", "cost_price": "493.23"},
        ]
        fake_bot = self._fake_bot(positions, {"MSFT": "497.33"})

        def boom(*a, **k):
            raise RuntimeError("boom")

        fake_bot.api.close_all_positions = boom
        sweep = AutoTrader.close_fractional_positions_before_core_close.__get__(fake_bot)

        sweep()  # must not raise

    def test_survives_a_quote_failure_for_one_symbol(self):
        """A quote failure for one fractional symbol must not stop the
        sweep from still closing others that quoted fine.
        """
        from webull_bot.bot import AutoTrader

        positions = [
            {"instrument_type": "EQUITY", "symbol": "MSFT", "quantity": "0.011", "cost_price": "493.23"},
            {"instrument_type": "EQUITY", "symbol": "BROKEN", "quantity": "0.02", "cost_price": "10.00"},
        ]
        fake_bot = self._fake_bot(positions, {"MSFT": "497.33"})

        def flaky_quote(symbol):
            if symbol == "BROKEN":
                raise RuntimeError("quote unavailable")
            return {"symbol": symbol, "price": "497.33"}

        fake_bot.api.stock_quote = flaky_quote
        calls = {}
        fake_bot.api.close_all_positions = lambda instrument_types, loss_callback=None, exclude_symbols=None: (
            calls.update(exclude_symbols=exclude_symbols) or ["order-1"]
        )
        sweep = AutoTrader.close_fractional_positions_before_core_close.__get__(fake_bot)

        sweep()

        self.assertEqual(calls["exclude_symbols"], {"BROKEN"})


class ExtendedHoursProfitSweepTests(unittest.TestCase):
    """close_profitable_positions_during_extended_hours - by request,
    after pre-market losses: "capturing any profits to close out the
    day as much as possible" outside core hours. Same shape as
    FractionalPreCloseSweepTests but for ALL equity positions (not
    just fractional ones), on its own dedicated cadence.
    """

    @staticmethod
    def _fake_bot(positions, quotes=None, config=None):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=config
            or SimpleNamespace(extended_hours_profit_sweep_seconds=60),
            last_extended_hours_profit_sweep=time.monotonic() - 999999,
            pending_stock_exits={"AAPL", "MSFT"},
            wash_sales=SimpleNamespace(block=lambda *a, **k: None),
            extended_hours_fractional_skip_logged=set(),
            is_fractional_quantity=AutoTrader.is_fractional_quantity,
        )
        quotes = quotes or {}
        fake_bot.api = SimpleNamespace(
            positions=lambda: positions,
            stock_quote=lambda symbol: {"symbol": symbol, "price": str(quotes.get(symbol, "0"))},
            quote_price=lambda quote: Decimal(str(quote["price"])),
        )
        return fake_bot

    def test_closes_only_profitable_equity_positions(self):
        from webull_bot.bot import AutoTrader

        positions = [
            {"instrument_type": "EQUITY", "symbol": "MSFT", "quantity": "1", "cost_price": "493.23"},
            {"instrument_type": "EQUITY", "symbol": "BABA", "quantity": "1", "cost_price": "128.93"},
            {"instrument_type": "OPTION", "symbol": "AAPL260918C00200000", "quantity": "0.5", "cost_price": "1.00"},
        ]
        quotes = {"MSFT": "497.33", "BABA": "122.20"}
        fake_bot = self._fake_bot(positions, quotes)
        calls = {}

        def fake_close_all_positions(instrument_types, loss_callback=None, exclude_symbols=None):
            calls["instrument_types"] = instrument_types
            calls["exclude_symbols"] = exclude_symbols
            return ["order-1"]

        fake_bot.api.close_all_positions = fake_close_all_positions
        sweep = AutoTrader.close_profitable_positions_during_extended_hours.__get__(fake_bot)

        sweep()

        self.assertEqual(calls["instrument_types"], {"EQUITY"})
        self.assertEqual(calls["exclude_symbols"], {"BABA"})
        self.assertNotIn("MSFT", fake_bot.pending_stock_exits)

    def test_skips_a_fractional_position_instead_of_retrying_a_guaranteed_rejection(self):
        """Live incident: UBER's extended-hours close order was rejected
        with OPENAPI_FRACT_ONLT_CORE_TIME (fractional orders are only
        accepted during core hours) every ~90s cycle for hours straight
        - this function only ever runs outside core hours, so a
        fractional-quantity position here will ALWAYS hit this same
        rejection. It should be skipped outright, not retried.
        """
        from webull_bot.bot import AutoTrader

        positions = [
            {"instrument_type": "EQUITY", "symbol": "MSFT", "quantity": "1", "cost_price": "493.23"},
            {"instrument_type": "EQUITY", "symbol": "UBER", "quantity": "0.5", "cost_price": "70.00"},
        ]
        quotes = {"MSFT": "497.33", "UBER": "75.00"}
        fake_bot = self._fake_bot(positions, quotes)
        calls = {}

        def fake_close_all_positions(instrument_types, loss_callback=None, exclude_symbols=None):
            calls["exclude_symbols"] = exclude_symbols
            return ["order-1"]

        fake_bot.api.close_all_positions = fake_close_all_positions
        sweep = AutoTrader.close_profitable_positions_during_extended_hours.__get__(fake_bot)

        sweep()

        # UBER never even reaches close_all_positions as a symbol to
        # close - it's excluded, same as an underwater position would be.
        self.assertIn("UBER", calls["exclude_symbols"])
        self.assertIn("UBER", fake_bot.extended_hours_fractional_skip_logged)

    def test_noop_when_every_position_is_underwater(self):
        from webull_bot.bot import AutoTrader

        positions = [
            {"instrument_type": "EQUITY", "symbol": "BABA", "quantity": "1", "cost_price": "128.93"},
        ]
        fake_bot = self._fake_bot(positions, {"BABA": "122.20"})

        def fail_if_called(*a, **k):
            raise AssertionError("must not attempt to close anything")

        fake_bot.api.close_all_positions = fail_if_called
        sweep = AutoTrader.close_profitable_positions_during_extended_hours.__get__(fake_bot)

        sweep()  # must not raise

    def test_throttled_within_its_own_sweep_interval(self):
        from webull_bot.bot import AutoTrader

        positions = [
            {"instrument_type": "EQUITY", "symbol": "MSFT", "quantity": "1", "cost_price": "493.23"},
        ]
        fake_bot = self._fake_bot(positions, {"MSFT": "497.33"})
        fake_bot.last_extended_hours_profit_sweep = time.monotonic()
        calls = []
        fake_bot.api.close_all_positions = lambda *a, **k: (calls.append(1), [])[1]
        sweep = AutoTrader.close_profitable_positions_during_extended_hours.__get__(fake_bot)

        sweep()

        self.assertEqual(calls, [])

    def test_survives_a_broker_failure(self):
        from webull_bot.bot import AutoTrader

        positions = [
            {"instrument_type": "EQUITY", "symbol": "MSFT", "quantity": "1", "cost_price": "493.23"},
        ]
        fake_bot = self._fake_bot(positions, {"MSFT": "497.33"})

        def boom(*a, **k):
            raise RuntimeError("boom")

        fake_bot.api.close_all_positions = boom
        sweep = AutoTrader.close_profitable_positions_during_extended_hours.__get__(fake_bot)

        sweep()  # must not raise
