import logging
import time
from decimal import Decimal

from webull_bot.trading.guards.price_sanity import OPTION_PRICE_SANITY_TOLERANCE
from webull_bot.webull_api import QuoteUnavailableError

log = logging.getLogger("webull-bot")


def _evaluate_option_entry(
    self,
    contract: dict,
    option_symbol: str,
    key: str,
    quote: dict,
    price,
    days_to_expiration: int,
    current_iv,
    directions: dict[str, str],
    guard_active: bool,
    current_vixy,
    open_count: int,
    buying_power: Decimal,
    positions: list[dict],
) -> tuple[int, Decimal]:
    """Fresh option-entry evaluation for one contract with a currently
    flat (quantity == 0) position - called from trade_options' per-
    contract loop only in that case. Returns the (possibly updated)
    open_count/buying_power - trade_options always moves on to the next
    contract immediately after calling this (every path through the
    original inline code ended in `continue`), so there's no exit-
    management fallthrough to worry about here.
    """
    self.pending_option_exits.discard(option_symbol)
    # Reset averaging-down state the moment the position
    # fully closes - same convention as the stock-side
    # volatility_scalp_average_down_count.pop(...) reset.
    had_averaging_state = option_symbol in self.option_average_down_count
    self.option_average_down_count.pop(option_symbol, None)
    self.last_option_average_down.pop(option_symbol, None)
    self.option_last_buy_price.pop(option_symbol, None)
    if had_averaging_state:
        # By request ("do a full on options sanity check") - persist
        # the reset too, or a stale count/last-buy-price for an
        # already-closed position would come back on the next
        # restart and wrongly narrow that (now-flat, unrelated)
        # symbol's next fresh entry's averaging-down ladder. Guarded
        # on had_averaging_state so this doesn't write to disk every
        # single cycle for every already-flat contract in the batch -
        # only when there was actually something to clear.
        self.option_contracts_state.save(
            self.option_contracts,
            self.option_discovery_attempted,
            {
                symbol: {
                    "count": count,
                    "last_buy_price": self.option_last_buy_price[symbol],
                }
                for symbol, count in self.option_average_down_count.items()
                if count > 0 and symbol in self.option_last_buy_price
            },
        )
    # By request ("scan through everything... figure out
    # what you missed"): live evidence showed real CALL/
    # PUT signals firing constantly all day (169/186
    # cycles had at least one), yet zero option orders
    # ever placed - something downstream of the signal
    # was silently blocking every single one, with NO
    # diagnostic visibility on any of these gates
    # (unlike the stock side's GATES summary). Every
    # rejection point below now counts into option_
    # gate_rejections (a dedicated dict, same pattern as
    # avgdown_gate_rejections, so it doesn't get
    # crowded out of the shared gate_rejections summary
    # by the far more numerous stock-side reasons) -
    # logged periodically to actually see which gate is
    # the real blocker instead of guessing.
    if days_to_expiration <= self.config.option_min_hold_dte:
        self.option_gate_rejections[
            "too close to expiration"
        ] += 1
        return open_count, buying_power
    underlying = contract["underlying_symbol"]
    direction = directions.get(underlying, "HOLD")
    contract_type = contract.get("option_type")
    # By explicit request, for a one-off diagnostic
    # ("make sure it fires... no barrier, quickly sell
    # it, and then change the option strategy again"):
    # option_smoke_test_mode skips every entry-QUALITY
    # gate below (direction signal, delta, IV
    # percentile, market regime, wash-sale, stop-loss
    # guard, quarantine) - structural checks (DTE,
    # affordability/sizing, cooldown, rate cap, max
    # open positions) still apply below, so this can't
    # spam unlimited orders. Off by default; meant to
    # be turned back off (and OPTION_TAKE_PROFIT_
    # PERCENT restored) once a real end-to-end trade is
    # confirmed.
    if not self.config.option_smoke_test_mode:
        # By request: "it doesn't buy puts while there is a
        # dip, or a call on a dip entry and quickly sell it.
        # This should happen for quick profit." The EMA
        # direction signal is trend-following (needs a fresh
        # cross to have already happened); it has no mean-
        # reversion path at all - a CALL on the underlying
        # DIPPING (betting on the same fast bounce the stock-
        # side volatility-scalp cohort already trades) or a
        # PUT on it RIPPING (the mirror-image bet on a fast
        # pullback) never had a way to fire. Reuses the exact
        # stock-side dip/rip signals and eligibility bar
        # (is_volatility_scalp_eligible - already-confirmed-
        # choppy-enough), so this only fires on names that
        # already clear the same bar the stock cohort does,
        # not every quiet name in the candidate pool.
        scalp_direction = "HOLD"
        if self.config.option_scalp_enabled and self.strategy.is_volatility_scalp_eligible(
            underlying
        ):
            underlying_price = self.strategy.prices.get(underlying)
            if underlying_price is not None:
                if self.strategy.volatility_scalp_dip_signal(
                    underlying, underlying_price
                ):
                    scalp_direction = "CALL"
                elif self.strategy.volatility_scalp_rip_signal(
                    underlying, underlying_price
                ):
                    scalp_direction = "PUT"
        # By request: "you can... use call and put
        # simultaneously type strategies for options as
        # well" - option_straddle_enabled (opt-in, off
        # by default) drops the requirement that this
        # contract's type match the underlying's single
        # directional EMA signal, so a CALL and a PUT on
        # the SAME underlying can both qualify for entry
        # at once (a straddle bet on movement itself,
        # not a specific direction). Every OTHER gate
        # below (delta, IV percentile, market regime,
        # wash-sale, stop-loss guard, quarantine,
        # cooldown, rate cap, affordability) still
        # applies unchanged - this only removes the
        # single-direction restriction, it doesn't
        # bypass quality checks the way smoke-test mode
        # does.
        if not (
            self.config.option_straddle_enabled
            or (contract_type == "CALL" and direction == "CALL")
            or (contract_type == "PUT" and direction == "PUT")
            or (contract_type == "CALL" and scalp_direction == "CALL")
            or (contract_type == "PUT" and scalp_direction == "PUT")
        ):
            self.option_gate_rejections[
                "no direction signal for this underlying"
            ] += 1
            return open_count, buying_power
        # Live incident (this bug, found right after the
        # option-batch-priority fix started actually
        # letting real signals reach this loop): tick/
        # order-flow confirmation was the dominant remaining
        # blocker (1-5 rejections every cycle) - it re-
        # checked the SAME "OPTU:" price series option_
        # direction_signal's EMA cross just fired on, but
        # only advances one sample per ~2-minute cycle, so
        # its 10-sample window spans ~20 minutes and could
        # easily disagree with a signal that just flipped
        # THIS cycle. By explicit request: removed for
        # options - trust the EMA direction signal on its
        # own, same as the stock side already effectively
        # does once tick_direction_ok's own separate check
        # passes.
        if not self.strategy.option_delta_ok(
            self.api.option_delta(quote)
        ):
            self.option_gate_rejections["delta out of range"] += 1
            return open_count, buying_power
        if not self.strategy.option_iv_percentile_ok(
            self.option_iv_history[option_symbol], current_iv
        ):
            self.option_gate_rejections["IV percentile failed"] += 1
            return open_count, buying_power
        if not self.strategy.option_market_regime_ok(
            self.vixy_history, current_vixy
        ):
            self.option_gate_rejections[
                "market regime (VIXY) gate active"
            ] += 1
            return open_count, buying_power
        # By request: "you can constantly buy puts and
        # calls on the same stock as it dips and rises" -
        # scoped to underlying+direction (see the
        # matching wash_sales.block call above), so a
        # stopped-out CALL only blocks a repurchased
        # CALL, not a PUT on the same underlying.
        wash_key = f"{underlying}:{contract_type}"
        blocked_until = self.wash_sales.blocked_until(wash_key)
        if blocked_until:
            self.option_gate_rejections["wash-sale blocked"] += 1
            if wash_key not in self.wash_skip_logged:
                self.wash_skip_logged.add(wash_key)
                log.info(
                    "WASH   | %-8s | option entry blocked until %s",
                    wash_key,
                    blocked_until.strftime("%Y-%m-%d"),
                )
            return open_count, buying_power
        self.wash_skip_logged.discard(wash_key)
        if guard_active:
            self.gate_rejections[
                "stop-loss guard active - too many recent stops"
            ] += 1
            self.option_gate_rejections[
                "stop-loss guard active"
            ] += 1
            return open_count, buying_power
        if self.symbol_quarantined(key):
            self.gate_rejections[
                "symbol quarantined - recent net losses on "
                "this symbol"
            ] += 1
            self.option_gate_rejections["symbol quarantined"] += 1
            return open_count, buying_power
    limit_price = self.api.option_limit_price(quote, "BUY")
    buy_quantity, contract_cost = (
        self.strategy.option_order_quantity(
            limit_price,
            self.cached_option_buying_power,
        )
    )
    # By request: "make sure your buy and sell price
    # will actually be executed inside the spread for
    # options, similar to stocks" - stocks already run
    # every order through this fat-finger backstop;
    # options never did. Computed once (not inline in
    # both the diagnostic elif chain below and the real
    # gate) since price_sanity_ok has a side effect
    # (logs + records the rejection timestamp) that
    # would otherwise double-fire.
    price_sane = self.price_sanity_ok(
        option_symbol,
        price,
        limit_price,
        tolerance=OPTION_PRICE_SANITY_TOLERANCE,
    )
    if buy_quantity <= 0:
        self.option_gate_rejections[
            "sizing produced zero contracts (price/buying "
            "power/risk cap)"
        ] += 1
    elif open_count >= self.config.max_open_positions:
        self.option_gate_rejections["max open positions"] += 1
    elif not self.cooldown_ready(key):
        self.option_gate_rejections[
            "order-submission cooldown"
        ] += 1
    elif self.rate_capped(key):
        self.option_gate_rejections["hourly rate cap"] += 1
    elif not self.reentry_cooldown_ready(key):
        self.option_gate_rejections["reentry cooldown"] += 1
    elif not price_sane:
        self.option_gate_rejections["price sanity check failed"] += 1
    if (
        open_count < self.config.max_open_positions
        and buy_quantity > 0
        and self.cooldown_ready(key)
        and not self.rate_capped(key)
        and self.reentry_cooldown_ready(key)
        and price_sane
    ):
        order_id = self.api.place_option(
            contract,
            "BUY",
            buy_quantity,
            limit_price,
            "BUY_TO_OPEN",
        )
        self.record_trade(
            key,
            order_id,
            "BUY",
            entry_price=limit_price,
            quantity=buy_quantity,
        )
        buying_power = max(
            Decimal("0"),
            buying_power - contract_cost * buy_quantity,
        )
        positions.append(
            {
                "instrument_type": "OPTION",
                "symbol": option_symbol,
                "quantity": str(buy_quantity),
            }
        )
        open_count += 1
    return open_count, buying_power


