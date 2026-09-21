import logging
from datetime import datetime
from decimal import Decimal

log = logging.getLogger("webull-bot")


def _both_directions_blocked(self, symbol: str) -> bool:
    """True when neither a CALL nor a PUT can be opened on this
    underlying because both sides are wash-sale blocked.

    Blocks are scoped to underlying+option_type (see the
    wash_sales.block call in option_entry_exit), so one blocked side
    still leaves a perfectly tradeable strategy - only losing BOTH
    makes a symbol useless to focus mode.
    """
    return bool(
        self.wash_sales.blocked_until(f"{symbol}:CALL")
        and self.wash_sales.blocked_until(f"{symbol}:PUT")
    )


def select_focus_symbol(self, moment: datetime) -> None:
    """By explicit request: "find one really good volatile stock to
    play with and go all in on that for the day... make sure you use
    all of the capital and know how to select the perfect stock."

    Stage two of the funnel refresh_daily_batch starts - it narrows
    this morning's researched shortlist to the ONE name the whole
    account will trade options on today.

    Runs at focus_lock_time (09:45 by default) rather than at the
    bell. The first hour genuinely does carry the best setups, but
    09:30-09:45 is also where opening-range fakeouts concentrate, and
    pre-market ranking frequently does not survive the open -
    committing the entire account at 09:30 on pre-market data alone
    is the exact trap this timing avoids. Every candidate is
    re-measured on live regular-session data here.

    Contract-level quality (premium floor, chain spread, moneyness,
    IV percentile) is deliberately NOT re-checked here. Those gates
    already exist and already run at entry time in _evaluate_option_
    entry - duplicating them would mean a second, divergent copy of
    the same rules plus extra option-chain API calls at exactly the
    busiest moment of the session.

    If nothing clears the gates, NO focus symbol is set and the bot
    simply does not trade options today. That is deliberate: forcing
    a pick out of a weak field is precisely the low-quality trade
    that a fixed daily target pushes traders into, and this account
    has already paid for enough of those.
    """
    if not self.config.focus_mode_enabled:
        return
    if moment < self.session_moment(moment, self.config.focus_lock_time):
        return
    if self.focus_symbol_date == moment.date():
        # Already locked today. The ONE sanctioned re-pick: the
        # current symbol has become untradeable in BOTH directions
        # (see focus_repick_when_blocked). Wash-sale blocks are left
        # fully intact by explicit request, so the only way to keep
        # trading is to move to another name rather than sit idle
        # with a dead symbol for the rest of the session.
        if not self.config.focus_repick_when_blocked:
            return
        if self.focus_symbol is None:
            return
        if not _both_directions_blocked(self, self.focus_symbol):
            return
        log.info(
            "FOCUS  | %s is wash-blocked on both CALL and PUT - "
            "re-picking a replacement for the rest of the session",
            self.focus_symbol,
        )
    # Deliberately NOT date-stamped until a symbol is actually
    # locked, for the same reason refresh_daily_batch defers its
    # stamp: RVOL comes from volume_delta, which needs consecutive
    # intraday scan samples, so a container that starts near
    # focus_lock_time would otherwise score an unwarmed field,
    # lock nothing, and mark the day done.
    scored: list[tuple[float, str, dict]] = []
    for symbol in self.daily_batch:
        price = self.strategy.prices.get(symbol)
        if price is None or price <= 0:
            continue
        # Never commit the whole account to a name that can't be
        # traded in either direction. The wash-sale block is a real
        # 31-day lockout and persists across restarts, so a symbol
        # stopped out on both sides last week is still dead today.
        if _both_directions_blocked(self, symbol):
            continue
        if not (self.config.focus_min_price <= price <= self.config.focus_max_price):
            continue
        metrics = self.strategy.metrics.get(symbol, {})
        if metrics.get("volume", 0) < self.config.popular_stock_min_volume:
            continue
        # Now that regular-session samples exist, RVOL is finally
        # measurable - it could not be gated at batch time (08:45),
        # since volume_delta needs consecutive intraday snapshots to
        # mean anything. The evidence for this particular bar is
        # unusually clean: below-average RVOL averaged -0.02R per
        # trade while above-average averaged +0.08R.
        volume_ema = self.strategy.volume_delta_ema.get(symbol)
        latest_delta = self.strategy.volume_delta_latest.get(symbol)
        if volume_ema is None or volume_ema <= 0 or latest_delta is None:
            continue
        rvol = Decimal(latest_delta) / Decimal(volume_ema)
        if rvol < self.config.focus_min_rvol:
            continue
        volatility = self.strategy.realized_volatility_percent(symbol)
        if volatility is None or volatility < self.config.option_min_volatility_percent:
            continue
        score = self.strategy.priority_score(symbol, self.agent_assessment(symbol))
        scored.append(
            (score, symbol, {"rvol": rvol, "volatility": volatility, "score": score})
        )
    if not scored:
        # Keep retrying while there is still a session left to trade;
        # stop once the option closeout window begins, since a symbol
        # locked then could only be flattened immediately.
        give_up = moment >= self.session_moment(
            moment, self.config.option_eod_close_time
        )
        if give_up:
            self.focus_symbol_date = moment.date()
        if self.focus_logged_empty_date != moment.date():
            self.focus_logged_empty_date = moment.date()
            log.info(
                "FOCUS  | no symbol in today's batch (%s) cleared the focus "
                "gates (rvol>=%s | volatility>=%s | price %s-%s | volume>=%s) "
                "- %s",
                ",".join(self.daily_batch) or "empty",
                self.config.focus_min_rvol,
                self.config.option_min_volatility_percent,
                self.config.focus_min_price,
                self.config.focus_max_price,
                self.config.popular_stock_min_volume,
                "not trading options today" if give_up else "will retry",
            )
        return
    scored.sort(key=lambda row: row[0], reverse=True)
    best_score, best_symbol, detail = scored[0]
    self.focus_symbol = best_symbol
    self.focus_symbol_date = moment.date()
    log.info(
        "FOCUS  | %s locked for the session | rvol=%.2fx volatility=%.2f%% "
        "score=%.1f | beat %s other candidate(s): %s",
        best_symbol,
        detail["rvol"],
        detail["volatility"] * 100,
        best_score,
        len(scored) - 1,
        ", ".join(
            f"{symbol}({row_score:.1f})" for row_score, symbol, _ in scored[1:]
        )
        or "none",
    )
