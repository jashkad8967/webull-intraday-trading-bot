import time
import unittest
import unittest.mock
from collections import defaultdict, deque
from decimal import Decimal
from types import SimpleNamespace

from webull_bot.strategy import TradingStrategy


class FreshEntryBlackoutActiveTests(unittest.TestCase):
    """fresh_entry_blackout_active - by request, after live evidence
    (WNW/WKHS stopping out shortly after core hours ended): a fresh
    entry this close to the bell has almost no runway to reach its
    target before conditions change. Blocks fresh entries only -
    averaging down and every exit path are unaffected (this function
    only feeds the fresh-entry gates, never the averaging-down or exit
    decision paths).
    """

    def test_active_just_before_close(self):
        from webull_bot.bot import AutoTrader

        self.assertTrue(
            AutoTrader.fresh_entry_blackout_active(5, 15, core_session_active=True)
        )

    def test_inactive_with_plenty_of_runway_left(self):
        from webull_bot.bot import AutoTrader

        self.assertFalse(
            AutoTrader.fresh_entry_blackout_active(120, 15, core_session_active=True)
        )

    def test_inactive_right_at_the_boundary(self):
        from webull_bot.bot import AutoTrader

        self.assertFalse(
            AutoTrader.fresh_entry_blackout_active(15, 15, core_session_active=True)
        )

    def test_inactive_outside_core_hours_even_with_negative_minutes(self):
        """A negative minutes_until_close (core hours already ended)
        must not itself trigger this - the separate "only established/
        popular symbols trade outside core hours" gate already covers
        that case.
        """
        from webull_bot.bot import AutoTrader

        self.assertFalse(
            AutoTrader.fresh_entry_blackout_active(
                -30, 15, core_session_active=False
            )
        )

    def test_inactive_when_core_session_is_not_active_regardless_of_minutes(self):
        from webull_bot.bot import AutoTrader

        self.assertFalse(
            AutoTrader.fresh_entry_blackout_active(2, 15, core_session_active=False)
        )


class OptionsPriorityWindowActiveTests(unittest.TestCase):
    """options_priority_window_active - by request: "keep it separate,
    all the bp should be for option, then at 9am cst whatever is
    remaining can be used for stocks." Blocks fresh stock entries only
    for the first priority_minutes of the session, so options get
    first claim on the account's buying power.
    """

    def test_active_right_after_open(self):
        from webull_bot.bot import AutoTrader

        self.assertTrue(
            AutoTrader.options_priority_window_active(
                5, 30, core_session_active=True
            )
        )

    def test_inactive_once_the_window_has_passed(self):
        from webull_bot.bot import AutoTrader

        self.assertFalse(
            AutoTrader.options_priority_window_active(
                45, 30, core_session_active=True
            )
        )

    def test_inactive_right_at_the_boundary(self):
        from webull_bot.bot import AutoTrader

        self.assertFalse(
            AutoTrader.options_priority_window_active(
                30, 30, core_session_active=True
            )
        )

    def test_inactive_outside_core_hours_even_with_negative_minutes(self):
        from webull_bot.bot import AutoTrader

        self.assertFalse(
            AutoTrader.options_priority_window_active(
                -5, 30, core_session_active=False
            )
        )

    def test_inactive_when_core_session_is_not_active_regardless_of_minutes(self):
        from webull_bot.bot import AutoTrader

        self.assertFalse(
            AutoTrader.options_priority_window_active(
                5, 30, core_session_active=False
            )
        )


class ManualTouchPauseTests(unittest.TestCase):
    """By request: "when i touch a stock stop doing anything with it
    while i am there." _manual_touch_active is module-level (not a
    self-method) so every repricer/escalation call site works
    unchanged against the many existing bare-SimpleNamespace fixtures
    - see its own docstring.
    """

    def test_true_within_the_pause_window(self):
        from webull_bot.bot import _manual_touch_active

        fake_bot = SimpleNamespace(
            manual_touch_at={"AAPL": 100.0},
            config=SimpleNamespace(manual_touch_pause_seconds=300),
        )
        with unittest.mock.patch("time.monotonic", return_value=200.0):
            self.assertTrue(_manual_touch_active(fake_bot, "AAPL"))

    def test_false_once_the_pause_window_elapses(self):
        from webull_bot.bot import _manual_touch_active

        fake_bot = SimpleNamespace(
            manual_touch_at={"AAPL": 100.0},
            config=SimpleNamespace(manual_touch_pause_seconds=300),
        )
        with unittest.mock.patch("time.monotonic", return_value=500.0):
            self.assertFalse(_manual_touch_active(fake_bot, "AAPL"))

    def test_false_for_a_symbol_never_touched(self):
        from webull_bot.bot import _manual_touch_active

        fake_bot = SimpleNamespace(
            manual_touch_at={},
            config=SimpleNamespace(manual_touch_pause_seconds=300),
        )
        self.assertFalse(_manual_touch_active(fake_bot, "AAPL"))

    def test_false_when_the_fixture_never_set_manual_touch_at(self):
        """Every pre-existing test fixture across this file - a bare
        SimpleNamespace bound to a single AutoTrader method - has no
        manual_touch_at attribute at all. Must default to "not
        touched," not raise.
        """
        from webull_bot.bot import _manual_touch_active

        fake_bot = SimpleNamespace(config=SimpleNamespace())
        self.assertFalse(_manual_touch_active(fake_bot, "AAPL"))

    def test_record_trade_stamps_manual_touch_on_manual_buy_and_sell(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            last_trade={},
            submitted_order_ids_today=set(),
            last_exit_at={},
            position_opened_at={},
            symbol_pnl_history=defaultdict(list),
            recent_stop_losses=deque(),
            last_volatility_stop_loss_at={},
            manual_touch_at={},
            trade_times=defaultdict(deque),
            working_orders={},
            status=SimpleNamespace(record_trade=lambda *a, **k: None),
            consecutive_exit_failures=defaultdict(int),
        )
        record_trade = AutoTrader.record_trade.__get__(fake_bot)
        with unittest.mock.patch("time.monotonic", return_value=42.0):
            record_trade("STOCK:AAPL", "order-1", "MANUAL_BUY")
        self.assertEqual(fake_bot.manual_touch_at["AAPL"], 42.0)
        with unittest.mock.patch("time.monotonic", return_value=99.0):
            record_trade("STOCK:AAPL", "order-2", "MANUAL_SELL")
        self.assertEqual(fake_bot.manual_touch_at["AAPL"], 99.0)

    def test_record_trade_does_not_stamp_a_bot_driven_trade(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            last_trade={},
            submitted_order_ids_today=set(),
            last_exit_at={},
            position_opened_at={},
            symbol_pnl_history=defaultdict(list),
            recent_stop_losses=deque(),
            last_volatility_stop_loss_at={},
            manual_touch_at={},
            trade_times=defaultdict(deque),
            working_orders={},
            status=SimpleNamespace(record_trade=lambda *a, **k: None),
            consecutive_exit_failures=defaultdict(int),
        )
        record_trade = AutoTrader.record_trade.__get__(fake_bot)
        record_trade("STOCK:AAPL", "order-1", "BUY")
        self.assertEqual(fake_bot.manual_touch_at, {})


