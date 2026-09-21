import logging
import time
from decimal import ROUND_UP, Decimal

from webull_bot.strategy_logic.types import Decision
from webull_bot.trading.guards.price_sanity import (
    OPTION_PRICE_SANITY_TOLERANCE,
    option_entry_spread_ok,
)
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
    # Same reset reasoning, for the profit-lock high-water mark: a
    # stale peak from a closed position would arm the trail
    # immediately on the NEXT entry into this contract and exit it at
    # a floor derived from a gain this position never had.
    self.option_peak_price.pop(option_symbol, None)
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
    # By explicit request ("just do not trade contracts that are not
    # easy to liquidify") - live incident: ORCL got bought into, then
    # its own STOP-loss couldn't find a buyer at any sane price (40%+
    # real bid/ask spread) and just kept resubmitting and timing out
    # unfilled while the position sat exposed. A structural, entry-
    # side liquidity floor - not a fix for a stuck exit, a refusal to
    # ever create one. Checked here, before any of the direction/
    # quality gates below, so a genuinely illiquid contract can't
    # qualify through any entry path (trend, scalp, or straddle).
    if not option_entry_spread_ok(
        self.api.quote_bid(quote),
        self.api.quote_ask(quote),
        self.config.option_max_entry_spread_percent,
    ):
        self.option_gate_rejections[
            "contract bid/ask spread too wide to liquidate reliably"
        ] += 1
        return open_count, buying_power
    underlying = contract["underlying_symbol"]
    # By explicit request: "find one really good volatile stock to
    # play with and go all in on that for the day... make sure you
    # use all of the capital." Deliberately placed ABOVE the smoke-
    # test bypass below, and above every entry-QUALITY gate, because
    # this is a STRUCTURAL constraint (which symbol the account is
    # committed to today), not a judgment about whether this setup
    # looks good. Sizing was never the thing preventing an all-in
    # position - option_capital_fraction is already 1.0 - it was
    # max_open_positions letting buying power drain into whatever
    # the scanner surfaced first. Gating entry to the one focus
    # symbol is what actually leaves the whole account available to
    # it. Until a focus symbol is locked (pre-09:45, or a day where
    # nothing cleared the gates) no option entry is allowed at all.
    if self.config.focus_mode_enabled and underlying != self.focus_symbol:
        self.option_gate_rejections[
            "not today's focus symbol"
            if self.focus_symbol
            else "no focus symbol locked yet"
        ] += 1
        return open_count, buying_power
    # By request ("once you hit a certain profit slow down") - the
    # day's target is already banked, so stop ADDING risk. Exits, the
    # profit-lock trail and the EOD close all stay live below.
    if self.new_entries_blocked():
        self.option_gate_rejections[
            "daily profit target reached - new entries throttled"
        ] += 1
        return open_count, buying_power
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
        # By explicit request ("it should consistently take the
        # perfect volatile and volume stock and trade a popular
        # call and put as it rises and dips"): every option entry -
        # not just the dip/rip scalp path below - must clear the
        # SAME real volatility+volume bar (is_volatility_scalp_
        # eligible) a stock needs to qualify for the stock-side
        # volatility-scalp cohort in the first place. Live incident
        # this catches: KO (a low-volatility blue-chip dividend
        # stock - about as far from "volatile and volume" as this
        # candidate pool gets) got a CALL bought purely off the
        # trend-following EMA direction signal below, which never
        # checked volatility/volume at all - only the scalp_
        # direction (dip/rip) path had this floor. Applied here,
        # before either path, so a boring name can qualify for
        # NEITHER a trend entry nor a scalp entry, uniformly.
        # By explicit request ("do you think this was supposed to
        # happen, not one single trade happened"): live incident -
        # NVDA, the locked focus symbol, was rejected here every
        # cycle for 2.5+ minutes straight. Confirmed live it was
        # genuinely active (72M share volume, +2.37% on the day,
        # 0.0044% spread), just moving SMOOTHLY tick-to-tick rather
        # than choppily - is_volatility_scalp_eligible/realized_
        # volatility_percent measure tick-to-tick stdev, which was
        # tuned to screen boring names (KO, SPY, NKE) OUT of a wide,
        # unvetted multi-thousand-symbol pool. Focus mode's own
        # selection pipeline already proves the underlying is
        # genuinely active by a DIFFERENT, no less real signal -
        # refresh_daily_batch's gap%/volume/spread gates plus the
        # established-symbols filter - and a heavily-traded,
        # efficiently-priced large-cap can clear all of that while
        # still reading "calm" on tick-to-tick stdev, exactly because
        # it IS liquid. Re-applying a gate built for a different
        # population onto an already-vetted one was blocking real,
        # tradeable setups outright. Skipped only in focus mode; the
        # non-focus path (disabled by default) keeps both checks.
        if not self.config.focus_mode_enabled:
            if not self.strategy.is_volatility_scalp_eligible(underlying):
                self.option_gate_rejections[
                    "underlying not volatile/high-volume enough"
                ] += 1
                return open_count, buying_power
            underlying_volatility = self.strategy.realized_volatility_percent(
                underlying
            )
            if (
                underlying_volatility is None
                or underlying_volatility < self.config.option_min_volatility_percent
            ):
                self.option_gate_rejections[
                    "underlying volatility below the option-specific floor"
                ] += 1
                return open_count, buying_power
        # By request (momentum-shift overview): general entry-quality
        # filter - is this candidate's move actually backed by real
        # volume? Same TradingStrategy.relative_volume_ok used on the
        # stock trend-entry side, checked against the underlying (the
        # option contract itself has no comparable volume-delta
        # tracking of its own).
        if not self.strategy.relative_volume_ok(underlying):
            self.option_gate_rejections[
                "underlying move not backed by above-average volume"
            ] += 1
            return open_count, buying_power
        # By request ("see the momentum by the buys and sells" / "make
        # sure the entry and exit happens at the right time according
        # to the momentum"): relative_volume_ok above only asks
        # whether volume is elevated - it is direction-blind. This
        # asks who is actually winning: a CALL needs buyers in
        # control, a PUT needs sellers. A dip nobody is buying is not
        # a dip worth buying a call into.
        if not self.strategy.pressure_supports_entry(underlying, contract_type):
            self.option_gate_rejections[
                "buy/sell pressure does not support this direction"
            ] += 1
            return open_count, buying_power
        # By explicit request ("there should not be too much quality
        # gate on the contract other than volume and volatility after
        # the momentum has been identified"): entry_extension_ok is a
        # chase/timing filter tuned for a broad multi-symbol
        # candidate pool. Focus mode already pre-vets a single
        # established underlying (large-cap, liquid, real volume -
        # see refresh_daily_batch), so once the direction/pressure
        # signals below confirm real momentum, an extra "is it too
        # close to today's high/low" veto stacks caution on top of
        # caution rather than screening out a genuinely bad pick.
        # Skipped only in focus mode; the non-focus path (disabled by
        # default) keeps it.
        underlying_price = self.strategy.prices.get(underlying)
        if (
            not self.config.focus_mode_enabled
            and underlying_price is not None
            and not self.strategy.entry_extension_ok(
                underlying,
                underlying_price,
                direction="SHORT" if contract_type == "PUT" else "BUY",
            )
        ):
            self.option_gate_rejections[
                "underlying still jumping toward today's high/low"
            ] += 1
            return open_count, buying_power
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
        # Same focus-mode carve-out as the entry-quality gate above -
        # is_volatility_scalp_eligible was blocking the dip/rip
        # momentum-flip signal on an already-vetted focus symbol for
        # the same "smooth tick-to-tick, still genuinely active"
        # reason.
        if self.config.option_scalp_enabled and (
            self.config.focus_mode_enabled
            or self.strategy.is_volatility_scalp_eligible(underlying)
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
        # By explicit request ("how to immediately sell call and buy
        # a put at the tip of momentum and vice versa"): the momentum
        # vision this whole options strategy was built around -
        # continuously trading a volatile underlying's swings, not
        # just one entry and done. _evaluate_option_exit stamps
        # option_momentum_flip[underlying] the moment it sells a CALL
        # into resistance or a PUT into support (a real momentum-
        # exhaustion signal, not the flat DTE/target exit) - that
        # exit itself IS the confirmation the OPPOSITE side just
        # became attractive, so the flip entry doesn't have to wait
        # for a fresh, separate dip/rip signal to build up again.
        # Bounded to option_momentum_flip_window_seconds so a stale
        # flip from long ago can't linger and fire on unrelated
        # later movement.
        flip = self.option_momentum_flip.get(underlying)
        if flip is not None:
            flip_type, flipped_at = flip
            if time.monotonic() - flipped_at <= float(
                self.config.option_momentum_flip_window_seconds
            ):
                scalp_direction = flip_type
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
        # By explicit request ("there should not be too much quality
        # gate on the contract other than volume and volatility after
        # the momentum has been identified"): IV percentile and the
        # broad VIXY market-regime gate are pricing-quality/market-wide
        # heuristics, not the underlying's own volume/volatility/
        # momentum - the three things focus mode already established
        # by selection (large-cap/liquid) and the gates above
        # (is_volatility_scalp_eligible, realized_volatility_percent,
        # relative_volume_ok, pressure_supports_entry, direction
        # match). Skipped only in focus mode; the non-focus path
        # (disabled by default) keeps both.
        if not self.config.focus_mode_enabled:
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
    # Live incident (SPY at $0.30, QQQ at $0.45 - both bought BELOW
    # the floor the same day it shipped): option_min_premium_dollars
    # was only enforced in select_atm_options, at contract-SELECTION
    # time. That isn't authoritative for two reasons - selection has a
    # bypass path (a single candidate, or no max_contract_cost, skips
    # the quoted-premium check entirely), and the premium it checks is
    # a discovery-time quote that can drift well below the floor by
    # the time this entry actually prices. limit_price here IS the
    # price the contract gets bought at, so the floor has to be
    # enforced against it, not against an earlier snapshot.
    if limit_price < self.config.option_min_premium_dollars:
        self.option_gate_rejections[
            "premium below the minimum (too cheap to survive its own noise)"
        ] += 1
        return open_count, buying_power
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
    #
    # Live incident (AMC): a candidate still qualifying every
    # scan cycle with a durably-too-wide spread re-triggered
    # price_sanity_ok's ERROR log every cycle with zero backoff
    # - 20+ lines in under 5 minutes for one contract, the same
    # class of bug already fixed for the option repricers.
    # price_sanity_cooldown_ready gates the call itself here (not
    # just the eventual order placement) so a stale rejection
    # doesn't get re-logged before its cooldown window clears.
    price_sane = self.price_sanity_cooldown_ready(
        option_symbol
    ) and self.price_sanity_ok(
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
            limit_price,
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
    # By explicit request ("why does it keep trying to sell RIVN for
    # 0.55 when the spread is actually way lower"): quote_price (the
    # `price` this function was called with) prioritizes the LAST
    # TRADE print, which on a thin/illiquid contract can be stale -
    # reflecting a trade from well before the underlying moved.
    # option_decision's PROFIT/LOSS thresholds are fixed (derived
    # from average_cost alone), but comparing them against a stale
    # last-trade price meant the bot kept believing a target was hit
    # when the REAL current bid (what a sale can actually realize)
    # was nowhere close - submit, never fill, cancel, recompute off
    # the same stale print, resubmit at the same unreachable price,
    # repeat. Uses the real current bid for this comparison instead
    # (falling back to the passed-in price if the bid is invalid/
    # unavailable) - target/stop thresholds themselves are unchanged,
    # only what gets compared against them. Averaging-down's own dip-
    # signal/sizing below intentionally keeps using the original
    # `price`, not this - that's a separate, already-tuned mechanism
    # and out of scope for this fix.
    sell_realizable_price = self.api.quote_bid(quote) or price
    opened_at = self.position_opened_at.get(key)
    seconds_since_entry = (
        time.monotonic() - opened_at if opened_at is not None else None
    )
    # By request ("make sure when there is a profit to not let on too
    # much loss") - the high-water mark the profit-lock trail rides.
    # Tracked off sell_realizable_price (the bid, what the position
    # could actually be sold for) rather than the mark, so the peak
    # reflects a gain that was genuinely realizable rather than one
    # that only ever existed on the mid.
    peak_price = max(
        self.option_peak_price.get(option_symbol, sell_realizable_price),
        sell_realizable_price,
    )
    self.option_peak_price[option_symbol] = peak_price
    decision = self.strategy.option_decision(
        sell_realizable_price,
        quantity,
        cost,
        days_to_expiration,
        seconds_since_entry=seconds_since_entry,
        peak_price=peak_price,
        # Once the day's target is banked, hold open winners on a
        # shorter leash - see focus_daily_profit_target_fraction.
        giveback_fraction=(
            self.config.profit_lock_giveback_fraction_after_throttle
            if self.profit_throttle_armed
            else self.config.profit_lock_giveback_fraction
        ),
    )
    # By request (momentum-shift overview): a bearish price/RSI
    # divergence on the UNDERLYING (not the option premium itself,
    # which is much noisier/leveraged) is an earlier warning than
    # waiting for option_decision's fixed profit target. Checked
    # against the underlying because RSI on a thinly-traded option's
    # own tick history would be measuring noise, not a real momentum
    # shift. Only upgrades a HOLD, and only when this position is
    # already profitable AFTER the flat sell fee AND past the next
    # valid option tick (not just nominally sell_realizable_price >
    # cost) - by explicit request ("still make sure to try and make
    # profit, not sell a loss for a profit"): live incident (AMD)
    # caught exactly this gap - the raw bid sat a hair above cost
    # (passing a naive ">"), the divergence check fired PROFIT, but
    # the actual SELL order price gets tick-quantized DOWN to the
    # SAME $0.05 tick as the entry, landing at cost - after the flat
    # $0.02 fee, a real loss labeled PROFIT. Requiring the price to
    # clear cost by at least fee_per_share (the exact margin option_
    # decision's own real profit target already builds in) guarantees
    # this only fires on a genuinely realizable gain.
    if decision.action == "HOLD" and sell_realizable_price > cost:
        fee_per_share = self.config.sell_fee_dollars / (quantity * 100)
        momentum_exit = False
        # Live incident (NKE, recurring even after the AMD fix): the
        # AMD fix required clearing cost by more than fee_per_share
        # (~$0.0002/share for a typical contract) - nowhere near
        # enough. decision.target_price gets quantized to the cent
        # (.quantize(Decimal("0.01")) in the PROFIT branch below)
        # before ever becoming the actual sell limit - a razor-thin
        # sub-cent edge (which clears fee_per_share trivially) rounds
        # right back down to the SAME price as cost, reproducing
        # "PROFIT" at the exact entry price (NKE sold at 0.18/0.15/
        # 0.12 - identical to its own entry price, three times, each
        # a real -$0.02 loss). Requiring the margin to clear a full
        # $0.05 option tick - not just the fee - guarantees the
        # quantized price is genuinely, meaningfully above cost.
        min_margin = max(fee_per_share, Decimal("0.05"))
        if sell_realizable_price - cost > min_margin:
            underlying = contract["underlying_symbol"]
            underlying_price = self.strategy.prices.get(underlying)
            if underlying_price is not None and self.strategy.rsi_divergence(
                underlying, underlying_price, time.monotonic()
            ) == "BEARISH":
                decision = Decision(
                    "PROFIT",
                    "bearish RSI divergence on the underlying - locking in the gain",
                    sell_realizable_price,
                )
                momentum_exit = True
            # By explicit request ("as a human I can see and make
            # profit off of the swings... seeing when there is
            # resistance so just sell off the profit"): a CALL
            # behaves like a BUY (resistance at today's high), a PUT
            # like a SHORT (resistance at today's low) - same
            # direction-mapping convention as entry_extension_ok's
            # own use of this same underlying price data.
            if decision.action == "HOLD" and underlying_price is not None:
                option_type = contract.get("option_type")
                if self.strategy.approaching_resistance(
                    underlying,
                    underlying_price,
                    "SHORT" if option_type == "PUT" else "BUY",
                ):
                    decision = Decision(
                        "PROFIT",
                        "underlying approaching resistance - locking in the gain",
                        sell_realizable_price,
                    )
                    momentum_exit = True
            # By request ("see the momentum by the buys and sells"):
            # participation has turned against this position hard
            # enough to read as exhaustion - the "tip of momentum"
            # this whole flip mechanic is built around. Sits inside
            # the same min_margin guard as the two checks above, so
            # like them it can only ever convert a HOLD into a
            # genuinely realizable PROFIT, never cut a loser (STOP
            # owns loss-cutting on its own separate terms).
            if decision.action == "HOLD" and self.strategy.pressure_flipped_against(
                underlying, contract.get("option_type")
            ):
                decision = Decision(
                    "PROFIT",
                    "buy/sell pressure flipped against the position - "
                    "locking in the gain",
                    sell_realizable_price,
                )
                momentum_exit = True
            # By explicit request ("how to immediately sell call and
            # buy a put at the tip of momentum and vice versa"): this
            # exit itself (divergence or resistance - a real momentum-
            # exhaustion read, not the flat DTE/target exit) IS the
            # confirmation the OPPOSITE side just became attractive.
            # Stamped here, read by _evaluate_option_entry's
            # scalp_direction check, so the flip entry doesn't have
            # to wait for a fresh, separate dip/rip signal to build.
            if momentum_exit:
                option_type = contract.get("option_type")
                self.option_momentum_flip[underlying] = (
                    "PUT" if option_type == "CALL" else "CALL",
                    time.monotonic(),
                )
    # By request: "you can also use averaging down... for
    # options as well" - only when the position is neither
    # profiting nor already at its stop (decision == HOLD),
    # still has genuine room before forced time-decay exit,
    # and hasn't hit its averaging cap/cooldown/strictly-
    # lower-price bar. Sized with the same option_order_
    # quantity risk-cap fresh entries use, against whatever
    # buying power remains this cycle.
    # By explicit request ("just do not trade contracts that are not
    # easy to liquidify") - the same entry-side spread floor applies
    # to averaging down: buying MORE of an already-illiquid contract
    # only deepens the exposure a stuck exit can't get out of, even
    # though the position itself is already held.
    average_down_spread_ok = option_entry_spread_ok(
        self.api.quote_bid(quote),
        self.api.quote_ask(quote),
        self.config.option_max_entry_spread_percent,
    )
    if (
        decision.action == "HOLD"
        and quantity > 0
        and option_symbol not in self.pending_option_exits
        and days_to_expiration > self.config.option_min_hold_dte
        # By explicit request, averaging down is deliberately NOT
        # gated on the daily profit throttle: the throttle stops
        # OPENING new risk, while averaging down is managing a
        # position that is already open and already exposed. Blocking
        # it would strand an underwater position with no way to
        # improve its basis while the throttle is armed.
        and average_down_spread_ok
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
            # NOTE: option_min_premium_dollars is deliberately NOT
            # applied here, unlike the fresh-entry path.
            #
            # By explicit request ("it isnt even averaging down"): the
            # floor briefly gated this too, which silently disabled
            # averaging down entirely - a position only becomes a
            # candidate for it AFTER its premium has fallen, so a
            # $0.51 entry sitting at $0.475 was rejected for being
            # "too cheap" despite the floor's whole purpose being to
            # vet the ORIGINAL entry, which it had already passed.
            # The floor exists to stop NEW money going into
            # lottery-ticket contracts, not to stop a validated thesis
            # from being averaged into. The averaging ladder's own
            # caps (option_max_averaging_buys, the widening dip
            # threshold, the cooldown, and the strictly-lower-price
            # bar) are what bound this path's risk.
            if (
                average_down_price is not None
                and self.price_sanity_cooldown_ready(option_symbol)
                and self.price_sanity_ok(
                    option_symbol,
                    price,
                    average_down_price,
                    tolerance=OPTION_PRICE_SANITY_TOLERANCE,
                )
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
                    average_down_price,
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
        # Live incident (NKE, recurring even after the AMD fee-margin
        # fix): quantizing to the CENT here, not the real $0.05 option
        # tick, silently erased genuine profit targets - a 2% target
        # on a $0.18 cost (0.1836) rounds to 0.18 at 2 decimal places,
        # the SAME price as cost, so "PROFIT" filled at cost exactly
        # (0.18 -> 0.18, three times, each a real -$0.02 loss). ROUND_UP
        # to the real tick guarantees the placed price is never
        # quantized back down below the intended target.
        target = self.api._quantize_to_option_tick(
            decision.target_price, ROUND_UP
        )
        limit_price = max(
            target,
            self.api.option_limit_price(quote, "SELL"),
        )
        if not self.price_sanity_cooldown_ready(
            option_symbol
        ) or not self.price_sanity_ok(
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
        if not self.price_sanity_cooldown_ready(
            option_symbol
        ) or not self.price_sanity_ok(
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
