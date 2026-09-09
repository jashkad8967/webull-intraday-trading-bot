import logging
import threading
import time
from datetime import timedelta

from webull_bot.webull_api import MarketDataPermissionError

log = logging.getLogger("webull-bot")


def run(self) -> None:
    log.info(
        "START  | mode=%s | poll=%ss | cooldown=%ss",
        self.config.mode,
        self.config.poll_seconds,
        self.config.trade_cooldown_seconds,
    )
    threading.Thread(
        target=self._position_protection_loop, daemon=True
    ).start()
    while True:
        moment = self.now()
        if not self.is_trading_day(moment):
            time.sleep(60)
            continue

        market_open = self.session_moment(moment, self.config.market_open_time)
        closeout = self.session_moment(moment, self.config.eod_close_time)
        market_close = self.session_moment(moment, self.config.market_close_time)
        option_open = self.session_moment(
            moment,
            self.config.option_market_open_time,
        )
        option_closeout = self.session_moment(
            moment,
            self.config.option_eod_close_time,
        )
        option_close = self.session_moment(
            moment,
            self.config.option_market_close_time,
        )

        if moment < market_open:
            # By request: "get the top gainers before the day
            # starts" - fetched here specifically so it still runs
            # during the pre-market wait below, not skipped by the
            # early continue on this branch. Once-per-day guarded
            # internally (see refresh_premarket_gainers), so this
            # is a no-op on every later tick of the same wait.
            self.refresh_premarket_gainers(moment)
            self.refresh_agent_predicted_gainers(moment)
            time.sleep(min(60, max(1, (market_open - moment).total_seconds())))
            continue

        if closeout <= moment < market_close:
            finished = self.close_instruments(
                {"EQUITY"}, apply_overnight_hold=True
            )
            time.sleep(60 if finished else self.config.eod_retry_seconds)
            continue

        if moment >= market_close:
            self.log_day_end_summary(moment)
            time.sleep(60)
            continue

        if option_closeout <= moment < option_close:
            self.close_instruments({"OPTION"})
            self.close_fractional_positions_before_core_close()

        opening_grace_active = option_open <= moment < option_open + timedelta(
            minutes=self.config.opening_grace_minutes
        )
        core_session_active = option_open <= moment < option_close
        # Read by _position_protection_loop (a separate thread) -
        # see its docstring. Plain attribute assignment is atomic
        # under the GIL, same convention already used for
        # stock_symbols/stock_categories in resolve_targets.
        self.cached_core_session_active = core_session_active
        if opening_grace_active and self.opening_grace_logged_date != moment.date():
            self.opening_grace_logged_date = moment.date()
            log.info(
                "GATES  | opening grace window active for %s minutes | "
                "spread/extension gates relaxed %sx/%sx near the bell",
                self.config.opening_grace_minutes,
                self.config.opening_grace_spread_multiplier,
                self.config.opening_grace_extension_multiplier,
            )

        cycle_started = time.monotonic()
        try:
            self.resolve_targets(moment)
            # Fallback call site for a restart that lands AFTER
            # market_open (common - most deploys land mid-session,
            # not during the pre-market wait above) - without this,
            # refresh_premarket_gainers would never fire that day
            # at all. Its own internal once-per-day guard makes
            # this a no-op on every cycle after the first.
            self.refresh_premarket_gainers(moment)
            self.refresh_agent_predicted_gainers(moment)
            # monitor_working_orders/the repricers/escalate_stalled_
            # stop_losses now run on their own fast, dedicated
            # thread (see _position_protection_loop, started once
            # above) - NOT called here too, or they'd run twice
            # concurrently and double-cancel/double-reprice the
            # same working orders.
            self.process_iceberg_orders()
            if not core_session_active:
                self.close_profitable_positions_during_extended_hours()
            self.select_volatility_scalp_symbols()
            self.reconcile_order_history()
            self.log_trade_events()
            buying_power, positions = self.account_state()
            buying_power = self.process_ui_commands(
                positions, buying_power, core_session_active
            )
            circuit_active = self.handle_portfolio_circuit_breaker(
                positions,
                buying_power,
            )
            if not circuit_active:
                circuit_active = self.handle_daily_loss_breaker()
            if not circuit_active:
                # By request: "at least you would be able to see
                # which contracts are there for core hours later on"
                # - discovery (listing strikes/expirations, plus a
                # best-effort affordability pick) doesn't need a
                # live, currently-tradable option quote to be useful,
                # only the underlying's own stock price (already
                # scanned across the wider 4am-8pm stock session -
                # see discover_option_contracts' own "underlying not
                # in self.strategy.prices" gate) - and select_atm_
                # options already falls back gracefully to a plain
                # nearest-ATM pick if a pre/post-market affordability
                # quote batch comes back stale or empty. Running this
                # across the whole stock session instead of only
                # inside the narrow option trading window means the
                # contract list (persisted - see OptionContractsState
                # Store) is already warm by the time real option
                # trading opens at 9:30, instead of starting the slow
                # discovery ramp-up cold every morning.
                self.discover_option_contracts()
                # By explicit request: "I want priority to option
                # trades, so make sure you buy options first."
                # Options and stocks draw from completely separate
                # buying-power pools (see account_state/
                # cached_option_buying_power's own comment), so
                # this reordering doesn't take capital away from
                # stocks - it just means an option candidate gets
                # first crack at this cycle's evaluation instead of
                # being evaluated after (and therefore effectively
                # de-prioritized behind) the general/scalp stock
                # paths every single cycle. Actual order PLACEMENT
                # still only happens inside real option trading
                # hours - options have no extended-hours market at
                # all, so an order outside this window would just be
                # rejected.
                if option_open <= moment < option_closeout:
                    buying_power = self.trade_options(positions, buying_power)
                buying_power = self.trade_pairs(positions, buying_power)
                buying_power = self.trade_stocks(
                    positions,
                    buying_power,
                    opening_grace_active,
                    core_session_active,
                )
                self.boost_stalled_positions(
                    positions,
                    option_open <= moment < option_closeout,
                    core_session_active,
                )
                self.cached_buying_power = buying_power
                self.cached_positions = [dict(item) for item in positions]
                self.submit_strategy_review(positions, buying_power)
            self.write_status_snapshot(positions, buying_power, circuit_active)
            if time.monotonic() - self.last_status_log >= 1:
                self.last_status_log = time.monotonic()
                log.info(
                    "SCAN   | stocks=%s/%s | options=%s/%s | positions=%s | "
                    "buying power=$%.2f | pnl today=$%.2f | watchlist=%s | paused=%s",
                    min(self.last_scan_batch_size, len(self.stock_symbols))
                    or min(self.config.stock_batch_size, len(self.stock_symbols)),
                    len(self.stock_symbols),
                    min(self.config.option_batch_size, len(self.option_contracts)),
                    len(self.option_contracts),
                    self.strategy.open_position_count(positions),
                    buying_power,
                    self.daily_realized_pnl,
                    len(self.user_watchlist),
                    "YES" if circuit_active else "NO",
                )
                if self.gate_rejections:
                    top_reasons = sorted(
                        self.gate_rejections.items(),
                        key=lambda item: item[1],
                        reverse=True,
                    )[:5]
                    log.info(
                        "GATES  | entries not yet firing because | %s",
                        " | ".join(
                            f"{reason}={count}" for reason, count in top_reasons
                        ),
                    )
                    self.gate_rejections.clear()
                # By request: "it is not averaging down at all" -
                # by request. Its own dedicated counter/summary
                # (not the shared gate_rejections dict above) - the
                # averaging-down diagnostic (see trade_stocks)
                # otherwise gets crowded out of the top-5 GATES
                # summary by the far more numerous fresh-entry
                # rejection reasons every single cycle, leaving
                # zero real visibility into why averaging down
                # isn't firing despite being logged.
                if self.avgdown_gate_rejections:
                    avgdown_top_reasons = sorted(
                        self.avgdown_gate_rejections.items(),
                        key=lambda item: item[1],
                        reverse=True,
                    )[:5]
                    log.info(
                        "AVGDOWN| not adding because | %s",
                        " | ".join(
                            f"{reason}={count}"
                            for reason, count in avgdown_top_reasons
                        ),
                    )
                    self.avgdown_gate_rejections.clear()
                if self.option_gate_rejections:
                    option_top_reasons = sorted(
                        self.option_gate_rejections.items(),
                        key=lambda item: item[1],
                        reverse=True,
                    )[:5]
                    log.info(
                        "OPTIONS| not entering because | %s",
                        " | ".join(
                            f"{reason}={count}"
                            for reason, count in option_top_reasons
                        ),
                    )
                    self.option_gate_rejections.clear()
        except Exception as exc:
            if isinstance(exc, MarketDataPermissionError):
                log.critical("STOP   | %s", exc)
                return
            log.error("CYCLE  | failed | %s", exc)

        seconds_to_closeout = max(
            1.0,
            (closeout - self.now()).total_seconds(),
        )
        cycle_elapsed = time.monotonic() - cycle_started
        delay = max(0.0, float(self.config.poll_seconds) - cycle_elapsed)
        if delay:
            time.sleep(min(delay, seconds_to_closeout))