class BrokerConflictTests(unittest.TestCase):
    def test_is_broker_position_conflict_matches_reverse_rejection(self):
        from webull_bot.bot import AutoTrader

        self.assertTrue(
            AutoTrader.is_broker_position_conflict(
                RuntimeError(
                    "HTTP Status: 417, Code: "
                    "OAUTH_OPENAPI_ORDER_NOT_SUPPORT_REVERSE_OPTION, Msg: "
                    "This order cannot be entered because it will reverse "
                    "an existing position."
                )
            )
        )
        self.assertFalse(
            AutoTrader.is_broker_position_conflict(RuntimeError("timeout"))
        )

    def test_handle_broker_conflict_clears_tracking_and_blacklists_symbol(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            broker_conflict_symbols=set(),
            pending_stock_exits={"ASHR"},
            pending_option_exits=set(),
            stop_exit_submitted={"ASHR": 123.0},
            stop_loss_escalated={"ASHR"},
            stop_condition_since={"ASHR": 100.0},
        )
        handle = AutoTrader.handle_broker_conflict.__get__(fake_bot)

        handle("ASHR", RuntimeError("reverse position"))

        self.assertIn("ASHR", fake_bot.broker_conflict_symbols)
        self.assertNotIn("ASHR", fake_bot.pending_stock_exits)
        self.assertNotIn("ASHR", fake_bot.stop_exit_submitted)
        self.assertNotIn("ASHR", fake_bot.stop_loss_escalated)
        self.assertNotIn("ASHR", fake_bot.stop_condition_since)

    def test_is_fractional_trading_not_enabled_matches_account_agreement_rejection(self):
        from webull_bot.bot import AutoTrader

        self.assertTrue(
            AutoTrader.is_fractional_trading_not_enabled(
                RuntimeError(
                    "HTTP Status: 417, Code: "
                    "OAUTH_OPENAPI_OPENAPI_FRACT_VERSION2_ACCOUNT_NOT_TRADE, "
                    "Msg: https://sp.webull.com/agreement/third-party"
                )
            )
        )
        self.assertFalse(
            AutoTrader.is_fractional_trading_not_enabled(RuntimeError("timeout"))
        )

    def test_handle_fractional_trading_not_enabled_disables_it_once(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(fractional_trading_enabled=True)
        handle = AutoTrader.handle_fractional_trading_not_enabled.__get__(fake_bot)

        handle(RuntimeError("FRACT_VERSION2_ACCOUNT_NOT_TRADE"))
        self.assertFalse(fake_bot.fractional_trading_enabled)

        # A second rejection while already disabled shouldn't re-log/re-flip
        # anything - just a no-op guard.
        handle(RuntimeError("FRACT_VERSION2_ACCOUNT_NOT_TRADE"))
        self.assertFalse(fake_bot.fractional_trading_enabled)

    def test_is_fractional_ticker_unsupported_matches_per_security_rejection(self):
        from webull_bot.bot import AutoTrader

        self.assertTrue(
            AutoTrader.is_fractional_ticker_unsupported(
                RuntimeError(
                    "HTTP Status: 417, Code: "
                    "OAUTH_OPENAPI_FRACT_TICKER_DONT_SUPPORT_TRADE, Msg: "
                    "This security is not available for fractional shares "
                    "trading."
                )
            )
        )
        self.assertFalse(
            AutoTrader.is_fractional_ticker_unsupported(RuntimeError("timeout"))
        )
        # Distinct from the account-wide rejection - must not cross-match.
        self.assertFalse(
            AutoTrader.is_fractional_ticker_unsupported(
                RuntimeError("FRACT_VERSION2_ACCOUNT_NOT_TRADE")
            )
        )

    def test_handle_fractional_ticker_unsupported_blacklists_only_that_symbol(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(fractional_unsupported_symbols=set())
        handle = AutoTrader.handle_fractional_ticker_unsupported.__get__(fake_bot)

        handle("XHG", RuntimeError("FRACT_TICKER_DONT_SUPPORT_TRADE"))

        self.assertEqual(fake_bot.fractional_unsupported_symbols, {"XHG"})

        # A different symbol is unaffected.
        self.assertNotIn("AAPL", fake_bot.fractional_unsupported_symbols)

    def test_handle_fractional_ticker_unsupported_is_idempotent(self):
        from webull_bot.bot import AutoTrader
        from webull_bot import bot as bot_module

        fake_bot = SimpleNamespace(fractional_unsupported_symbols={"XHG"})
        handle = AutoTrader.handle_fractional_ticker_unsupported.__get__(fake_bot)

        # No new log line for a symbol already known-unsupported - would
        # otherwise spam a warning every cycle forever.
        with unittest.mock.patch.object(bot_module.log, "warning") as warn:
            handle("XHG", RuntimeError("FRACT_TICKER_DONT_SUPPORT_TRADE"))
        warn.assert_not_called()

    def test_handle_otc_extended_hours_unsupported_blacklists_only_that_symbol(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(otc_extended_hours_unsupported_symbols=set())
        handle = AutoTrader.handle_otc_extended_hours_unsupported.__get__(fake_bot)

        handle("NLST", RuntimeError("OTC_TICKER_NOT_SUPPORT_X_P"))

        self.assertEqual(fake_bot.otc_extended_hours_unsupported_symbols, {"NLST"})
        self.assertNotIn("AAPL", fake_bot.otc_extended_hours_unsupported_symbols)

    def test_handle_otc_extended_hours_unsupported_is_idempotent(self):
        from webull_bot.bot import AutoTrader
        from webull_bot.trading.handlers import otc_extended_hours_handler

        fake_bot = SimpleNamespace(
            otc_extended_hours_unsupported_symbols={"NLST"}
        )
        handle = AutoTrader.handle_otc_extended_hours_unsupported.__get__(fake_bot)

        with unittest.mock.patch.object(
            otc_extended_hours_handler.log, "warning"
        ) as warn:
            handle("NLST", RuntimeError("OTC_TICKER_NOT_SUPPORT_X_P"))
        warn.assert_not_called()

    def test_is_short_selling_unsupported_matches_sub_2k_equity_rejection(self):
        from webull_bot.bot import AutoTrader

        self.assertTrue(
            AutoTrader.is_short_selling_unsupported(
                RuntimeError(
                    "HTTP Status: 417, Code: "
                    "OAUTH_OPENAPI_NEW_NO_POSITION_MARGIN_ACCOUNT_CAN_NOT_"
                    "SELL_SHORT_FOR_LT_2K, Msg: You currently have no open "
                    "positions in MZZ. Short selling is not permitted for "
                    "accounts under $2,000 in equity or with an overdue "
                    "Intraday Margin Deficit (IMD)."
                )
            )
        )
        self.assertFalse(
            AutoTrader.is_short_selling_unsupported(RuntimeError("timeout"))
        )
        # Distinct from the other account-wide/per-security rejections -
        # must not cross-match.
        self.assertFalse(
            AutoTrader.is_short_selling_unsupported(
                RuntimeError("FRACT_VERSION2_ACCOUNT_NOT_TRADE")
            )
        )

    def test_handle_short_selling_unsupported_disables_it_once(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(short_selling_supported=True)
        handle = AutoTrader.handle_short_selling_unsupported.__get__(fake_bot)

        handle(RuntimeError("CAN_NOT_SELL_SHORT_FOR_LT_2K"))
        self.assertFalse(fake_bot.short_selling_supported)

        # A second rejection while already disabled shouldn't re-log/
        # re-flip anything - just a no-op guard.
        from webull_bot import bot as bot_module

        with unittest.mock.patch.object(bot_module.log, "error") as err:
            handle(RuntimeError("CAN_NOT_SELL_SHORT_FOR_LT_2K"))
        err.assert_not_called()
        self.assertFalse(fake_bot.short_selling_supported)

    def test_is_symbol_restricted_to_closing_only_matches_the_broker_rejection(self):
        from webull_bot.bot import AutoTrader

        self.assertTrue(
            AutoTrader.is_symbol_restricted_to_closing_only(
                RuntimeError(
                    "HTTP Status: 417, Code: "
                    "OAUTH_OPENAPI_CAN_NOT_CREATE_A_OPEN_ORDER, Msg: This "
                    "symbol is restricted to closing orders only."
                )
            )
        )
        self.assertFalse(
            AutoTrader.is_symbol_restricted_to_closing_only(RuntimeError("timeout"))
        )
        self.assertFalse(
            AutoTrader.is_symbol_restricted_to_closing_only(
                RuntimeError("CAN_NOT_SELL_SHORT_FOR_LT_2K")
            )
        )

    def test_handle_symbol_restricted_to_closing_only_blocks_just_that_symbol(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(entry_restricted_symbols=set())
        handle = AutoTrader.handle_symbol_restricted_to_closing_only.__get__(fake_bot)

        handle("RFAI", RuntimeError("CAN_NOT_CREATE_A_OPEN_ORDER"))
        self.assertIn("RFAI", fake_bot.entry_restricted_symbols)
        self.assertEqual(len(fake_bot.entry_restricted_symbols), 1)

        # A second rejection for the same symbol is a silent no-op.
        from webull_bot import bot as bot_module

        with unittest.mock.patch.object(bot_module.log, "warning") as warn:
            handle("RFAI", RuntimeError("CAN_NOT_CREATE_A_OPEN_ORDER"))
        warn.assert_not_called()

    def test_restriction_blocks_entries_but_not_broker_conflict_symbols(self):
        """entry_restricted_symbols must be a distinct set from
        broker_conflict_symbols - the latter skips a symbol's exit
        management entirely too (see trade_stocks' top-of-loop check),
        which would be exactly backwards for a "closing orders only"
        restriction: exits must keep working normally.
        """
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            entry_restricted_symbols=set(), broker_conflict_symbols=set()
        )
        handle = AutoTrader.handle_symbol_restricted_to_closing_only.__get__(fake_bot)
        handle("RFAI", RuntimeError("CAN_NOT_CREATE_A_OPEN_ORDER"))
        self.assertIn("RFAI", fake_bot.entry_restricted_symbols)
        self.assertNotIn("RFAI", fake_bot.broker_conflict_symbols)


class StopLossGuardTests(unittest.TestCase):
    @staticmethod
    def _fake_bot(recent_stop_losses=None, **config_overrides):
        from webull_bot.bot import AutoTrader

        defaults = dict(
            stop_loss_guard_enabled=True,
            stop_loss_guard_trade_limit=4,
            stop_loss_guard_lookback_seconds=1200,
            stop_loss_guard_cooldown_seconds=600,
        )
        defaults.update(config_overrides)
        config = SimpleNamespace(**defaults)
        fake_bot = SimpleNamespace(
            config=config,
            recent_stop_losses=deque(recent_stop_losses or []),
            stop_loss_guard_until=0.0,
        )
        return AutoTrader.stop_loss_guard_active.__get__(fake_bot), fake_bot

    def test_passes_through_below_the_trade_limit(self):
        now = time.monotonic()
        guard, _ = self._fake_bot([now - 10, now - 20, now - 30])
        self.assertFalse(guard())

    def test_trips_at_the_trade_limit_within_lookback(self):
        now = time.monotonic()
        guard, fake_bot = self._fake_bot([now - 10, now - 20, now - 30, now - 40])

        self.assertTrue(guard())
        # Once tripped, stays active for the cooldown even without any new
        # stop-losses - the whole point is to pause NEW entries, so the
        # very next call (same cycle or the next one) must still see it.
        self.assertTrue(guard())

    def test_old_stop_losses_outside_lookback_dont_count(self):
        now = time.monotonic()
        guard, _ = self._fake_bot(
            [now - 5000, now - 4000, now - 3000, now - 2000],
            stop_loss_guard_lookback_seconds=1200,
        )
        self.assertFalse(guard())

    def test_disabled_by_config(self):
        now = time.monotonic()
        guard, _ = self._fake_bot(
            [now - 10, now - 20, now - 30, now - 40],
            stop_loss_guard_enabled=False,
        )
        self.assertFalse(guard())

    def test_resumes_automatically_after_the_cooldown_elapses(self):
        """Unlike handle_portfolio_circuit_breaker (which stays paused
        until re-evaluated/manually resumed), the stop-loss guard is a
        pure rolling-window check - once the tripping stops age out of
        the lookback (which happens well before a short cooldown elapses
        in this test), it clears on its own.
        """
        from webull_bot.bot import AutoTrader

        config = SimpleNamespace(
            stop_loss_guard_enabled=True,
            stop_loss_guard_trade_limit=4,
            stop_loss_guard_lookback_seconds=1,
            stop_loss_guard_cooldown_seconds=1,
        )
        now = time.monotonic()
        fake_bot = SimpleNamespace(
            config=config,
            recent_stop_losses=deque([now - 0.9, now - 0.8, now - 0.7, now - 0.6]),
            stop_loss_guard_until=0.0,
        )
        guard = AutoTrader.stop_loss_guard_active.__get__(fake_bot)
        self.assertTrue(guard())

        # Simulate the cooldown having elapsed and the old stops now being
        # well outside the (short, 1s) lookback window too.
        fake_bot.stop_loss_guard_until = time.monotonic() - 10
        fake_bot.recent_stop_losses = deque(
            t - 20 for t in fake_bot.recent_stop_losses
        )
        self.assertFalse(guard())

    def test_record_trade_appends_a_stop_but_not_other_actions(self):
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
        )
        record_trade = AutoTrader.record_trade.__get__(fake_bot)

        record_trade("STOCK:AAPL", "order-1", "PROFIT", Decimal("10"), Decimal("1"), Decimal("9"))
        self.assertEqual(len(fake_bot.recent_stop_losses), 0)

        record_trade("STOCK:AAPL", "order-2", "STOP", Decimal("9"), Decimal("-1"), Decimal("10"))
        self.assertEqual(len(fake_bot.recent_stop_losses), 1)


class SymbolQuarantineTests(unittest.TestCase):
    KEY = "STOCK:AAPL"

    @classmethod
    def _fake_bot(cls, pnl_history=None, key=None, **config_overrides):
        from webull_bot.bot import AutoTrader

        defaults = dict(
            symbol_quarantine_enabled=True,
            symbol_quarantine_lookback_seconds=1800,
            symbol_quarantine_min_trades=3,
            symbol_quarantine_loss_dollars=Decimal("0.50"),
            symbol_quarantine_cooldown_seconds=900,
        )
        defaults.update(config_overrides)
        config = SimpleNamespace(**defaults)
        history = defaultdict(deque)
        if pnl_history is not None:
            history[key or cls.KEY] = deque(pnl_history)
        fake_bot = SimpleNamespace(
            config=config,
            symbol_pnl_history=history,
            symbol_quarantine_until={},
        )
        return AutoTrader.symbol_quarantined.__get__(fake_bot), fake_bot

    def test_passes_through_with_no_history(self):
        quarantined, _ = self._fake_bot(None)
        self.assertFalse(quarantined(self.KEY))

    def test_passes_through_below_min_trades(self):
        now = time.monotonic()
        quarantined, _ = self._fake_bot(
            [(now - 10, Decimal("-1")), (now - 20, Decimal("-1"))]
        )
        self.assertFalse(quarantined(self.KEY))

    def test_trips_when_net_loss_meets_threshold_within_lookback(self):
        now = time.monotonic()
        quarantined, _ = self._fake_bot(
            [
                (now - 10, Decimal("-0.20")),
                (now - 20, Decimal("-0.20")),
                (now - 30, Decimal("-0.20")),
            ]
        )
        self.assertTrue(quarantined(self.KEY))
        # Once tripped, stays quarantined for the cooldown even without a
        # new loss - same shape as stop_loss_guard_active.
        self.assertTrue(quarantined(self.KEY))

    def test_net_loss_under_threshold_does_not_trip(self):
        now = time.monotonic()
        quarantined, _ = self._fake_bot(
            [
                (now - 10, Decimal("-0.10")),
                (now - 20, Decimal("-0.10")),
                (now - 30, Decimal("-0.10")),
            ]
        )
        self.assertFalse(quarantined(self.KEY))

    def test_profitable_trades_dont_trip_even_at_the_trade_count(self):
        now = time.monotonic()
        quarantined, _ = self._fake_bot(
            [
                (now - 10, Decimal("5")),
                (now - 20, Decimal("-1")),
                (now - 30, Decimal("-1")),
            ]
        )
        self.assertFalse(quarantined(self.KEY))

    def test_old_trades_outside_lookback_dont_count(self):
        now = time.monotonic()
        quarantined, _ = self._fake_bot(
            [
                (now - 5000, Decimal("-1")),
                (now - 4000, Decimal("-1")),
                (now - 3000, Decimal("-1")),
            ],
            symbol_quarantine_lookback_seconds=1800,
        )
        self.assertFalse(quarantined(self.KEY))

    def test_disabled_by_config(self):
        now = time.monotonic()
        quarantined, _ = self._fake_bot(
            [
                (now - 10, Decimal("-1")),
                (now - 20, Decimal("-1")),
                (now - 30, Decimal("-1")),
            ],
            symbol_quarantine_enabled=False,
        )
        self.assertFalse(quarantined(self.KEY))

    def test_other_symbols_are_unaffected(self):
        now = time.monotonic()
        quarantined, _ = self._fake_bot(
            [
                (now - 10, Decimal("-1")),
                (now - 20, Decimal("-1")),
                (now - 30, Decimal("-1")),
            ]
        )
        self.assertTrue(quarantined(self.KEY))
        self.assertFalse(quarantined("STOCK:MSFT"))

    def test_record_trade_partitions_pnl_history_per_key(self):
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
        )
        record_trade = AutoTrader.record_trade.__get__(fake_bot)

        record_trade(
            "STOCK:AAPL", "order-1", "STOP", Decimal("9"),
            pnl=Decimal("-1"), entry_price=Decimal("10"),
        )
        record_trade(
            "STOCK:MSFT", "order-2", "PROFIT", Decimal("11"),
            pnl=Decimal("2"), entry_price=Decimal("10"),
        )

        self.assertEqual(len(fake_bot.symbol_pnl_history["STOCK:AAPL"]), 1)
        self.assertEqual(fake_bot.symbol_pnl_history["STOCK:AAPL"][0][1], Decimal("-1"))
        self.assertEqual(len(fake_bot.symbol_pnl_history["STOCK:MSFT"]), 1)
        self.assertEqual(fake_bot.symbol_pnl_history["STOCK:MSFT"][0][1], Decimal("2"))


class PostStopReentryCooldownTests(unittest.TestCase):
    """post_stop_reentry_ready - by request, after the DAIC incident (3
    stop-losses in ~9 minutes on one symbol during a fast decline,
    erasing the day's gains). Narrow and symbol-specific, unlike a
    same-day quarantine (explicitly rejected earlier as "a bandaid").
    """

    def test_ready_when_never_stopped_out(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            last_volatility_stop_loss_at={},
            config=SimpleNamespace(volatility_scalp_post_stop_cooldown_seconds=300),
        )
        ready = AutoTrader.post_stop_reentry_ready.__get__(fake_bot)
        self.assertTrue(ready("DAIC"))

    def test_blocked_immediately_after_a_stop_loss(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            last_volatility_stop_loss_at={"DAIC": time.monotonic()},
            config=SimpleNamespace(volatility_scalp_post_stop_cooldown_seconds=300),
        )
        ready = AutoTrader.post_stop_reentry_ready.__get__(fake_bot)
        self.assertFalse(ready("DAIC"))

    def test_a_stop_on_a_different_symbol_does_not_block_this_one(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            last_volatility_stop_loss_at={"DAIC": time.monotonic()},
            config=SimpleNamespace(volatility_scalp_post_stop_cooldown_seconds=300),
        )
        ready = AutoTrader.post_stop_reentry_ready.__get__(fake_bot)
        self.assertTrue(ready("OTHER"))

    def test_ready_again_once_the_cooldown_elapses(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            last_volatility_stop_loss_at={"DAIC": time.monotonic() - 301},
            config=SimpleNamespace(volatility_scalp_post_stop_cooldown_seconds=300),
        )
        ready = AutoTrader.post_stop_reentry_ready.__get__(fake_bot)
        self.assertTrue(ready("DAIC"))

    def test_record_trade_stamps_the_cooldown_on_a_stop_loss_exit(self):
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
        )
        record_trade = AutoTrader.record_trade.__get__(fake_bot)

        record_trade(
            "STOCK:DAIC", "order-1", "STOP", Decimal("9"),
            pnl=Decimal("-1"), entry_price=Decimal("10"),
        )

        self.assertIn("DAIC", fake_bot.last_volatility_stop_loss_at)

    def test_record_trade_does_not_stamp_on_a_profit_exit(self):
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
        )
        record_trade = AutoTrader.record_trade.__get__(fake_bot)

        record_trade(
            "STOCK:DAIC", "order-1", "PROFIT", Decimal("11"),
            pnl=Decimal("1"), entry_price=Decimal("10"),
        )

        self.assertNotIn("DAIC", fake_bot.last_volatility_stop_loss_at)


