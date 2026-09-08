import logging
import time
from decimal import Decimal

from webull_bot.strategy import Decision
from webull_bot.trading.handlers.broker_conflict_check import _broker_conflict
from webull_bot.trading.orders.locks import _working_orders_lock
from webull_bot.trading.orders.manual_touch import _manual_touch_active
from webull_bot.trading.orders.rate_limit_retry import _retry_once_on_rate_limit

log = logging.getLogger("webull-bot")


def escalate_stalled_stop_losses(self) -> None:
    """Cancel and re-flag an exit (stop-loss OR profit-take) for a more
    aggressive re-quote if its gentler price hasn't filled quickly.

    A stop sitting unfilled while price keeps falling turns a bounded
    loss into an unbounded one. A profit-take sitting unfilled at a
    fixed target that the market never actually reaches (thin ETF, wide
    spread) is a different failure mode with the same fix: without
    this, it cancels on the generic order timeout, then resubmits at
    the exact same unreachable price next cycle since nothing about
    the decision changed - forever, never realizing the gain.
    """
    threshold = float(self.config.stop_loss_escalate_seconds)
    now = time.monotonic()
    for symbol, submitted_at in list(self.stop_exit_submitted.items()):
        key = f"STOCK:{symbol}"
        if symbol not in self.pending_stock_exits:
            self.stop_exit_submitted.pop(symbol, None)
            self.stop_loss_escalated.discard(symbol)
            continue
        if now - submitted_at < threshold:
            continue
        # By request: "when i touch a stock stop doing anything
        # with it while i am there."
        if _manual_touch_active(self, symbol):
            continue
        # Live incident (PETZ): see _broker_conflict's docstring.
        if _broker_conflict(self, symbol):
            continue
        order_id = None
        action = None
        with _working_orders_lock(self):
            snapshot = list(self.working_orders.items())
        for oid, order in snapshot:
            if order.get("key") == key and order.get("action") in (
                "STOP",
                "PROFIT",
            ):
                order_id = oid
                action = order.get("action")
                break
        if order_id:
            try:
                _retry_once_on_rate_limit(self.api.cancel, order_id)
            except Exception as exc:
                if self.is_order_not_cancelable(exc):
                    log.warning(
                        "STOP   | %s | escalation cancel skipped | "
                        "order already resolving | %s",
                        symbol,
                        exc,
                    )
                else:
                    log.error(
                        "STOP   | %s | escalation cancel failed | %s",
                        symbol,
                        exc,
                    )
                continue
            with _working_orders_lock(self):
                order = self.working_orders.pop(order_id, None)
            # This order is being deliberately abandoned mid-flight (it
            # never filled at the gentler price) - a fresh order fires
            # its own PROFIT/STOP decision and records its own pnl next
            # cycle, so the pnl recorded at THIS order's submission has
            # to be reversed now or it inflates the daily total for an
            # exit that never actually happened.
            if order:
                self.reverse_phantom_exit(order.get("pnl"), order_id)
                self._note_exit_failure(key)
        self.stop_loss_escalated.add(symbol)
        self.pending_stock_exits.discard(symbol)
        self.stop_exit_submitted.pop(symbol, None)
        log.warning(
            "STOP   | %s | %s exit unfilled after %ss | escalating to "
            "an aggressive crossing price",
            symbol,
            (action or "pending").lower(),
            threshold,
        )
        # Live incident (CLGN): this log line has always claimed
        # "escalating to an aggressive crossing price," but nothing
        # below it ever actually placed one - the cancelled order
        # was simply abandoned here, leaving the position with NO
        # resting exit at all until trade_stocks' own quote loop
        # (SCAN cadence, observed 30-90s+ live) happened to reach
        # this symbol again. CLGN's PROFIT order stalled while still
        # genuinely above cost, got cancelled here, and then sat
        # completely unprotected for the next ~34 seconds while the
        # price crashed straight through cost and past the hard-
        # stop floor before the slow loop ever noticed - turning a
        # real, in-hand profit into a much larger loss. Reusing the
        # full stock_decision/pricing pipeline here isn't safe to
        # duplicate quickly (it carries its own careful, live-
        # incident-tuned safeguards - price sanity, lot-restriction,
        # stop-loss wick confirmation), so this stays narrow and
        # provably safe: only acts when the position has ALREADY
        # dropped to or below cost (an undeniable loss already
        # forming, not a guess), and only ever fires the same
        # aggressive-cross SELL price genuine stop escalations
        # already use elsewhere - never touches the profit-
        # preserving path when price is still genuinely above cost,
        # which keeps waiting for the slow loop's existing, already-
        # correct _stall_exit_price logic exactly as before.
        try:
            quantity, cost = self.api.stock_position(symbol, self.cached_positions)
            if quantity > 0 and cost > 0:
                quote = self._batched_quotes([symbol]).get(symbol)
                if quote is not None:
                    price = self.api.quote_price(quote)
                    # Live incident ("still selling too fast and not
                    # waiting to average down"): this used to sell
                    # the instant price <= cost, full stop - no
                    # regard for the volatility-scalp cohort's
                    # entire "average down instead of stopping out"
                    # design (volatility_scalp_exit_override,
                    # already respected everywhere else a LOSS
                    # decision is handled). SST bought 14:52:46,
                    # stalled, escalated 37s later, and got sold
                    # immediately here without ever getting a chance
                    # to average down - a real regression this fix
                    # introduced. Now runs the SAME override the
                    # rest of the codebase uses before selling: a
                    # scalp-cohort position only sells here if
                    # averaging capacity is already exhausted or the
                    # hard-stop floor is genuinely breached, exactly
                    # matching the slow loop's own logic. A non-
                    # scalp position (no averaging plan behind it)
                    # is unaffected - still sells immediately, since
                    # there's nothing to wait for.
                    should_sell = price is not None and price <= cost
                    if should_sell and (
                        self.strategy.is_volatility_scalp_eligible(symbol)
                        or symbol in self.volatility_scalp_positions
                    ):
                        self.volatility_scalp_positions.add(symbol)
                        estimated_average_down_quantity = self.strategy.volatility_scalp_share_count(
                            price,
                            buying_power=self.cached_buying_power,
                            intensity=Decimal("1"),
                        )
                        per_buy_risk_dollars = (
                            price
                            * Decimal(estimated_average_down_quantity)
                            * self.config.volatility_scalp_hard_stop_percent
                        )
                        remaining_capacity = self.strategy.averaging_down_capacity(
                            per_buy_risk_dollars,
                            self.cached_buying_power,
                            self.config.volatility_scalp_max_symbol_risk_fraction,
                            self.config.volatility_scalp_max_averaging_buys,
                        )
                        averaging_available = (
                            self.volatility_scalp_average_down_count[symbol]
                            < remaining_capacity
                        )
                        override = self.strategy.volatility_scalp_exit_override(
                            Decision("LOSS", "already at/below cost", price),
                            quantity,
                            cost,
                            price,
                            averaging_available=averaging_available,
                            symbol=symbol,
                        )
                        should_sell = override.action == "LOSS"
                    if should_sell:
                        limit_price = self.api.stock_limit_price(quote, "SELL")
                        if limit_price is not None and self.price_sanity_ok(
                            symbol, price, limit_price
                        ):
                            new_order_id = self.api.place_stock(
                                symbol,
                                "SELL",
                                quantity,
                                limit_price=limit_price,
                                fractional=self.is_fractional_quantity(quantity),
                            )
                            self.wash_sales.block(
                                symbol, "stop-loss exit submitted"
                            )
                            self.pending_stock_exits.add(symbol)
                            self.stop_exit_submitted[symbol] = time.monotonic()
                            pnl = self.record_realized_exit(
                                cost, limit_price, quantity
                            )
                            self.record_trade(
                                key,
                                new_order_id,
                                "STOP",
                                limit_price,
                                pnl=pnl,
                                entry_price=cost,
                                quantity=quantity,
                            )
                            log.warning(
                                "STOP   | %s | already at/below cost after "
                                "the stall - selling immediately instead "
                                "of waiting for the next scan",
                                symbol,
                            )
        except Exception as exc:
            if self.is_order_reverses_existing_position(exc):
                # Live incident: the cancel just above this often
                # hasn't fully cleared the broker's own book yet by
                # the time this immediate sell fires - see is_order_
                # reverses_existing_position's own docstring. The
                # symbol falls through to a fresh, uncontested stop
                # order on the very next fast-loop cycle either way.
                log.warning(
                    "STOP   | %s | immediate post-escalation sell "
                    "skipped | prior order still resolving | %s",
                    symbol,
                    exc,
                )
            else:
                log.error(
                    "STOP   | %s | immediate post-escalation sell failed | %s",
                    symbol,
                    exc,
                )