def _evaluate_option_exit(
    self,
    contract: dict,
    option_symbol: str,
    key: str,
    quote: dict,
    price,
    quantity,
    cost,
    days_to_expiration: int,
    buying_power: Decimal,
) -> Decimal:
    """Held-option management (averaging down, PROFIT, LOSS) for one
    contract with a currently nonzero (quantity > 0) position - called
    from trade_options' per-contract loop only in that case. Returns
    the (possibly updated) buying_power; every path through the
    original inline code either fell through to the end of the loop
    iteration or `continue`d with nothing left to do, so an early
    return here is equivalent - trade_options moves on to the next
    contract immediately after calling this either way.
    """
    decision = self.strategy.option_decision(
        price,
        quantity,
        cost,
        days_to_expiration,
    )
    # By request: "you can also use averaging down... for
    # options as well" - only when the position is neither
    # profiting nor already at its stop (decision == HOLD),
    # still has genuine room before forced time-decay exit,
    # and hasn't hit its averaging cap/cooldown/strictly-
    # lower-price bar. Sized with the same option_order_
    # quantity risk-cap fresh entries use, against whatever
    # buying power remains this cycle.
    if (
        decision.action == "HOLD"
        and quantity > 0
        and option_symbol not in self.pending_option_exits
        and days_to_expiration > self.config.option_min_hold_dte
        and self.option_average_down_count[option_symbol]
        < self.config.option_max_averaging_buys
        and (
            time.monotonic()
            - self.last_option_average_down.get(option_symbol, 0.0)
        )
        >= float(self.config.option_averaging_reentry_cooldown_seconds)
        and self.strategy.option_average_down_signal(
            price,
            cost,
            level=self.option_average_down_count[option_symbol],
        )
        and (
            option_symbol not in self.option_last_buy_price
            or price < self.option_last_buy_price[option_symbol]
        )
    ):
        average_down_quantity, average_down_contract_cost = (
            self.strategy.option_order_quantity(price, buying_power)
        )
        if average_down_quantity <= 0:
            # By explicit request ("nah there should be no constraint
            # like that") - option_order_quantity's risk-per-trade
            # cap (option_capital_fraction, 5% of buying power) was
            # floor-rounding to 0 contracts whenever the remaining
            # OPTION buying power was thin relative to this specific
            # contract's cost, silently blocking every averaging-down
            # attempt that had otherwise cleared every real gate (dip
            # signal, cooldown, cap, DTE) - live incident: UBER sat
            # unaveraged for many cycles this way. Averaging down is
            # a deliberate decision the signal/cap/cooldown gates
            # above already vetted; once made, it should still buy at
            # least ONE contract if genuinely affordable, rather than
            # let the risk-fraction math alone silently veto it down
            # to zero. The only real constraint left here is
            # affordability itself, not the risk fraction.
            average_down_contract_cost = price * 100
            if buying_power >= average_down_contract_cost:
                average_down_quantity = 1
            else:
                self.option_gate_rejections[
                    "averaging down - cannot afford even 1 contract"
                ] += 1
        if average_down_quantity > 0:
            try:
                average_down_price = self.api.option_limit_price(
                    quote, "BUY"
                )
            except QuoteUnavailableError:
                average_down_price = None
            if average_down_price is not None and self.price_sanity_ok(
                option_symbol,
                price,
                average_down_price,
                tolerance=OPTION_PRICE_SANITY_TOLERANCE,
            ):
                order_id = self.api.place_option(
                    contract,
                    "BUY",
                    average_down_quantity,
                    average_down_price,
                    "BUY_TO_OPEN",
                )
                self.record_trade(
                    key,
                    order_id,
                    "BUY",
                    entry_price=average_down_price,
                    quantity=average_down_quantity,
                    counts_toward_idle_cash_ramp=False,
                )
                self.option_average_down_count[option_symbol] += 1
                self.last_option_average_down[option_symbol] = (
                    time.monotonic()
                )
                self.option_last_buy_price[option_symbol] = price
                # By request ("do a full on options sanity check") -
                # persist the ladder state right when it changes, not
                # just on the next fresh discovery - see
                # OptionContractsStateStore's docstring for why this
                # must survive a restart.
                self.option_contracts_state.save(
                    self.option_contracts,
                    self.option_discovery_attempted,
                    {
                        symbol: {
                            "count": count,
                            "last_buy_price": self.option_last_buy_price[symbol],
                        }
                        for symbol, count in self.option_average_down_count.items()
                        if count > 0 and symbol in self.option_last_buy_price
                    },
                )
                buying_power = max(
                    Decimal("0"),
                    buying_power
                    - average_down_contract_cost * average_down_quantity,
                )
                log.info(
                    "OPTIONS| %-8s | average down | qty=%s | price=%s "
                    "| count=%s",
                    option_symbol,
                    average_down_quantity,
                    average_down_price,
                    self.option_average_down_count[option_symbol],
                )
    if (
        decision.action == "PROFIT"
        and option_symbol not in self.pending_option_exits
        and self.cooldown_ready(key)
    ):
        if decision.target_price is None:
            return buying_power
        target = decision.target_price.quantize(Decimal("0.01"))
        limit_price = max(
            target,
            self.api.option_limit_price(quote, "SELL"),
        )
        if not self.price_sanity_ok(
            option_symbol,
            price,
            limit_price,
            tolerance=OPTION_PRICE_SANITY_TOLERANCE,
        ):
            return buying_power
        order_id = self.api.place_option(
            contract,
            "SELL",
            quantity,
            limit_price,
            "SELL_TO_CLOSE",
        )
        self.pending_option_exits.add(option_symbol)
        pnl = self.record_realized_exit(cost, limit_price, quantity, multiplier=100)
        self.record_trade(
            key, order_id, "PROFIT", limit_price, pnl=pnl,
            entry_price=cost, quantity=quantity,
        )
    if (
        decision.action == "LOSS"
        and option_symbol not in self.pending_option_exits
        and self.cooldown_ready(key)
    ):
        limit_price = self.api.option_limit_price(quote, "SELL")
        if not self.price_sanity_ok(
            option_symbol,
            price,
            limit_price,
            tolerance=OPTION_PRICE_SANITY_TOLERANCE,
        ):
            return buying_power
        order_id = self.api.place_option(
            contract,
            "SELL",
            quantity,
            limit_price,
            "SELL_TO_CLOSE",
        )
        # By request: "you can constantly buy puts and calls
        # on the same stock as it dips and rises" - blocking
        # the whole underlying after ANY option stop-loss
        # (as this used to) would lock out a PUT re-entry
        # for WASH_SALE_BLOCK_DAYS just because a CALL on
        # the same name stopped out, defeating exactly the
        # swing-with-the-price behavior requested. Scoping
        # the block to underlying+direction still blocks
        # repurchasing the SAME losing side too soon (the
        # actual wash-sale concern) while leaving the
        # opposite side free.
        self.wash_sales.block(
            f"{contract['underlying_symbol']}:{contract['option_type']}",
            "option stop-loss exit submitted",
        )
        self.pending_option_exits.add(option_symbol)
        pnl = self.record_realized_exit(cost, limit_price, quantity, multiplier=100)
        self.record_trade(
            key, order_id, "STOP", limit_price, pnl=pnl,
            entry_price=cost, quantity=quantity,
        )
    return buying_power