class RegimeGateTests(unittest.TestCase):
    def test_passes_through_with_no_current_reading(self):
        history = deque([Decimal(x) for x in range(1, 21)])
        self.assertTrue(
            TradingStrategy.stock_market_regime_ok(history, None, Decimal("0.85"))
        )

    def test_passes_through_without_enough_history(self):
        history = deque([Decimal("1"), Decimal("2")])
        self.assertTrue(
            TradingStrategy.stock_market_regime_ok(
                history, Decimal("2"), Decimal("0.85")
            )
        )

    def test_rejects_when_current_is_in_the_top_of_its_own_range(self):
        history = deque([Decimal(x) for x in range(1, 21)])
        self.assertFalse(
            TradingStrategy.stock_market_regime_ok(
                history, Decimal("20"), Decimal("0.85")
            )
        )

    def test_allows_when_current_is_mid_range(self):
        history = deque([Decimal(x) for x in range(1, 21)])
        self.assertTrue(
            TradingStrategy.stock_market_regime_ok(
                history, Decimal("5"), Decimal("0.85")
            )
        )

    def test_uses_its_own_configurable_percentile_distinct_from_options(self):
        # 15th of 20 samples => rank 0.75 - rejected at a strict 0.70 gate,
        # allowed at a looser 0.90 one, proving the threshold is genuinely
        # parameterized rather than hardcoded to OPTION_VIXY_REJECT_PERCENTILE.
        history = deque([Decimal(x) for x in range(1, 21)])
        self.assertFalse(
            TradingStrategy.stock_market_regime_ok(
                history, Decimal("15"), Decimal("0.70")
            )
        )
        self.assertTrue(
            TradingStrategy.stock_market_regime_ok(
                history, Decimal("15"), Decimal("0.90")
            )
        )

    def test_disabled_regime_gate_active_is_always_false(self):
        """AutoTrader.trade_stocks only computes regime_gate_active at all
        when REGIME_GATE_ENABLED is set - mirrors guard_active's own
        config-gated pattern.
        """
        config = SimpleNamespace(
            regime_gate_enabled=False,
            regime_gate_reject_percentile=Decimal("0.85"),
        )
        vixy_history = deque([Decimal(x) for x in range(1, 21)])
        regime_gate_active = config.regime_gate_enabled and not (
            TradingStrategy.stock_market_regime_ok(
                vixy_history, vixy_history[-1], config.regime_gate_reject_percentile
            )
        )
        self.assertFalse(regime_gate_active)


