import logging
import time
from decimal import Decimal

from webull_bot.trading.handlers.broker_conflict_check import _broker_conflict
from webull_bot.trading.orders.locks import _working_orders_lock
from webull_bot.trading.orders.manual_touch import _manual_touch_active
from webull_bot.trading.util.concurrent_dispatch import _dispatch_concurrently

log = logging.getLogger("webull-bot")


def evaluate_held_stock_exits(self) -> None:
    """By request: "we want entry and profit to be quicker." Detects
    a held LONG stock position's FIRST crossing into profit/loss
    territory - the step that previously only happened inside
    trade_stocks' slow, full-universe scan (live evidence: 30-90s+
    between cycles) - and places its exit order right here, on the
    fast poll_seconds cadence, instead of waiting for the scan to
    come back around. Once an order actually exists, it was already
    being actively managed fast (reprice_volatility_scalp_exits/
    reprice_resting_exits at 1s/5s, escalate_stalled_stop_losses at
    15s) - this closes the one remaining slow step: the very first
    detection.

    Deliberately narrow scope to keep this safe to run unreviewed
    on live capital: LONG stock positions only (no shorts/covers -
    their pricing/WASH-blocking differs enough that duplicating it
    here isn't worth the added risk for a first pass), whole-share
    only (no fractional lot-restriction edge cases). Anything
    outside this scope is simply skipped here and falls back to the
    exact same slow-loop handling as before - a pure addition,
    never a regression, since the slow loop's own handling for
    every position is completely unchanged and still runs.

    Reuses the exact same TradingStrategy.stock_decision/
    volatility_scalp_exit_override calls the slow loop uses (same
    cached multipliers - see cached_profit_target_multiplier's
    __init__ comment - so the decision computed here is identical
    to what the slow loop would eventually compute), and the same
    _stall_exit_price/stock_stop_exit_price pricing plus price-
    sanity/lot-restriction/stop-confirmation gates the slow loop
    already relies on - never a new pricing path, only the existing
    one running sooner. Also stamps stop_condition_since itself
    (mirroring the slow loop's own stamp) so the stop-loss
    confirmation window starts counting from the FIRST fast-loop
    detection, not from whenever the slow loop happens to also
    notice the same breach.
    """
    now = time.monotonic()
    if now - self.last_held_exit_scan < float(self.config.poll_seconds):
        return
    self.last_held_exit_scan = now
    with _working_orders_lock(self):
        working_snapshot = list(self.working_orders.values())
    keys_with_orders = {
        str(order.get("key"))
        for order in working_snapshot
        if order.get("action") in ("PROFIT", "STOP")
    }
    candidates = []
    for item in self.cached_positions:
        if item.get("instrument_type") != "EQUITY":
            continue
        symbol = str(item.get("symbol", "")).upper()
        if not symbol:
            continue
        quantity = Decimal(str(item.get("quantity", "0")))
        if quantity <= 0:
            continue
        key = f"STOCK:{symbol}"
        if key in keys_with_orders or symbol in self.pending_stock_exits:
            continue
        # By request: "when i touch a stock stop doing anything
        # with it while i am there."
        if _manual_touch_active(self, symbol):
            continue
        # Live incident (PETZ): broker_conflict_symbols is meant to
        # pause a symbol's exit management entirely, not just
        # entries - see _broker_conflict's own docstring.
        if _broker_conflict(self, symbol):
            continue
        candidates.append(symbol)
    if not candidates:
        return
    quotes = self._batched_quotes(candidates)

    def _evaluate_one(symbol: str) -> None:
        key = f"STOCK:{symbol}"
        try:
            quote = quotes.get(symbol)
            if quote is None:
                return
            quantity, cost = self.api.stock_position(symbol, self.cached_positions)
            if quantity <= 0 or cost <= 0:
                return
            if self.is_fractional_quantity(quantity):
                return
            price = self.api.quote_price(quote)
            if price is None:
                return
            opened_at = self.position_opened_at.get(key)
            seconds_since_entry = (
                time.monotonic() - opened_at if opened_at is not None else None
            )
            decision = self.strategy.stock_decision(
                key,
                price,
                quantity,
                cost,
                self.agent_assessment(symbol),
                self.cached_opening_grace_active,
                self.cached_idle_relaxation_multiplier,
                self.cached_idle_relaxation_amount,
                seconds_since_entry,
                self.cached_effective_core_session_active,
                self.cached_profit_target_multiplier,
                self.cached_stop_tighten_multiplier,
            )
            is_scalp_cohort = self.strategy.is_volatility_scalp_eligible(
                symbol
            ) or symbol in self.volatility_scalp_positions
            if is_scalp_cohort:
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
                decision = self.strategy.volatility_scalp_exit_override(
                    decision,
                    quantity,
                    cost,
                    price,
                    averaging_available=averaging_available,
                    symbol=symbol,
                )
            if decision.action == "LOSS":
                if symbol not in self.stop_condition_since:
                    self.stop_condition_since[symbol] = time.monotonic()
            else:
                self.stop_condition_since.pop(symbol, None)
            # By request: "we still need to prioritize profit over
            # cutting losses, so we may need to wait in some cases
            # rather than quickly sell off" / "get rid of the loss
            # fast please." This fast path now only ever acts on
            # PROFIT - a LOSS decision still gets stamped into
            # stop_condition_since above (so the slow loop's own
            # confirmation timer starts counting from the same
            # moment this fast loop first saw it, losing no real
            # time there), but the actual STOP order submission is
            # deliberately left to the slow loop only, giving a
            # losing position the slow loop's own full cycle to
            # recover before anything fires - fast to take profit,
            # patient to cut losses, by explicit request.
            if decision.action != "PROFIT":
                return
            if self.strategy.exit_blocked_by_lot_restriction(quantity, price):
                return
            if not self.price_sanity_cooldown_ready(symbol):
                return
            fee_per_share = self.config.sell_fee_dollars / quantity
            min_profit = cost * self.config.volatility_scalp_target_percent
            if is_scalp_cohort:
                limit_price = self._stall_exit_price(
                    quote,
                    cost,
                    min_profit,
                    fee_per_share,
                    max_spread_percent=(
                        self.config.volatility_scalp_max_exit_spread_percent
                    ),
                )
            else:
                ask = self.api.quote_ask(quote)
                target = decision.target_price
                if target is None:
                    return
                limit_price = max(ask, target) if ask else target
            if limit_price is None:
                return
            if not self.price_sanity_ok(symbol, price, limit_price):
                return
            # By request: "buy 20 shares, then sell 5 every 5 cents
            # it goes up... sell 10 and keep the rest for later
            # profit." Scoped to the volatility-scalp cohort's
            # PROFIT branch only - the slow loop's PROFIT path and
            # every LOSS/STOP path everywhere else always sells the
            # full quantity, matching the user's own framing that
            # this is about riding further upside, not softening a
            # loss exit.
            #
            # Live correction (this bug): a plain, never-averaged-
            # down position (GELS, PPBT) got a partial slice sold
            # off on its very first profit exit - by explicit
            # request, partial exits only make sense for a position
            # that's actually been averaged down (multiple buys at
            # different costs, so locking in part of the gain while
            # letting the rest ride against a blended cost basis is
            # meaningful); a single, un-averaged entry should just
            # sell in full on profit like it always did.
            sell_quantity = quantity
            is_partial = False
            if is_scalp_cohort and self.volatility_scalp_average_down_count[symbol] > 0:
                partial_quantity = self.strategy.volatility_scalp_partial_exit_quantity(
                    int(quantity),
                    price,
                    self.volatility_scalp_last_partial_exit_price.get(symbol),
                )
                if partial_quantity <= 0:
                    return
                if partial_quantity < quantity:
                    sell_quantity = Decimal(partial_quantity)
                    is_partial = True
            order_id = self.api.place_stock(
                symbol, "SELL", sell_quantity, limit_price=limit_price
            )
            self.pending_stock_exits.add(symbol)
            self.stop_exit_submitted[symbol] = time.monotonic()
            pnl = self.record_realized_exit(cost, limit_price, sell_quantity)
            self.record_trade(
                key, order_id, "PARTIAL_PROFIT" if is_partial else "PROFIT",
                limit_price, pnl=pnl,
                entry_price=cost, quantity=sell_quantity,
            )
            if is_partial:
                self.volatility_scalp_last_partial_exit_price[symbol] = price
                self.pending_stock_exits.discard(symbol)
            else:
                self.volatility_scalp_last_partial_exit_price.pop(symbol, None)
            log.info(
                "REPRICE| %-8s | %s (fast) | qty=%s | limit=%s | id=%s",
                symbol,
                "PARTIAL_PROFIT" if is_partial else "PROFIT",
                sell_quantity,
                limit_price,
                order_id,
            )
        except Exception as exc:
            if self.is_sell_with_no_position(exc):
                # By request ("check the logs and see what errors
                # happened, and ensure they do not happen again") -
                # live incident: cached_positions only gets
                # overwritten once per SLOW scan cycle; if that
                # refresh lands before the broker's own account state
                # has caught up to a fill that already happened, this
                # symbol can look held again for one more fast-loop
                # pass even though it's genuinely already flat -
                # Webull correctly rejects the resulting sell as an
                # attempted short. Benign and self-resolving (the
                # position really is closed), not a fault - zero the
                # stale entry immediately (same convention record_
                # trade's own PROFIT/STOP/MANUAL_SELL zeroing already
                # uses) so this cycle's own view corrects itself
                # without waiting out another full slow-scan cycle.
                for item in self.cached_positions:
                    if (
                        item.get("instrument_type") == "EQUITY"
                        and str(item.get("symbol", "")).upper() == symbol
                    ):
                        item["quantity"] = "0"
                log.warning(
                    "PROTECT| %s | fast held-exit evaluation found no "
                    "real position (already closed) | %s",
                    symbol,
                    exc,
                )
                return
            log.error(
                "PROTECT| %s | fast held-exit evaluation failed | %s",
                symbol,
                exc,
            )

    _dispatch_concurrently(candidates, _evaluate_one)
