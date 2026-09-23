import json
import logging
import shutil
import time
import unittest
import unittest.mock
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from webull_bot.commands import CommandQueue
from webull_bot.config import Settings
from webull_bot.daily_logging import DatedDailyFileHandler
from webull_bot.status import StatusWriter
from webull_bot.strategy import TradingStrategy
from webull_bot.webull_api import WebullAPI


class TradeEventLoggingTests(unittest.TestCase):
    """AutoTrader.log_trade_events is purely observational (Phase 0) -
    it must never touch trading state, only log.
    """

    def test_noop_when_the_service_is_disabled(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(trade_event_service=None)
        log_trade_events = AutoTrader.log_trade_events.__get__(fake_bot)
        log_trade_events()  # must not raise

    def test_drains_and_logs_every_event(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            trade_event_service=SimpleNamespace(
                drain=lambda: [(1024, 1, {"order_id": "abc"})]
            )
        )
        log_trade_events = AutoTrader.log_trade_events.__get__(fake_bot)
        with self.assertLogs("webull-bot", level="INFO") as logs:
            log_trade_events()
        self.assertIn("abc", logs.output[0])


class AllocationAndLoggingTests(unittest.TestCase):
    def test_default_capital_and_position_allocations(self):
        config = Settings()
        self.assertEqual(
            sum(config.stock_capital_fractions().values()),
            Decimal("1.00"),
        )
        self.assertEqual(
            config.stock_bucket_slot_limits(),
            {"POPULAR": 35, "PENNY": 5, "DISCOVERY": 10},
        )
        self.assertEqual(config.stock_universe_page_size, 200)
        self.assertEqual(config.stocks(), ["ALL"])
        self.assertEqual(config.max_symbols, 800)
        self.assertEqual(config.stock_universe_limit(), 800)
        self.assertEqual(Settings(max_symbols=0).stock_universe_limit(), 500)
        self.assertTrue(
            {"NVDA", "TSLA", "AAPL", "GME", "AMC"}
            <= set(config.popular_stocks())
        )
        self.assertNotIn("SPY", config.popular_stocks())
        self.assertNotIn("QQQ", config.popular_stocks())

    def test_default_risk_tuning_keeps_stop_floor_above_spread_gate(self):
        """STOCK_ENTRY_MAX_SPREAD_PERCENT must stay comfortably below
        STOCK_STOP_LOSS_MIN_PERCENT - the *floor*, not just the ceiling.
        A calm stock's adaptive stop clamps to the floor regardless of the
        range multiplier, so if the floor sat below the max tolerated
        spread (it briefly did: a 0.12% floor against a 0.50% spread gate),
        an entry near that spread ceiling could get stopped out by an
        ordinary bid/ask bounce alone, before any real adverse move -
        "trigger happy" stops firing on noise, not on a real loss.
        """
        config = Settings()
        self.assertEqual(config.stock_entry_max_spread_percent, Decimal("0.50"))
        self.assertEqual(config.stock_stop_loss_min_percent, Decimal("0.009"))
        self.assertEqual(config.stock_stop_loss_max_percent, Decimal("0.015"))
        self.assertEqual(config.stock_stop_loss_range_multiplier, Decimal("0.35"))
        spread_as_fraction = config.stock_entry_max_spread_percent / 100
        self.assertLess(spread_as_fraction, config.stock_stop_loss_min_percent)
        self.assertLess(spread_as_fraction, config.stock_stop_loss_max_percent)

    def test_default_reward_risk_ratio_gives_a_comfortable_breakeven_margin(self):
        """At the old STOCK_TARGET_STOP_MULTIPLE=1.2, breakeven needs a
        ~45.5% win rate (1 / (1 + ratio)) - too thin a margin for normal
        noise/whipsaw, and a real cause of net-losing days even with
        plenty of individual winning trades. 1.8 only needs ~35.7%,
        clearing the researched "most professional traders target at
        least 1:1.5-1:2 reward:risk" convention.
        """
        config = Settings()
        self.assertEqual(config.stock_target_stop_multiple, Decimal("1.8"))
        breakeven_win_rate = 1 / (1 + config.stock_target_stop_multiple)
        self.assertLess(breakeven_win_rate, Decimal("0.36"))

    def test_default_watchlist_is_parsed_and_deduplicated_by_membership(self):
        config = Settings()
        watchlist = config.default_watchlist()
        self.assertTrue(
            {"AAPL", "NVDA", "TSLA", "MSFT", "AMZN"} <= set(watchlist)
        )
        self.assertEqual(watchlist, [item.upper() for item in watchlist])

    def test_log_handler_writes_year_month_and_date_path(self):
        directory = Path("tests/.generated_logs")
        shutil.rmtree(directory, ignore_errors=True)
        try:
            handler = DatedDailyFileHandler(directory, "UTC")
            handler.setFormatter(logging.Formatter("%(message)s"))
            record = logging.LogRecord(
                "test",
                logging.INFO,
                __file__,
                1,
                "important context",
                (),
                None,
            )
            handler.emit(record)
            handler.close()

            today = datetime.now(timezone.utc).date()
            path = (
                Path(directory)
                / f"{today:%Y}"
                / f"{today:%m}"
                / f"{today:%Y-%m-%d}.log"
            )
            self.assertEqual(path.read_text(encoding="utf-8"), "important context\n")
        finally:
            shutil.rmtree(directory, ignore_errors=True)


class AccountDayPnlExtractionTests(unittest.TestCase):
    def test_extracts_total_day_profit_loss(self):
        balance = {"total_day_profit_loss": "-0.22"}
        self.assertEqual(
            WebullAPI.account_day_pnl_from_balance(balance), Decimal("-0.22")
        )

    def test_none_when_unreported(self):
        self.assertIsNone(WebullAPI.account_day_pnl_from_balance({}))

    def test_none_on_a_malformed_value(self):
        balance = {"total_day_profit_loss": "not-a-number"}
        self.assertIsNone(WebullAPI.account_day_pnl_from_balance(balance))

    def test_buying_power_from_balance_matches_the_prior_buying_power_behavior(self):
        balance = {
            "account_currency_assets": [
                {"currency": "USD", "day_buying_power": "120.00", "buying_power": "100.00"}
            ]
        }
        self.assertEqual(
            WebullAPI.buying_power_from_balance(balance), Decimal("120.00")
        )

    def test_option_buying_power_from_balance_reads_the_separate_pool(self):
        """Live incident: a real option order attempt failed with
        OPENAPI_DAY_BUYING_POWER_INSUFFICIENT even though stock buying
        power (day_buying_power) had plenty available - Webull tracks
        option_buying_power as a completely separate field/pool.
        """
        balance = {
            "account_currency_assets": [
                {
                    "currency": "USD",
                    "day_buying_power": "202.53",
                    "option_buying_power": "0.00",
                }
            ]
        }
        self.assertEqual(
            WebullAPI.option_buying_power_from_balance(balance), Decimal("0.00")
        )
        self.assertEqual(
            WebullAPI.buying_power_from_balance(balance), Decimal("202.53")
        )

    def test_option_buying_power_from_balance_defaults_to_zero_when_unreported(self):
        self.assertEqual(
            WebullAPI.option_buying_power_from_balance({}), Decimal("0")
        )


class StatusWriterTests(unittest.TestCase):
    def test_write_includes_pending_orders_for_the_dashboard(self):
        path = Path("tests/.generated_status/status.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(path))
            writer.write(
                mode="LIVE",
                buying_power=Decimal("1000"),
                positions=[],
                watchlist=[],
                agent_summary=None,
                paused=False,
                stock_count=10,
                option_count=0,
                pending_orders=[
                    {
                        "order_id": "order-1",
                        "instrument_type": "STOCK",
                        "symbol": "TSLA",
                        "action": "STOP",
                        "limit_price": "99.00",
                        "age_seconds": 12,
                        "cancel_requested": False,
                    }
                ],
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["pending_orders"]), 1)
            self.assertEqual(payload["pending_orders"][0]["symbol"], "TSLA")
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_write_defaults_pending_orders_to_empty_list(self):
        path = Path("tests/.generated_status/status2.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(path))
            writer.write(
                mode="LIVE",
                buying_power=Decimal("1000"),
                positions=[],
                watchlist=[],
                agent_summary=None,
                paused=False,
                stock_count=10,
                option_count=0,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["pending_orders"], [])
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_pnl_today_total_is_realized_plus_open_not_since_cost_unrealized(self):
        """Regression test: pnl_today used to blend the daily-reset
        realized total with a since-cost unrealized figure that could
        span several days for a position held that long - both fields
        under a write() call must actually be "today" scoped.
        """
        path = Path("tests/.generated_status/status10.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(path))
            writer.write(
                mode="LIVE",
                buying_power=Decimal("1000"),
                positions=[],
                watchlist=[],
                agent_summary=None,
                paused=False,
                stock_count=10,
                option_count=0,
                realized_pnl_today=Decimal("3.00"),
                open_pnl_total=Decimal("-1.25"),
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["pnl_today"]["realized"], "3.00")
            self.assertEqual(payload["pnl_today"]["open"], "-1.25")
            self.assertEqual(payload["pnl_today"]["total"], "1.75")
            self.assertNotIn("unrealized", payload["pnl_today"])
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_pnl_today_total_prefers_webulls_own_account_day_pnl(self):
        """Live incident: the bot's own realized+open estimate drifted
        from what Webull's app showed, worst for fractional holdings.
        realized_pnl_today is only ever an at-submission-time estimate
        (record_realized_exit's own docstring: "actual fill price can
        differ slightly"), and drift compounds over many trades on a
        high-frequency account - Webull's own account_day_pnl_total, when
        available, is ground truth and must win over the local sum.

        Second live incident, same day: showing the bot's own
        realized_pnl_today next to a Webull-sourced total produced a
        headline total that visibly didn't match its own breakdown
        (0 + 0.11 != -0.46 in the wild). realized must be backed out as
        total - open instead, so the breakdown always sums to the total
        exactly.
        """
        path = Path("tests/.generated_status/status11.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(path))
            writer.write(
                mode="LIVE",
                buying_power=Decimal("1000"),
                positions=[],
                watchlist=[],
                agent_summary=None,
                paused=False,
                stock_count=10,
                option_count=0,
                realized_pnl_today=Decimal("3.00"),
                open_pnl_total=Decimal("-1.25"),
                account_day_pnl_total=Decimal("-0.22"),
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            # open still shows the bot's own (largely Webull-sourced)
            # per-position breakdown; realized is backed out from the
            # authoritative total instead of the bot's own drifting
            # estimate, so open + realized == total always holds.
            self.assertEqual(payload["pnl_today"]["open"], "-1.25")
            self.assertEqual(payload["pnl_today"]["total"], "-0.22")
            self.assertEqual(payload["pnl_today"]["realized"], "1.03")
            self.assertEqual(
                Decimal(payload["pnl_today"]["realized"])
                + Decimal(payload["pnl_today"]["open"]),
                Decimal(payload["pnl_today"]["total"]),
            )
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_pnl_today_total_falls_back_to_local_sum_when_webull_unreported(self):
        path = Path("tests/.generated_status/status12.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(path))
            writer.write(
                mode="LIVE",
                buying_power=Decimal("1000"),
                positions=[],
                watchlist=[],
                agent_summary=None,
                paused=False,
                stock_count=10,
                option_count=0,
                realized_pnl_today=Decimal("3.00"),
                open_pnl_total=Decimal("-1.25"),
                account_day_pnl_total=None,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["pnl_today"]["total"], "1.75")
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_record_balance_appears_in_the_written_payload(self):
        path = Path("tests/.generated_status/status5.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(path))
            writer.record_balance(Decimal("1234.56"))
            writer.write(
                mode="LIVE",
                buying_power=Decimal("1000"),
                positions=[],
                watchlist=[],
                agent_summary=None,
                paused=False,
                stock_count=10,
                option_count=0,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["balance_history"]), 1)
            self.assertEqual(payload["balance_history"][0]["balance"], "1234.56")
            self.assertIn("time", payload["balance_history"][0])
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_balance_history_survives_a_new_statuswriter_instance(self):
        status_path = Path("tests/.generated_status/status6.json")
        state_path = Path("tests/.generated_status/trade_history6.json")
        shutil.rmtree(status_path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(status_path), state_file=str(state_path))
            writer.record_balance(Decimal("500.25"))

            restarted = StatusWriter(str(status_path), state_file=str(state_path))
            self.assertEqual(len(restarted.balance_history), 1)
            self.assertEqual(restarted.balance_history[0]["balance"], "500.25")
        finally:
            shutil.rmtree(status_path.parent, ignore_errors=True)

    def test_pre_existing_plain_list_state_file_still_loads_as_trades(self):
        """The state file predates balance history and may already exist on
        a running deployment as a bare JSON list of trades (the old
        format) - it must keep loading correctly, just with no balance
        history yet, rather than erroring or silently discarding trades.
        """
        status_path = Path("tests/.generated_status/status7.json")
        state_path = Path("tests/.generated_status/trade_history7.json")
        shutil.rmtree(status_path.parent, ignore_errors=True)
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps([{"symbol": "OLDFMT", "instrument_type": "STOCK"}]),
                encoding="utf-8",
            )
            writer = StatusWriter(str(status_path), state_file=str(state_path))
            self.assertEqual(len(writer.trades), 1)
            self.assertEqual(writer.trades[0]["symbol"], "OLDFMT")
            self.assertEqual(list(writer.balance_history), [])
        finally:
            shutil.rmtree(status_path.parent, ignore_errors=True)

    def test_recorded_trade_history_survives_a_new_statuswriter_instance(self):
        status_path = Path("tests/.generated_status/status3.json")
        state_path = Path("tests/.generated_status/trade_history3.json")
        shutil.rmtree(status_path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(status_path), state_file=str(state_path))
            writer.record_trade(
                "STOCK",
                "TSLA",
                "PROFIT",
                Decimal("101.00"),
                "order-1",
                pnl=Decimal("5.00"),
                entry_price=Decimal("96.00"),
            )

            # A fresh instance (simulating a restart) pointed at the same
            # state file must rehydrate the trade instead of starting empty.
            restarted = StatusWriter(str(status_path), state_file=str(state_path))
            self.assertEqual(len(restarted.trades), 1)
            self.assertEqual(restarted.trades[0]["symbol"], "TSLA")
            self.assertEqual(restarted.trades[0]["entry_price"], "96.00")
        finally:
            shutil.rmtree(status_path.parent, ignore_errors=True)

    def test_statuswriter_without_state_file_starts_empty_and_does_not_persist(self):
        status_path = Path("tests/.generated_status/status4.json")
        shutil.rmtree(status_path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(status_path))
            writer.record_trade("STOCK", "TSLA", "BUY", Decimal("100.00"), "order-1")
            self.assertEqual(len(writer.trades), 1)
            self.assertIsNone(writer.state_path)
        finally:
            shutil.rmtree(status_path.parent, ignore_errors=True)

    def test_recorded_exit_trade_includes_entry_price(self):
        status_path = Path("tests/.generated_status/status5.json")
        shutil.rmtree(status_path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(status_path))
            writer.record_trade(
                "STOCK",
                "TSLA",
                "STOP",
                Decimal("95.00"),
                "order-1",
                pnl=Decimal("-5.00"),
                entry_price=Decimal("100.00"),
            )
            trade = writer.trades[0]
            self.assertEqual(trade["entry_price"], "100.00")
            self.assertEqual(trade["limit_price"], "95.00")
            self.assertEqual(trade["pnl"], "-5.00")
        finally:
            shutil.rmtree(status_path.parent, ignore_errors=True)

    def test_recorded_entry_trade_has_no_entry_price(self):
        status_path = Path("tests/.generated_status/status6.json")
        shutil.rmtree(status_path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(status_path))
            writer.record_trade("STOCK", "TSLA", "BUY", Decimal("100.00"), "order-1")
            self.assertIsNone(writer.trades[0]["entry_price"])
        finally:
            shutil.rmtree(status_path.parent, ignore_errors=True)

    def test_write_excludes_still_pending_orders_from_recent_trades(self):
        """By request: "no pending order should go into the recent
        trades." record_trade writes to self.trades optimistically at
        order-submission time (before it's actually filled) - write()
        now filters the DISPLAYED recent-trades list against whatever's
        passed as still-pending, so a resting (not yet filled, not yet
        cancelled) order shows in pending_orders but not recent_trades
        at the same time.
        """
        status_path = Path("tests/.generated_status/status_pending_filter.json")
        shutil.rmtree(status_path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(status_path))
            writer.record_trade("STOCK", "TSLA", "BUY", Decimal("100.00"), "order-1")
            writer.record_trade("STOCK", "AAPL", "PROFIT", Decimal("50.00"), "order-2")
            writer.write(
                mode="LIVE",
                buying_power=Decimal("1000"),
                positions=[],
                watchlist=[],
                agent_summary=None,
                paused=False,
                stock_count=10,
                option_count=0,
                pending_orders=[
                    {
                        "order_id": "order-1",
                        "instrument_type": "STOCK",
                        "symbol": "TSLA",
                        "action": "BUY",
                        "limit_price": "100.00",
                        "age_seconds": 3,
                        "cancel_requested": False,
                    }
                ],
            )
            payload = json.loads(status_path.read_text(encoding="utf-8"))
            symbols = [trade["symbol"] for trade in payload["recent_trades"]]
            self.assertNotIn("TSLA", symbols)
            self.assertIn("AAPL", symbols)
        finally:
            shutil.rmtree(status_path.parent, ignore_errors=True)

    def test_discard_trade_removes_only_the_matching_order(self):
        """Regression test for a live incident: a cancelled order's
        optimistically-recorded trade-log entry stayed on the dashboard's
        Recent Trades list forever, shown as a completed profit that never
        happened - see AutoTrader.reverse_phantom_exit.
        """
        status_path = Path("tests/.generated_status/status7.json")
        shutil.rmtree(status_path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(status_path))
            writer.record_trade(
                "STOCK", "TSLA", "PROFIT", Decimal("101.00"), "order-1",
                pnl=Decimal("5.00"),
            )
            writer.record_trade(
                "STOCK", "AAPL", "PROFIT", Decimal("201.00"), "order-2",
                pnl=Decimal("3.00"),
            )
            writer.discard_trade("order-1")
            self.assertEqual(len(writer.trades), 1)
            self.assertEqual(writer.trades[0]["order_id"], "order-2")
        finally:
            shutil.rmtree(status_path.parent, ignore_errors=True)

    def test_discard_trade_for_an_unknown_order_id_is_a_safe_noop(self):
        status_path = Path("tests/.generated_status/status8.json")
        shutil.rmtree(status_path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(status_path))
            writer.record_trade(
                "STOCK", "TSLA", "PROFIT", Decimal("101.00"), "order-1",
                pnl=Decimal("5.00"),
            )
            writer.discard_trade("order-does-not-exist")
            self.assertEqual(len(writer.trades), 1)
        finally:
            shutil.rmtree(status_path.parent, ignore_errors=True)

    def test_discarded_trade_stays_gone_after_a_new_statuswriter_instance(self):
        status_path = Path("tests/.generated_status/status9.json")
        state_path = Path("tests/.generated_status/trade_history9.json")
        shutil.rmtree(status_path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(status_path), state_file=str(state_path))
            writer.record_trade(
                "STOCK", "TSLA", "PROFIT", Decimal("101.00"), "order-1",
                pnl=Decimal("5.00"),
            )
            writer.discard_trade("order-1")

            restarted = StatusWriter(str(status_path), state_file=str(state_path))
            self.assertEqual(len(restarted.trades), 0)
        finally:
            shutil.rmtree(status_path.parent, ignore_errors=True)

    def test_rekey_trade_lets_a_later_discard_find_the_repriced_entry(self):
        """Regression test for a live incident: CTRM's PROFIT order was
        cancelled and repriced (see AutoTrader.reprice_resting_exits) -
        the visible Recent Trades entry was still filed under the
        original (now-cancelled) order_id, but the eventual "never
        filled" reversal only ever learns the newest order_id. Without
        rekey_trade, discard_trade(new_order_id) finds nothing to remove,
        and the cancelled order's phantom profit stays on the dashboard
        forever - it sat there for 2.5+ hours before this fix.
        """
        status_path = Path("tests/.generated_status/status11.json")
        shutil.rmtree(status_path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(status_path))
            writer.record_trade(
                "STOCK", "CTRM", "PROFIT", Decimal("2.38"), "order-1",
                pnl=Decimal("0.05"),
            )
            # Two reprices, matching the live incident's cancel-and-
            # replace chain: order-1 -> order-2 -> order-3.
            writer.rekey_trade("order-1", "order-2")
            writer.rekey_trade("order-2", "order-3")
            writer.discard_trade("order-3")
            self.assertEqual(len(writer.trades), 0)
        finally:
            shutil.rmtree(status_path.parent, ignore_errors=True)

    def test_rekey_trade_for_an_unknown_order_id_is_a_safe_noop(self):
        status_path = Path("tests/.generated_status/status12.json")
        shutil.rmtree(status_path.parent, ignore_errors=True)
        try:
            writer = StatusWriter(str(status_path))
            writer.record_trade(
                "STOCK", "CTRM", "PROFIT", Decimal("2.38"), "order-1",
                pnl=Decimal("0.05"),
            )
            writer.rekey_trade("order-does-not-exist", "order-2")
            self.assertEqual(len(writer.trades), 1)
            self.assertEqual(writer.trades[0]["order_id"], "order-1")
        finally:
            shutil.rmtree(status_path.parent, ignore_errors=True)


class DashboardCommandTests(unittest.TestCase):
    def test_command_queue_round_trip(self):
        path = Path("tests/.generated_commands/commands.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            queue = CommandQueue(str(path))
            self.assertEqual(queue.pop_all(), [])
            command_id = queue.enqueue("close_all")
            self.assertTrue(command_id)
            popped = queue.pop_all()
            self.assertEqual(len(popped), 1)
            self.assertEqual(popped[0]["type"], "close_all")
            self.assertEqual(popped[0]["id"], command_id)
            self.assertEqual(queue.pop_all(), [])
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_command_queue_accumulates_multiple_commands(self):
        path = Path("tests/.generated_commands/commands2.json")
        shutil.rmtree(path.parent, ignore_errors=True)
        try:
            queue = CommandQueue(str(path))
            queue.enqueue("sell", symbol="TSLA", instrument_type="EQUITY")
            queue.enqueue("watchlist_add", symbol="AAPL")
            popped = queue.pop_all()
            self.assertEqual([c["type"] for c in popped], ["sell", "watchlist_add"])
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_option_exits_are_evaluated_on_the_fast_thread(self):
        """The single biggest reason a winner round-tripped into a
        loser.

        _evaluate_option_exit computes the profit target, the stop, the
        profit-lock trail and the stale exit, AND records
        option_peak_price - but it was only ever reached from
        trade_options inside the slow scan. Measured live 2026-09-22,
        consecutive SCAN lines were 13:13:02, 13:17:50 and 13:24:33:
        the entire exit ladder sampled every 5-7 minutes.

        A trail cannot protect a high it never observed. GME ran $1.40
        -> $1.46 -> $1.37 between cycles, and the user had to close
        positions by hand to capture the gains.
        """
        import inspect

        from webull_bot.trading.orders.position_protection_loop import (
            _position_protection_loop,
        )

        body = inspect.getsource(_position_protection_loop)
        self.assertIn(
            "self.evaluate_held_option_exits()",
            body,
            "held-option exits must be evaluated on the 0.25s thread, "
            "not once per full universe scan",
        )

    def test_held_option_exits_skip_positions_with_a_resting_exit(self):
        """The fast thread and the slow scan both reach
        _evaluate_option_exit, so this must not double-submit.
        """
        from types import SimpleNamespace

        from webull_bot.bot import AutoTrader

        calls = []
        fake = SimpleNamespace(
            config=SimpleNamespace(
                held_option_exit_enabled=True,
                held_option_exit_seconds=Decimal("0"),
            ),
            last_held_option_exit_scan=0.0,
            cached_positions=[
                {
                    "instrument_type": "OPTION",
                    "symbol": "NFLX",
                    "quantity": "1",
                    "cost_price": "2.00",
                }
            ],
            pending_option_exits=set(),
            cached_option_buying_power=Decimal("100"),
            api=SimpleNamespace(
                contract_from_position=lambda p: {
                    "symbol": "NFLXP", "expiration_date": "2026-10-09",
                },
                option_quotes=lambda syms: calls.append(syms) or [],
            ),
            has_pending_sell_order=lambda key: True,
        )
        fake._evaluate_option_exit = lambda *a, **k: calls.append("EVALUATED")
        run = AutoTrader.evaluate_held_option_exits.__get__(fake)
        run()
        self.assertEqual(
            calls, [], "a position with a resting exit must be skipped"
        )

    def test_ui_commands_run_on_the_fast_thread_not_the_slow_scan(self):
        """By request: "the manual sell button or cancel button or buy
        buttons are very slow and not working properly, they should be
        put on a separate thread".

        process_ui_commands used to run inline in the main loop, so a
        click was only acted on once per full scan cycle. Measured live
        2026-09-22 with 3,408 discovered contracts and the host under
        load, consecutive SCAN lines were 13:13:02, 13:17:50, 13:24:33
        - a Sell could sit queued for SEVEN MINUTES.

        It must be dispatched by the 0.25s protection loop, and must
        NOT also remain in the main loop, or one click would place two
        orders.
        """
        import inspect

        from webull_bot.trading.main_loop import run
        from webull_bot.trading.orders.position_protection_loop import (
            _position_protection_loop,
        )

        fast = inspect.getsource(_position_protection_loop)
        slow = inspect.getsource(run)
        self.assertIn(
            "process_ui_commands",
            fast,
            "dashboard commands must be dispatched by the fast thread",
        )
        self.assertNotIn(
            "self.process_ui_commands(",
            slow,
            "the slow scan loop must not also dispatch them - two "
            "dispatchers means one click places two orders",
        )

    def test_ui_commands_are_dispatched_before_the_repricers(self):
        """A manual action must take effect before automated order
        management reasons about the same position.
        """
        import inspect

        from webull_bot.trading.orders.position_protection_loop import (
            _position_protection_loop,
        )

        body = inspect.getsource(_position_protection_loop)
        # Compare the real call sites, not bare names - both appear in
        # the surrounding comments too.
        self.assertLess(
            body.index("self.process_ui_commands("),
            body.index("self.monitor_working_orders()"),
            "manual commands must run first in the tick",
        )

    def test_process_ui_commands_dispatches_close_all(self):
        from webull_bot.bot import AutoTrader

        calls = []
        fake_bot = SimpleNamespace(
            commands=SimpleNamespace(pop_all=lambda: [{"type": "close_all"}]),
            close_instruments=lambda types: calls.append(types),
        )
        process = AutoTrader.process_ui_commands.__get__(fake_bot)
        process([])

        self.assertEqual(calls, [{"EQUITY", "OPTION"}])

    def test_process_ui_commands_survives_unknown_type_and_handler_error(self):
        from webull_bot.bot import AutoTrader

        def boom(command, positions, core_session_active=False):
            raise RuntimeError("boom")

        fake_bot = SimpleNamespace(
            commands=SimpleNamespace(
                pop_all=lambda: [{"type": "unknown"}, {"type": "sell"}]
            ),
            _manual_sell=boom,
        )
        process = AutoTrader.process_ui_commands.__get__(fake_bot)
        process([])  # must not raise despite the unknown type and handler error

    def test_manual_cancel_order_cancels_a_tracked_working_order(self):
        from webull_bot.bot import AutoTrader

        cancelled = []
        fake_bot = SimpleNamespace(
            working_orders={
                "order-1": {
                    "key": "STOCK:TSLA",
                    "action": "STOP",
                    "cancel_requested_at": None,
                }
            },
            api=SimpleNamespace(cancel=lambda order_id: cancelled.append(order_id)),
        )
        cancel = AutoTrader._manual_cancel_order.__get__(fake_bot)

        cancel({"order_id": "order-1"})

        self.assertEqual(cancelled, ["order-1"])
        self.assertIsNotNone(
            fake_bot.working_orders["order-1"]["cancel_requested_at"]
        )

    def test_manual_cancel_order_skips_unknown_or_already_requested(self):
        from webull_bot.bot import AutoTrader

        cancelled = []
        fake_bot = SimpleNamespace(
            working_orders={
                "order-2": {
                    "key": "STOCK:AAPL",
                    "action": "PROFIT",
                    "cancel_requested_at": 123.0,
                }
            },
            api=SimpleNamespace(cancel=lambda order_id: cancelled.append(order_id)),
        )
        cancel = AutoTrader._manual_cancel_order.__get__(fake_bot)

        cancel({"order_id": "does-not-exist"})
        cancel({"order_id": "order-2"})  # already cancel-requested

        self.assertEqual(cancelled, [])

    def test_manual_buy_sizes_by_dollars_during_core_session(self):
        from webull_bot.bot import AutoTrader

        placed = []
        config = Settings(
            stock_core_session_position_fraction=Decimal("0.10"),
            max_order_notional=Decimal("1000"),
            max_open_positions=10,
        )
        fake_bot = SimpleNamespace(
            config=config,
            broker_conflict_symbols=set(),
            wash_sales=SimpleNamespace(blocked_until=lambda symbol: None),
            fractional_trading_enabled=True,
            position_buckets={},
            strategy=SimpleNamespace(
                open_position_count=TradingStrategy.open_position_count,
                update_stock_snapshot=lambda quote, price: None,
                dollar_stock_quantity=TradingStrategy.dollar_stock_quantity.__get__(
                    TradingStrategy(config)
                ),
            ),
            api=SimpleNamespace(
                stock_position=lambda symbol, positions: (Decimal("0"), Decimal("0")),
                stock_quote=lambda symbol: {"bid": "49.90", "ask": "50.10"},
                quote_price=lambda quote: Decimal("50.00"),
                stock_limit_price=lambda quote, side: Decimal("50.00"),
                place_stock=lambda symbol, side, quantity, limit_price=None, fractional=False: (
                    placed.append((symbol, side, quantity, fractional)) or "order-1"
                ),
            ),
        )
        recorded = []
        fake_bot.record_trade = lambda *a, **k: recorded.append((a, k))
        manual_buy = AutoTrader._manual_buy.__get__(fake_bot)

        remaining = manual_buy(
            {"symbol": "MSFT"}, [], Decimal("1000"), True
        )

        self.assertEqual(len(placed), 1)
        symbol, side, quantity, fractional = placed[0]
        self.assertEqual((symbol, side), ("MSFT", "BUY"))
        self.assertTrue(fractional)
        # 10% of $1000 = $100 target notional at ~$50/share -> ~2 shares,
        # well above fractional_stock_quantity's old 1-share cap.
        self.assertGreater(quantity, Decimal("1"))
        self.assertLess(remaining, Decimal("1000"))
        self.assertEqual(fake_bot.position_buckets.get("MSFT"), "MANUAL")
        # Regression coverage: a manual buy's dashboard row must show the
        # price paid, not a blank Entry column.
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0][1].get("entry_price"), Decimal("50.00"))

    def test_manual_buy_skips_when_already_holding_a_position(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            broker_conflict_symbols=set(),
            api=SimpleNamespace(
                stock_position=lambda symbol, positions: (Decimal("5"), Decimal("40")),
            ),
        )
        manual_buy = AutoTrader._manual_buy.__get__(fake_bot)

        remaining = manual_buy({"symbol": "MSFT"}, [], Decimal("1000"))

        self.assertEqual(remaining, Decimal("1000"))

    def test_process_ui_commands_dispatches_buy_and_threads_buying_power(self):
        from webull_bot.bot import AutoTrader

        calls = []
        fake_bot = SimpleNamespace(
            commands=SimpleNamespace(
                pop_all=lambda: [{"type": "buy", "symbol": "MSFT"}]
            ),
            _manual_buy=lambda command, positions, buying_power, core_session_active: (
                calls.append((command, buying_power, core_session_active))
                or Decimal("42")
            ),
        )
        process = AutoTrader.process_ui_commands.__get__(fake_bot)

        result = process([], Decimal("1000"), True)

        self.assertEqual(result, Decimal("42"))
        self.assertEqual(calls, [({"type": "buy", "symbol": "MSFT"}, Decimal("1000"), True)])

    def test_manual_sell_closes_the_option_even_when_told_equity(self):
        """Live incident 2026-09-22: a dashboard Sell on an OPTION
        position was dispatched as EQUITY (the request model defaults
        instrument_type to EQUITY, so anything missing or mistyped
        silently becomes a stock sell). It logged
        ORDER | STOCK | MANUAL_SELL | GME against an account holding no
        GME shares; the real option position stayed open and had to be
        closed in the broker app.

        A sell is an unambiguous "get me out of this symbol", so when
        exactly one open position matches, close THAT one.
        """
        from webull_bot.bot import AutoTrader

        placed = []
        fake_bot = SimpleNamespace(
            pending_stock_exits=set(),
            pending_option_exits=set(),
            fractional_trading_enabled=True,
            is_fractional_quantity=AutoTrader.is_fractional_quantity,
            api=SimpleNamespace(
                contract_from_position=lambda position: {
                    "symbol": "GME261009C00023000",
                    "underlying_symbol": "GME",
                },
                option_quote=lambda symbol: {"bid": "1.40", "ask": "1.45"},
                quote_ask=lambda quote: Decimal(str(quote["ask"])),
                option_limit_price=lambda quote, side: Decimal("1.42"),
                place_option=lambda contract, side, quantity, price, action: (
                    placed.append((contract["symbol"], side, quantity, price))
                    or "order-opt-1"
                ),
                place_stock=lambda *a, **k: placed.append(("STOCK-ORDER",)) or "bad",
            ),
            wash_sales=SimpleNamespace(block=lambda symbol, reason: None),
        )
        from webull_bot.trading.orders.option_exit_claim import (
            _claim_option_exit,
            _release_option_exit,
        )

        fake_bot._claim_option_exit = _claim_option_exit.__get__(fake_bot)
        fake_bot._release_option_exit = _release_option_exit.__get__(fake_bot)
        fake_bot.record_realized_exit = lambda cost, price, qty, multiplier=1: Decimal("1")
        fake_bot.record_trade = lambda *a, **k: None
        manual_sell = AutoTrader._manual_sell.__get__(fake_bot)

        positions = [
            {
                "instrument_type": "OPTION",
                "symbol": "GME",
                "quantity": "2",
                "cost_price": "1.40",
            }
        ]
        manual_sell({"symbol": "GME", "instrument_type": "EQUITY"}, positions)

        self.assertEqual(len(placed), 1, "exactly one order must go out")
        self.assertEqual(
            placed[0][0],
            "GME261009C00023000",
            "the OPTION contract must be sold, not a phantom stock order",
        )
        self.assertNotIn(
            ("STOCK-ORDER",), placed, "must not place a stock order"
        )

    def test_manual_sell_respects_the_declared_type_when_both_are_held(self):
        """Ambiguity is the one case the fallback must NOT guess at -
        holding both the stock and options on one symbol means closing
        the wrong one is a real risk.
        """
        from webull_bot.bot import AutoTrader

        placed = []
        fake_bot = SimpleNamespace(
            pending_stock_exits=set(),
            pending_option_exits=set(),
            fractional_trading_enabled=True,
            is_fractional_quantity=AutoTrader.is_fractional_quantity,
            api=SimpleNamespace(
                stock_quote=lambda symbol: {"bid": "20.00", "ask": "20.10"},
                quote_ask=lambda quote: Decimal(str(quote["ask"])),
                stock_limit_price=lambda quote, side: Decimal("20.00"),
                place_stock=lambda symbol, side, quantity, limit_price=None, fractional=False, market=False: (
                    placed.append(("STOCK", symbol, quantity)) or "order-1"
                ),
            ),
            wash_sales=SimpleNamespace(block=lambda symbol, reason: None),
        )
        from webull_bot.trading.orders.option_exit_claim import (
            _claim_option_exit,
            _release_option_exit,
        )

        fake_bot._claim_option_exit = _claim_option_exit.__get__(fake_bot)
        fake_bot._release_option_exit = _release_option_exit.__get__(fake_bot)
        fake_bot.record_realized_exit = lambda cost, price, qty, multiplier=1: Decimal("1")
        fake_bot.record_trade = lambda *a, **k: None
        manual_sell = AutoTrader._manual_sell.__get__(fake_bot)

        positions = [
            {"instrument_type": "EQUITY", "symbol": "GME", "quantity": "5", "cost_price": "20.00"},
            {"instrument_type": "OPTION", "symbol": "GME", "quantity": "2", "cost_price": "1.40"},
        ]
        manual_sell({"symbol": "GME", "instrument_type": "EQUITY"}, positions)
        self.assertEqual(placed, [("STOCK", "GME", Decimal("5"))])

    def test_manual_sell_prices_at_the_ask_outside_core_session(self):
        from webull_bot.bot import AutoTrader

        placed = []
        recorded_pnl = []

        fake_bot = SimpleNamespace(
            pending_stock_exits=set(),
            pending_option_exits=set(),
            fractional_trading_enabled=True,
            is_fractional_quantity=AutoTrader.is_fractional_quantity,
            api=SimpleNamespace(
                stock_quote=lambda symbol: {"bid": "99.00", "ask": "99.20"},
                quote_ask=lambda quote: Decimal(str(quote["ask"])),
                stock_limit_price=lambda quote, side: Decimal("99.00"),
                place_stock=lambda symbol, side, quantity, limit_price=None, fractional=False, market=False: (
                    placed.append((symbol, side, quantity, limit_price, fractional, market))
                    or "order-1"
                ),
            ),
            wash_sales=SimpleNamespace(block=lambda symbol, reason: None),
        )
        from webull_bot.trading.orders.option_exit_claim import (
            _claim_option_exit,
            _release_option_exit,
        )

        fake_bot._claim_option_exit = _claim_option_exit.__get__(fake_bot)
        fake_bot._release_option_exit = _release_option_exit.__get__(fake_bot)
        fake_bot.record_realized_exit = lambda cost, price, qty, multiplier=1: (
            recorded_pnl.append((cost, price, qty)) or (price - cost) * qty * multiplier
        )
        fake_bot.record_trade = lambda *a, **k: None
        manual_sell = AutoTrader._manual_sell.__get__(fake_bot)

        positions = [
            {
                "instrument_type": "EQUITY",
                "symbol": "TSLA",
                "quantity": "3",
                "cost_price": "100.00",
            }
        ]
        # core_session_active defaults to False - a top-of-spread LIMIT
        # order, not a MARKET order, since MARKET orders outside core
        # hours aren't reliably supported.
        manual_sell({"symbol": "TSLA", "instrument_type": "EQUITY"}, positions)

        self.assertEqual(
            placed,
            [("TSLA", "SELL", Decimal("3"), Decimal("99.20"), False, False)],
        )
        self.assertIn("TSLA", fake_bot.pending_stock_exits)
        self.assertEqual(recorded_pnl, [(Decimal("100.00"), Decimal("99.20"), Decimal("3"))])

    def test_manual_sell_uses_a_market_order_during_core_session(self):
        from webull_bot.bot import AutoTrader

        placed = []

        fake_bot = SimpleNamespace(
            pending_stock_exits=set(),
            pending_option_exits=set(),
            fractional_trading_enabled=True,
            is_fractional_quantity=AutoTrader.is_fractional_quantity,
            api=SimpleNamespace(
                stock_quote=lambda symbol: {"bid": "99.00", "ask": "99.20"},
                quote_ask=lambda quote: Decimal(str(quote["ask"])),
                stock_limit_price=lambda quote, side: Decimal("99.00"),
                place_stock=lambda symbol, side, quantity, limit_price=None, fractional=False, market=False: (
                    placed.append((symbol, side, quantity, limit_price, fractional, market))
                    or "order-1"
                ),
            ),
            wash_sales=SimpleNamespace(block=lambda symbol, reason: None),
        )
        from webull_bot.trading.orders.option_exit_claim import (
            _claim_option_exit,
            _release_option_exit,
        )

        fake_bot._claim_option_exit = _claim_option_exit.__get__(fake_bot)
        fake_bot._release_option_exit = _release_option_exit.__get__(fake_bot)
        fake_bot.record_realized_exit = lambda cost, price, qty, multiplier=1: (
            price - cost
        ) * qty * multiplier
        fake_bot.record_trade = lambda *a, **k: None
        manual_sell = AutoTrader._manual_sell.__get__(fake_bot)

        positions = [
            {
                "instrument_type": "EQUITY",
                "symbol": "TSLA",
                "quantity": "3",
                "cost_price": "100.00",
            }
        ]
        manual_sell(
            {"symbol": "TSLA", "instrument_type": "EQUITY"},
            positions,
            True,
        )

        self.assertEqual(
            placed,
            [("TSLA", "SELL", Decimal("3"), None, False, True)],
        )

    def test_manual_sell_of_a_fractional_position_never_uses_market(self):
        """A fractional-quantity position must still go through the
        fractional order machinery (MARKET+CORE forced by fractional=True
        already) rather than the plain market=True path, even during core
        hours - the two paths shouldn't both try to force MARKET at once.
        """
        from webull_bot.bot import AutoTrader

        placed = []

        fake_bot = SimpleNamespace(
            pending_stock_exits=set(),
            pending_option_exits=set(),
            fractional_trading_enabled=True,
            is_fractional_quantity=AutoTrader.is_fractional_quantity,
            api=SimpleNamespace(
                stock_quote=lambda symbol: {"bid": "99.00", "ask": "99.20"},
                quote_ask=lambda quote: Decimal(str(quote["ask"])),
                stock_limit_price=lambda quote, side: Decimal("99.00"),
                place_stock=lambda symbol, side, quantity, limit_price=None, fractional=False, market=False: (
                    placed.append((symbol, side, quantity, limit_price, fractional, market))
                    or "order-1"
                ),
            ),
            wash_sales=SimpleNamespace(block=lambda symbol, reason: None),
        )
        from webull_bot.trading.orders.option_exit_claim import (
            _claim_option_exit,
            _release_option_exit,
        )

        fake_bot._claim_option_exit = _claim_option_exit.__get__(fake_bot)
        fake_bot._release_option_exit = _release_option_exit.__get__(fake_bot)
        fake_bot.record_realized_exit = lambda cost, price, qty, multiplier=1: (
            price - cost
        ) * qty * multiplier
        fake_bot.record_trade = lambda *a, **k: None
        manual_sell = AutoTrader._manual_sell.__get__(fake_bot)

        positions = [
            {
                "instrument_type": "EQUITY",
                "symbol": "TSLA",
                "quantity": "2.5",
                "cost_price": "100.00",
            }
        ]
        manual_sell(
            {"symbol": "TSLA", "instrument_type": "EQUITY"},
            positions,
            True,
        )

        placed_symbol, placed_side, placed_qty, placed_limit, placed_fractional, placed_market = (
            placed[0]
        )
        self.assertTrue(placed_fractional)
        self.assertFalse(placed_market)

    def test_manual_sell_skips_when_no_matching_position(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(pending_stock_exits=set(), pending_option_exits=set())
        manual_sell = AutoTrader._manual_sell.__get__(fake_bot)

        manual_sell({"symbol": "TSLA", "instrument_type": "EQUITY"}, [])

    def test_add_to_watchlist_resolves_category_and_appends_symbol(self):
        from webull_bot.bot import AutoTrader

        fake_bot = SimpleNamespace(
            user_watchlist=set(),
            stock_categories={},
            stock_symbols=["AAPL"],
            priority_scan_symbols=set(),
            api=SimpleNamespace(stock_categories=lambda symbols: {"TSLA": "US_STOCK"}),
        )
        add = AutoTrader.add_to_watchlist.__get__(fake_bot)

        add("tsla")

        self.assertIn("TSLA", fake_bot.user_watchlist)
        self.assertEqual(fake_bot.stock_categories.get("TSLA"), "US_STOCK")
        self.assertIn("TSLA", fake_bot.stock_symbols)
        # Regression coverage: a freshly-added symbol has zero
        # accumulated activity score and can otherwise lose out to every
        # already-active watchlist symbol in prioritized_stock_batch's
        # ranking forever - live incident, HOWL never once got scanned
        # after being added. Must be queued for a guaranteed first scan.
        self.assertIn("TSLA", fake_bot.priority_scan_symbols)


class WriteStatusSnapshotBalanceGuardTests(unittest.TestCase):
    """Live incident (user-reported, real distress: "why is it ONLY
    LOSING"): the balance chart showed the account crashing to
    literally $0 and instantly recovering, several times in one day,
    even though the real balance never moved - a transient bad
    total_equity read (e.g. cached_raw_buying_power momentarily 0
    right after the fast thread starts, before the first account_
    state() refresh lands) got written straight into balance_history.
    """

    def setUp(self):
        self.addCleanup(
            lambda: shutil.rmtree(
                Path("tests/.generated_status"), ignore_errors=True
            )
        )

    def _fake_bot(self, raw_buying_power, prior_balance=None):
        from webull_bot.bot import AutoTrader

        status = StatusWriter(
            path=str(Path("tests/.generated_status/snapshot.json"))
        )
        if prior_balance is not None:
            status.balance_history.append(
                {"time": time.time(), "balance": str(prior_balance)}
            )
        fake_bot = SimpleNamespace(
            config=SimpleNamespace(poll_seconds=Decimal("0.25"), mode="LIVE"),
            last_status_write=0.0,
            last_balance_history_write=0.0,
            position_buckets={},
            user_watchlist=set(),
            market_agent=None,
            strategy=SimpleNamespace(
                prices={},
                selection_bucket=lambda symbol: "DISCOVERY",
                metrics={},
                position_unrealized_pnl=lambda position: Decimal("0"),
                position_day_pnl=lambda position: Decimal("0"),
            ),
            cached_raw_buying_power=raw_buying_power,
            cached_account_day_pnl=None,
            cached_account_value=None,
            daily_realized_pnl=Decimal("0"),
            stock_symbols=[],
            option_contracts=[],
            working_orders={},
            status=status,
            # write_status_snapshot feeds the daily profit throttle
            # from here, since this is the only place equity is
            # computed with the option multiplier applied. These
            # tests only exercise the balance-history guard, so the
            # throttle itself is a no-op stub.
            update_profit_throttle=lambda total_equity: None,
        )
        return AutoTrader.write_status_snapshot.__get__(fake_bot), status

    def test_discards_an_implausible_zero_reading_after_a_real_balance(self):
        write, status = self._fake_bot(
            raw_buying_power=Decimal("0"), prior_balance=Decimal("363.96")
        )

        with self.assertLogs("webull-bot", level="WARNING"):
            write(positions=[], buying_power=Decimal("0"), paused=False)

        self.assertEqual(len(status.balance_history), 1)
        self.assertEqual(status.balance_history[-1]["balance"], "363.96")

    def test_records_a_genuine_zero_when_there_is_no_prior_balance_yet(self):
        # A real $0 balance on a fresh account (nothing recorded yet)
        # must still be recorded - the guard only discards a $0 blip
        # that contradicts an already-known nonzero balance.
        write, status = self._fake_bot(raw_buying_power=Decimal("0"))

        write(positions=[], buying_power=Decimal("0"), paused=False)

        self.assertEqual(len(status.balance_history), 1)
        self.assertEqual(status.balance_history[-1]["balance"], "0")

    def test_option_positions_count_their_full_100x_contract_value(self):
        """Live incident, found by reconciling the balance chart
        against the broker: the account showed a ~$75 "loss" over a
        morning it was actually flat. total_equity multiplied
        quantity * last_price for every row, so a $0.475 OPTION
        contract counted as 48 CENTS instead of $47.50 - buying an
        option moved real cash out of buying_power but added only
        1/100th of the position value back, making every option entry
        look like an instant ~99% loss on the chart.
        """
        write, status = self._fake_bot(raw_buying_power=Decimal("293.85"))
        positions = [
            {
                "instrument_type": "OPTION",
                "symbol": "QQQ260930P00655000",
                "quantity": "1",
                "cost_price": "0.51",
                "last_price": "0.475",
            },
            {
                "instrument_type": "OPTION",
                "symbol": "SPY260930P00700000",
                "quantity": "1",
                "cost_price": "0.30",
                "last_price": "0.275",
            },
        ]

        write(positions=positions, buying_power=Decimal("293.85"), paused=False)

        # 0.475*100 + 0.275*100 = 75.00 of real contract value, not 0.75
        self.assertEqual(
            Decimal(status.balance_history[-1]["balance"]), Decimal("368.85")
        )

    def test_a_stock_position_is_not_multiplied(self):
        write, status = self._fake_bot(raw_buying_power=Decimal("100"))
        positions = [
            {
                "instrument_type": "EQUITY",
                "symbol": "TNMG",
                "quantity": "10",
                "cost_price": "3.97",
                "last_price": "4.00",
            }
        ]

        write(positions=positions, buying_power=Decimal("100"), paused=False)

        self.assertEqual(
            Decimal(status.balance_history[-1]["balance"]), Decimal("140.00")
        )

    def test_records_a_normal_nonzero_balance_as_usual(self):
        write, status = self._fake_bot(
            raw_buying_power=Decimal("400"), prior_balance=Decimal("363.96")
        )

        write(positions=[], buying_power=Decimal("400"), paused=False)

        self.assertEqual(len(status.balance_history), 2)
        self.assertEqual(status.balance_history[-1]["balance"], "400")