class ExecutionGuardrailTests(unittest.TestCase):
    def test_price_sanity_ok_within_tolerance(self):
        from webull_bot.bot import AutoTrader

        check = AutoTrader.price_sanity_ok.__get__(
            SimpleNamespace(price_sanity_rejected_at={})
        )
        self.assertTrue(check("AAPL", Decimal("100.00"), Decimal("103.00")))

    def test_price_sanity_rejects_large_deviation(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(price_sanity_rejected_at={})
        check = AutoTrader.price_sanity_ok.__get__(fake_bot)
        with self.assertLogs("webull-bot", level="ERROR") as logs:
            self.assertFalse(check("AAPL", Decimal("100.00"), Decimal("110.00")))
        self.assertIn("AAPL", logs.output[0])
        self.assertIn("AAPL", fake_bot.price_sanity_rejected_at)

    def test_option_tolerance_allows_a_wide_but_real_option_spread(self):
        """By request: "make sure your buy and sell price will
        actually be executed inside the spread for options, similar
        to stocks." A 20% deviation is normal for a liquid option
        (unlike a stock) and must not be rejected under the wider
        option tolerance, even though it would fail the default 5%
        stock tolerance.
        """
        from webull_bot.bot import AutoTrader
        from webull_bot.trading.guards.price_sanity import (
            OPTION_PRICE_SANITY_TOLERANCE,
        )

        fake_bot = SimpleNamespace(price_sanity_rejected_at={})
        check = AutoTrader.price_sanity_ok.__get__(fake_bot)
        self.assertTrue(
            check(
                "AAPL260101C00200000",
                Decimal("1.00"),
                Decimal("1.20"),
                tolerance=OPTION_PRICE_SANITY_TOLERANCE,
            )
        )
        self.assertEqual(fake_bot.price_sanity_rejected_at, {})

    def test_option_tolerance_still_rejects_a_truly_stale_quote(self):
        from webull_bot.bot import AutoTrader
        from webull_bot.trading.guards.price_sanity import (
            OPTION_PRICE_SANITY_TOLERANCE,
        )

        fake_bot = SimpleNamespace(price_sanity_rejected_at={})
        check = AutoTrader.price_sanity_ok.__get__(fake_bot)
        with self.assertLogs("webull-bot", level="ERROR"):
            self.assertFalse(
                check(
                    "AAPL260101C00200000",
                    Decimal("1.00"),
                    Decimal("2.00"),
                    tolerance=OPTION_PRICE_SANITY_TOLERANCE,
                )
            )

    def test_option_entry_spread_ok_allows_a_normal_liquid_spread(self):
        """By request ("just do not trade contracts that are not easy
        to liquidify"). A $1.00 contract quoting $0.90/$1.10 is a
        normal, liquid 20% option spread - must not be blocked.
        """
        from webull_bot.trading.guards.price_sanity import option_entry_spread_ok

        self.assertTrue(
            option_entry_spread_ok(
                Decimal("0.90"), Decimal("1.10"), Decimal("25")
            )
        )

    def test_option_entry_spread_ok_rejects_the_orcl_incident_spread(self):
        """Live incident: ORCL got bought into, then its own STOP-loss
        could never find a buyer at any sane price - a genuine 42.9%
        bid/ask spread. This should never have qualified for entry in
        the first place.
        """
        from webull_bot.trading.guards.price_sanity import option_entry_spread_ok

        self.assertFalse(
            option_entry_spread_ok(
                Decimal("0.07"), Decimal("0.10"), Decimal("25")
            )
        )

    def test_option_entry_spread_ok_passes_open_on_a_missing_quote(self):
        from webull_bot.trading.guards.price_sanity import option_entry_spread_ok

        self.assertTrue(option_entry_spread_ok(None, None, Decimal("25")))
        self.assertTrue(
            option_entry_spread_ok(Decimal("1.00"), None, Decimal("25"))
        )

    def test_entry_price_sanity_cooldown_blocks_a_recent_rejection(self):
        """Live incident: one illiquid symbol's quote sat just past
        price_sanity_ok's tolerance and got retried (and re-rejected) on
        essentially every scan cycle for hours - nothing backed it off.
        """
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(price_sanity_cooldown_seconds=30),
            price_sanity_rejected_at={"AAPL": 100.0},
        )
        ready = AutoTrader.price_sanity_cooldown_ready.__get__(fake_bot)
        with unittest.mock.patch("time.monotonic", return_value=110.0):
            self.assertFalse(ready("AAPL"))

    def test_entry_price_sanity_cooldown_clears_after_the_window(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(price_sanity_cooldown_seconds=30),
            price_sanity_rejected_at={"AAPL": 100.0},
        )
        ready = AutoTrader.price_sanity_cooldown_ready.__get__(fake_bot)
        with unittest.mock.patch("time.monotonic", return_value=131.0):
            self.assertTrue(ready("AAPL"))

    def test_price_sanity_cooldown_ready_for_a_never_rejected_symbol(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(price_sanity_cooldown_seconds=30),
            price_sanity_rejected_at={},
        )
        ready = AutoTrader.price_sanity_cooldown_ready.__get__(fake_bot)
        self.assertTrue(ready("AAPL"))

    def test_entry_price_sanity_cooldown_is_per_symbol(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(price_sanity_cooldown_seconds=30),
            price_sanity_rejected_at={"AAPL": 100.0},
        )
        ready = AutoTrader.price_sanity_cooldown_ready.__get__(fake_bot)
        with unittest.mock.patch("time.monotonic", return_value=110.0):
            self.assertTrue(ready("MSFT"))

    @staticmethod
    def _fake_bot_for_order_errors():
        return SimpleNamespace(
            order_error_times=deque(),
            broker_conflict_symbols=set(),
            pending_stock_exits=set(),
            pending_option_exits=set(),
            stop_exit_submitted={},
            stop_loss_escalated=set(),
            stop_condition_since={},
        )

    def test_record_order_error_blacklists_only_the_offending_symbol(self):
        """Regression test: this used to trip a global kill switch that
        halted every symbol's entries AND exits until the process was
        restarted - in production, a single symbol stuck in a broker-side
        rejection (Webull's $0.10-$0.999 lot-size rule) repeatedly tripped
        this and froze the entire bot over a problem confined to one
        symbol. Must now blacklist only that symbol (reusing
        broker_conflict_symbols, which every entry path already checks),
        leaving every other symbol unaffected.
        """
        from webull_bot.bot import AutoTrader, CONSECUTIVE_ORDER_ERROR_LIMIT

        fake_bot = self._fake_bot_for_order_errors()
        record = AutoTrader.record_order_error.__get__(fake_bot)
        with self.assertLogs("webull-bot", level="CRITICAL"):
            for _ in range(CONSECUTIVE_ORDER_ERROR_LIMIT):
                record("OPTT", RuntimeError("boom"))
        self.assertIn("OPTT", fake_bot.broker_conflict_symbols)
        self.assertFalse(hasattr(fake_bot, "order_kill_switch_tripped"))

    def test_record_order_error_does_not_trip_below_threshold(self):
        from webull_bot.bot import AutoTrader, CONSECUTIVE_ORDER_ERROR_LIMIT

        fake_bot = self._fake_bot_for_order_errors()
        record = AutoTrader.record_order_error.__get__(fake_bot)
        for _ in range(CONSECUTIVE_ORDER_ERROR_LIMIT - 1):
            record("TEST", RuntimeError("boom"))
        self.assertEqual(fake_bot.broker_conflict_symbols, set())

    def test_record_order_error_prunes_entries_outside_window(self):
        from webull_bot.bot import AutoTrader, ORDER_ERROR_WINDOW_SECONDS

        fake_bot = self._fake_bot_for_order_errors()
        record = AutoTrader.record_order_error.__get__(fake_bot)
        fake_bot.order_error_times.append(
            time.monotonic() - ORDER_ERROR_WINDOW_SECONDS - 5
        )
        record("TEST", RuntimeError("boom"))
        self.assertEqual(len(fake_bot.order_error_times), 1)

    @staticmethod
    def _fake_bot_for_placement(placed, price="10.00"):
        from webull_bot.bot import AutoTrader
        from webull_bot.strategy import TradingStrategy

        class FakeApi:
            @staticmethod
            def quote_price(q):
                return Decimal(str(q["price"]))

            @staticmethod
            def stock_limit_price(q, side):
                return Decimal(str(q["price"]))

            def place_stock(self, symbol, side, quantity, limit_price=None, fractional=False):
                placed.append((symbol, side, quantity, limit_price, fractional))
                return "order-1"

        fake_bot = SimpleNamespace(
            api=FakeApi(),
            iceberg_orders={},
            price_sanity_rejected_at={},
            config=SimpleNamespace(price_sanity_cooldown_seconds=30),
            strategy=SimpleNamespace(minimum_lot_size=TradingStrategy.minimum_lot_size),
        )
        fake_bot.price_sanity_ok = AutoTrader.price_sanity_ok.__get__(fake_bot)
        fake_bot.price_sanity_cooldown_ready = AutoTrader.price_sanity_cooldown_ready.__get__(fake_bot)
        fake_bot.record_order_error = AutoTrader.record_order_error.__get__(fake_bot)
        return fake_bot

    def test_place_stock_scaled_below_threshold_places_single_order(self):
        from webull_bot.bot import AutoTrader, ICEBERG_MIN_SHARES

        placed = []
        fake_bot = self._fake_bot_for_placement(placed)
        place = AutoTrader.place_stock_scaled.__get__(fake_bot)
        quantity = ICEBERG_MIN_SHARES - 1
        order_id = place("AAA", "BUY", quantity, "STOCK:AAA", {"price": "10.00"})
        self.assertEqual(order_id, "order-1")
        self.assertEqual(len(placed), 1)
        self.assertEqual(placed[0][2], quantity)
        self.assertNotIn("AAA:BUY", fake_bot.iceberg_orders)

    def test_place_stock_scaled_at_threshold_slices_and_registers_remainder(self):
        from webull_bot.bot import (
            AutoTrader,
            ICEBERG_MIN_SHARES,
            ICEBERG_SLICE_SHARES,
        )

        placed = []
        fake_bot = self._fake_bot_for_placement(placed)
        place = AutoTrader.place_stock_scaled.__get__(fake_bot)
        total_qty = ICEBERG_MIN_SHARES + 25
        order_id = place("AAA", "BUY", total_qty, "STOCK:AAA", {"price": "10.00"})
        self.assertEqual(order_id, "order-1")
        self.assertEqual(placed[0][2], Decimal(ICEBERG_SLICE_SHARES))
        entry = fake_bot.iceberg_orders["AAA:BUY"]
        self.assertEqual(entry["remaining"], total_qty - ICEBERG_SLICE_SHARES)

    def test_place_stock_scaled_never_slices_below_the_lot_restricted_minimum(self):
        """Live incident: HOWL, priced in the $0.10-$0.999 band, sized to
        a 100+ share order (Webull's own minimum there) but iceberg-sliced
        into 10-share clips - every clip individually below the 100-share
        floor, so every single slice got rejected with 417
        OAUTH_OPENAPI_CANT_TRADE_FOR_PRICE_BETWEEN_0099_AND_0999. A
        lot-restricted order must go out whole, never sliced.
        """
        from webull_bot.bot import AutoTrader, ICEBERG_MIN_SHARES

        placed = []
        fake_bot = self._fake_bot_for_placement(placed)
        place = AutoTrader.place_stock_scaled.__get__(fake_bot)
        total_qty = ICEBERG_MIN_SHARES + 50
        order_id = place("HOWL", "BUY", total_qty, "STOCK:HOWL", {"price": "0.30"})
        self.assertEqual(order_id, "order-1")
        self.assertEqual(len(placed), 1)
        self.assertEqual(placed[0][2], total_qty)
        self.assertNotIn("HOWL:BUY", fake_bot.iceberg_orders)

    def test_place_stock_scaled_clamps_to_hard_notional_ceiling(self):
        from webull_bot.bot import AutoTrader

        placed = []
        fake_bot = self._fake_bot_for_placement(placed)
        place = AutoTrader.place_stock_scaled.__get__(fake_bot)
        # 10-share slice at $250 = $2500, over the $2000 ceiling -> clamps
        # to floor(2000/250) = 8 shares instead.
        order_id = place("AAA", "BUY", 100, "STOCK:AAA", {"price": "250"})
        self.assertEqual(order_id, "order-1")
        self.assertEqual(placed[0][2], Decimal("8"))

    def test_place_stock_scaled_returns_none_on_price_sanity_failure(self):
        from webull_bot.bot import AutoTrader

        placed = []

        class BadPriceApi:
            @staticmethod
            def quote_price(q):
                return Decimal(str(q["price"]))

            @staticmethod
            def stock_limit_price(q, side):
                return Decimal("999")

            def place_stock(self, *a, **k):
                placed.append((a, k))
                return "order-1"

        fake_bot = SimpleNamespace(
            api=BadPriceApi(),
            iceberg_orders={},
            price_sanity_rejected_at={},
            config=SimpleNamespace(price_sanity_cooldown_seconds=30),
        )
        fake_bot.price_sanity_ok = AutoTrader.price_sanity_ok.__get__(fake_bot)
        fake_bot.price_sanity_cooldown_ready = AutoTrader.price_sanity_cooldown_ready.__get__(fake_bot)
        fake_bot.record_order_error = AutoTrader.record_order_error.__get__(fake_bot)
        place = AutoTrader.place_stock_scaled.__get__(fake_bot)
        with self.assertLogs("webull-bot", level="ERROR"):
            order_id = place("AAA", "BUY", 5, "STOCK:AAA", {"price": "10.00"})
        self.assertIsNone(order_id)
        self.assertEqual(placed, [])

    def test_process_iceberg_orders_places_next_slice_after_interval(self):
        from webull_bot.bot import (
            AutoTrader,
            ICEBERG_SLICE_INTERVAL_SECONDS,
            ICEBERG_SLICE_SHARES,
        )
        from webull_bot.strategy import TradingStrategy

        placed = []

        class FakeApi:
            def stock_quote(self, symbol):
                return {"symbol": symbol, "bid": "10.00", "ask": "10.02", "price": "10.01"}

            @staticmethod
            def quote_price(q):
                return Decimal(str(q["price"]))

            @staticmethod
            def stock_limit_price(q, side):
                return Decimal(str(q["price"]))

            def place_stock(self, symbol, side, quantity, limit_price=None):
                placed.append((symbol, side, quantity, limit_price))
                return "order-2"

        fake_bot = SimpleNamespace(
            api=FakeApi(),
            iceberg_orders={
                "AAA:BUY": {
                    "symbol": "AAA",
                    "side": "BUY",
                    "key": "STOCK:AAA",
                    "remaining": Decimal("15"),
                    "last_slice_at": time.monotonic()
                    - ICEBERG_SLICE_INTERVAL_SECONDS
                    - 1,
                }
            },
            price_sanity_rejected_at={},
            strategy=SimpleNamespace(minimum_lot_size=TradingStrategy.minimum_lot_size),
        )
        fake_bot.price_sanity_ok = AutoTrader.price_sanity_ok.__get__(fake_bot)
        fake_bot.record_order_error = AutoTrader.record_order_error.__get__(fake_bot)
        recorded = []
        fake_bot.record_trade = (
            lambda key, order_id, action, entry_price=None, quantity=None: recorded.append(
                (key, order_id, action, entry_price)
            )
        )
        process = AutoTrader.process_iceberg_orders.__get__(fake_bot)
        process()
        self.assertEqual(len(placed), 1)
        self.assertEqual(placed[0][2], Decimal(ICEBERG_SLICE_SHARES))
        self.assertEqual(
            fake_bot.iceberg_orders["AAA:BUY"]["remaining"],
            Decimal("15") - Decimal(ICEBERG_SLICE_SHARES),
        )
        # Regression coverage: an iceberg slice's dashboard row must show
        # the price paid, not a blank Entry column.
        self.assertEqual(
            recorded, [("STOCK:AAA", "order-2", "BUY", Decimal("10.01"))]
        )

    def test_process_iceberg_orders_skips_before_interval_elapses(self):
        from webull_bot.bot import AutoTrader

        class FakeApi:
            def stock_quote(self, symbol):
                raise AssertionError("must not fetch a quote before the interval")

        fake_bot = SimpleNamespace(
            api=FakeApi(),
            iceberg_orders={
                "AAA:BUY": {
                    "symbol": "AAA",
                    "side": "BUY",
                    "key": "STOCK:AAA",
                    "remaining": Decimal("15"),
                    "last_slice_at": time.monotonic(),
                }
            },
        )
        process = AutoTrader.process_iceberg_orders.__get__(fake_bot)
        process()
        self.assertEqual(fake_bot.iceberg_orders["AAA:BUY"]["remaining"], Decimal("15"))

    def test_process_iceberg_orders_removes_entry_when_fully_filled(self):
        from webull_bot.bot import AutoTrader, ICEBERG_SLICE_INTERVAL_SECONDS, ICEBERG_SLICE_SHARES
        from webull_bot.strategy import TradingStrategy

        class FakeApi:
            def stock_quote(self, symbol):
                return {"symbol": symbol, "bid": "10.00", "ask": "10.02", "price": "10.01"}

            @staticmethod
            def quote_price(q):
                return Decimal(str(q["price"]))

            @staticmethod
            def stock_limit_price(q, side):
                return Decimal(str(q["price"]))

            def place_stock(self, symbol, side, quantity, limit_price=None):
                return "order-3"

        fake_bot = SimpleNamespace(
            api=FakeApi(),
            iceberg_orders={
                "AAA:BUY": {
                    "symbol": "AAA",
                    "side": "BUY",
                    "key": "STOCK:AAA",
                    "remaining": Decimal(str(ICEBERG_SLICE_SHARES)),
                    "last_slice_at": time.monotonic()
                    - ICEBERG_SLICE_INTERVAL_SECONDS
                    - 1,
                }
            },
            record_trade=lambda *a, **k: None,
            price_sanity_rejected_at={},
            strategy=SimpleNamespace(minimum_lot_size=TradingStrategy.minimum_lot_size),
        )
        fake_bot.price_sanity_ok = AutoTrader.price_sanity_ok.__get__(fake_bot)
        fake_bot.record_order_error = AutoTrader.record_order_error.__get__(fake_bot)
        process = AutoTrader.process_iceberg_orders.__get__(fake_bot)
        process()
        self.assertNotIn("AAA:BUY", fake_bot.iceberg_orders)

    def test_process_iceberg_orders_places_the_full_remainder_in_the_lot_restricted_band(self):
        """Companion to the place_stock_scaled regression test - if price
        drifts into the $0.10-$0.999 band after the first clip already
        went out, a later slice must not keep trying 10-share clips
        either.
        """
        from webull_bot.bot import AutoTrader, ICEBERG_SLICE_INTERVAL_SECONDS
        from webull_bot.strategy import TradingStrategy

        placed = []

        class FakeApi:
            def stock_quote(self, symbol):
                return {"symbol": symbol, "bid": "0.29", "ask": "0.31", "price": "0.30"}

            @staticmethod
            def quote_price(q):
                return Decimal(str(q["price"]))

            @staticmethod
            def stock_limit_price(q, side):
                return Decimal(str(q["price"]))

            def place_stock(self, symbol, side, quantity, limit_price=None):
                placed.append((symbol, side, quantity, limit_price))
                return "order-4"

        fake_bot = SimpleNamespace(
            api=FakeApi(),
            iceberg_orders={
                "HOWL:BUY": {
                    "symbol": "HOWL",
                    "side": "BUY",
                    "key": "STOCK:HOWL",
                    "remaining": Decimal("120"),
                    "last_slice_at": time.monotonic()
                    - ICEBERG_SLICE_INTERVAL_SECONDS
                    - 1,
                }
            },
            price_sanity_rejected_at={},
            strategy=SimpleNamespace(minimum_lot_size=TradingStrategy.minimum_lot_size),
        )
        fake_bot.price_sanity_ok = AutoTrader.price_sanity_ok.__get__(fake_bot)
        fake_bot.record_order_error = AutoTrader.record_order_error.__get__(fake_bot)
        fake_bot.record_trade = lambda *a, **k: None
        process = AutoTrader.process_iceberg_orders.__get__(fake_bot)
        process()
        self.assertEqual(len(placed), 1)
        self.assertEqual(placed[0][2], Decimal("120"))
        self.assertNotIn("HOWL:BUY", fake_bot.iceberg_orders)


class FractionalExitGuardTests(unittest.TestCase):
    def test_is_fractional_quantity(self):
        from webull_bot.bot import AutoTrader

        self.assertTrue(AutoTrader.is_fractional_quantity(Decimal("2.5847")))
        self.assertFalse(AutoTrader.is_fractional_quantity(Decimal("5")))
        self.assertFalse(AutoTrader.is_fractional_quantity(Decimal("0")))

    def test_stall_equity_quotes_batches_into_one_call_per_category(self):
        """Regression test: boost_stalled_positions used to call
        api.stock_quote(symbol) individually inside its loop - which
        itself makes two API calls per symbol (a category lookup plus a
        single-symbol quote fetch) - so a held-position count in the
        teens meant dozens of sequential, individually rate-limited round
        trips blocking the entire single-threaded main loop for minutes
        at a stretch. Candidates across two categories must batch into
        exactly one call per category, not one call per symbol.
        """
        from webull_bot.bot import AutoTrader

        calls = []

        class FakeApi:
            def stock_quotes_resilient(self, symbols, category):
                calls.append((category, tuple(symbols)))
                return [
                    {"symbol": s, "bid": "10.00"} for s in symbols
                ], set()

        # Not a real time.monotonic() reading - last_trade defaults to 0.0
        # per-symbol below, and the real monotonic clock isn't guaranteed
        # to already exceed stall_seconds (120) on every CI runner (same
        # class of flaky-clock bug fixed elsewhere in this file).
        now = 10_000.0
        fake_bot = SimpleNamespace(
            api=FakeApi(),
            stock_categories={"NVDA": "US_ETF"},
            pending_stock_exits=set(),
            last_trade={},
        )
        fake_bot.cooldown_ready = lambda key: True
        fake_bot.has_pending_sell_order = lambda key: False
        fake_bot.is_fractional_quantity = AutoTrader.is_fractional_quantity
        fetch = AutoTrader._stall_equity_quotes.__get__(fake_bot)
        positions = [
            {"instrument_type": "EQUITY", "symbol": "AAPL", "quantity": "5", "cost_price": "100"},
            {"instrument_type": "EQUITY", "symbol": "MSFT", "quantity": "3", "cost_price": "200"},
            {"instrument_type": "EQUITY", "symbol": "NVDA", "quantity": "2", "cost_price": "50"},
            {"instrument_type": "OPTION", "symbol": "AAPL260918C00200000", "quantity": "1", "cost_price": "1"},
        ]

        quote_by_symbol = fetch(positions, core_session_active=True, stall_seconds=120, now=now)

        # Exactly one call per category (US_STOCK: AAPL+MSFT, US_ETF: NVDA)
        # - never one call per symbol, and the OPTION leg is never
        # considered at all.
        self.assertEqual(len(calls), 2)
        called_categories = {category for category, _ in calls}
        self.assertEqual(called_categories, {"US_STOCK", "US_ETF"})
        self.assertEqual(set(quote_by_symbol), {"AAPL", "MSFT", "NVDA"})

    def test_boost_stalled_positions_skips_fractional_position_outside_core_hours(self):
        # Regression test: Webull rejects ANY order (buy or sell) on a
        # non-integer quantity outside core hours regardless of the
        # client-side fractional/order-type flags - previously this kept
        # retrying every stall-breaker interval and spamming the same
        # OAUTH_OPENAPI_FRACT_ONLT_CORE_TIME rejection.
        from webull_bot.bot import AutoTrader

        calls = []

        class FakeApi:
            def stock_quotes_resilient(self, symbols, category):
                calls.append(symbols)
                return [
                    {"symbol": s, "bid": "100.00", "ask": "100.05"} for s in symbols
                ], set()

            @staticmethod
            def quote_bid(q):
                return Decimal(str(q["bid"]))

            @staticmethod
            def quote_ask(q):
                return Decimal(str(q["ask"]))

            def place_stock(self, *a, **k):
                raise AssertionError("must not place an order outside core hours")

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                stall_breaker_enabled=True,
                stall_breaker_seconds=1,
                stall_breaker_min_profit=Decimal("0.01"),
                sell_fee_dollars=Decimal("0.02"),
                stock_entry_max_spread_percent=Decimal("0.50"),
            ),
            api=FakeApi(),
            stock_categories={},
            last_trade={},
            last_stall_boost=0.0,
            pending_stock_exits=set(),
            pending_option_exits=set(),
        )
        fake_bot.cooldown_ready = lambda key: True
        fake_bot.has_pending_sell_order = lambda key: False
        fake_bot.is_fractional_quantity = AutoTrader.is_fractional_quantity
        fake_bot._stall_equity_quotes = AutoTrader._stall_equity_quotes.__get__(fake_bot)
        fake_bot._stall_exit_price = AutoTrader._stall_exit_price.__get__(fake_bot)
        boost = AutoTrader.boost_stalled_positions.__get__(fake_bot)
        positions = [
            {
                "instrument_type": "EQUITY",
                "symbol": "COST",
                "quantity": "2.5847",
                "cost_price": "95.00",
            }
        ]
        boost(positions, options_active=False, core_session_active=False)
        self.assertEqual(calls, [])

    def test_boost_stalled_positions_skips_sub_lot_position_in_penny_band(self):
        # Regression test: Webull rejects ANY order (either side) under
        # 100 shares while price sits in $0.10-$0.999
        # (OAUTH_OPENAPI_CANT_TRADE_FOR_PRICE_BETWEEN_0099_AND_0999),
        # regardless of how many shares are actually held - a position
        # that fell into this band with fewer than 100 shares can't be
        # exited by a normal order at all until price moves back out.
        from webull_bot.bot import AutoTrader
        from webull_bot.strategy import TradingStrategy

        calls = []

        class FakeApi:
            def stock_quotes_resilient(self, symbols, category):
                calls.extend(symbols)
                return [
                    {"symbol": s, "bid": "0.50", "ask": "0.51"} for s in symbols
                ], set()

            @staticmethod
            def quote_bid(q):
                return Decimal(str(q["bid"]))

            @staticmethod
            def quote_ask(q):
                return Decimal(str(q["ask"]))

            def place_stock(self, *a, **k):
                raise AssertionError(
                    "must not place a sub-100-share order in the "
                    "lot-restricted band"
                )

        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                stall_breaker_enabled=True,
                stall_breaker_seconds=1,
                stall_breaker_min_profit=Decimal("0.01"),
                sell_fee_dollars=Decimal("0.02"),
                stock_entry_max_spread_percent=Decimal("0.50"),
            ),
            api=FakeApi(),
            strategy=SimpleNamespace(
                minimum_lot_size=TradingStrategy.minimum_lot_size,
                exit_blocked_by_lot_restriction=TradingStrategy.exit_blocked_by_lot_restriction,
            ),
            stock_categories={},
            last_trade={},
            last_stall_boost=0.0,
            pending_stock_exits=set(),
            pending_option_exits=set(),
        )
        fake_bot.cooldown_ready = lambda key: True
        fake_bot.has_pending_sell_order = lambda key: False
        fake_bot.is_fractional_quantity = AutoTrader.is_fractional_quantity
        fake_bot._stall_equity_quotes = AutoTrader._stall_equity_quotes.__get__(fake_bot)
        fake_bot._stall_exit_price = AutoTrader._stall_exit_price.__get__(fake_bot)
        boost = AutoTrader.boost_stalled_positions.__get__(fake_bot)
        positions = [
            {
                "instrument_type": "EQUITY",
                "symbol": "OPTT",
                "quantity": "5",
                "cost_price": "0.40",
            }
        ]
        boost(positions, options_active=False, core_session_active=True)
        self.assertEqual(calls, ["OPTT"])

    def test_stall_check_is_per_symbol_not_a_global_activity_clock(self):
        """Regression test: an account that's generally active (new
        entries landing every minute or two, well under
        STALL_BREAKER_SECONDS) previously blocked the stall-breaker from
        running at all, via one global "has ANYTHING filled recently"
        clock - even though a specific older position had been sitting
        untouched with no order activity of its own the whole time. The
        check must be per-symbol (self.last_trade[key]), not global.
        """
        from webull_bot.bot import AutoTrader
        from webull_bot.strategy import TradingStrategy

        calls = []

        class FakeApi:
            def stock_quotes_resilient(self, symbols, category):
                calls.extend(symbols)
                return [
                    {"symbol": s, "bid": "101.00", "ask": "101.05"} for s in symbols
                ], set()

            @staticmethod
            def quote_bid(q):
                return Decimal(str(q["bid"]))

            @staticmethod
            def quote_ask(q):
                return Decimal(str(q["ask"]))

            @staticmethod
            def price_tick_size(price):
                from webull_bot.webull_api import WebullAPI

                return WebullAPI.price_tick_size(price)

            def place_stock(self, *a, **k):
                return "order-1"

        now = time.monotonic()
        fake_bot = SimpleNamespace(
            config=SimpleNamespace(
                stall_breaker_enabled=True,
                stall_breaker_seconds=120,
                stall_breaker_min_profit=Decimal("0.01"),
                sell_fee_dollars=Decimal("0.02"),
                stock_entry_max_spread_percent=Decimal("0.50"),
            ),
            api=FakeApi(),
            strategy=SimpleNamespace(
                minimum_lot_size=TradingStrategy.minimum_lot_size,
                exit_blocked_by_lot_restriction=TradingStrategy.exit_blocked_by_lot_restriction,
            ),
            stock_categories={},
            # A different symbol traded 10 seconds ago (the account is
            # "generally active"), but STALE's own last order was 500
            # seconds ago - well past stall_breaker_seconds.
            last_trade={"STOCK:OTHER": now - 10, "STOCK:STALE": now - 500},
            # Not 0.0 - the real time.monotonic() isn't guaranteed to
            # already be past stall_breaker_seconds (120) on every CI
            # runner (same class of flaky-clock bug fixed earlier this
            # session, reintroduced here since 0.0 is only safe for a
            # tiny threshold like the other fixtures in this class use).
            last_stall_boost=now - 999999,
            pending_stock_exits=set(),
            pending_option_exits=set(),
        )
        fake_bot.cooldown_ready = lambda key: True
        fake_bot.has_pending_sell_order = lambda key: False
        fake_bot.is_fractional_quantity = AutoTrader.is_fractional_quantity
        fake_bot.record_realized_exit = lambda *a, **k: Decimal("0.05")
        fake_bot.record_trade = lambda *a, **k: None
        fake_bot._stall_equity_quotes = AutoTrader._stall_equity_quotes.__get__(fake_bot)
        fake_bot._stall_exit_price = AutoTrader._stall_exit_price.__get__(fake_bot)
        boost = AutoTrader.boost_stalled_positions.__get__(fake_bot)
        positions = [
            {
                "instrument_type": "EQUITY",
                "symbol": "STALE",
                "quantity": "10",
                "cost_price": "100.00",
            }
        ]

        boost(positions, options_active=False, core_session_active=True)

        self.assertEqual(calls, ["STALE"])
        self.assertIn("STALE", fake_bot.pending_stock_exits)
