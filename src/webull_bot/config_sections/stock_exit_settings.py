from decimal import Decimal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class StockExitSettings(BaseSettings):
    """The general (non-volatility-scalp) stock path's profit-target/stop-
    loss sizing, price-sanity guards, entry blackout/priority windows,
    the daily-significant-profit target-widen/stop-tighten pair, and the
    entry-extension-from-high buffer."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    stock_min_net_profit_percent: Decimal = Field(
        default=Decimal("0.0015"),
        ge=0,
        le=1,
    )
    stock_estimated_round_trip_cost_percent: Decimal = Field(
        default=Decimal("0.002"),
        ge=0,
        le=Decimal("0.10"),
    )
    # The flat regulatory pass-through fee Webull charges on the sell leg
    # only (SEC fee + FINRA TAF, rounded up to whole cents) - never charged
    # on the buy. It scales slightly with trade size (larger notional can
    # round up to 3 cents instead of 2), but a flat estimate is close enough
    # to make sure every realized P&L figure - dashboard, daily loss
    # breaker, trade log - reflects a real cost instead of assuming a free
    # round trip. Also folded into every profit target (stock, option) so a
    # target isn't hit at a price that nets a loss once this fee comes out.
    sell_fee_dollars: Decimal = Field(default=Decimal("0.02"), ge=0)
    stock_stop_loss_min_percent: Decimal = Field(default=Decimal("0.009"), gt=0, le=1)
    stock_stop_loss_max_percent: Decimal = Field(default=Decimal("0.015"), gt=0, le=1)
    stock_stop_loss_range_multiplier: Decimal = Field(
        default=Decimal("0.35"),
        ge=0,
        le=Decimal("5"),
    )
    # Already at 1.8:1 - clears the researched "most professional
    # traders target at least 1:1.5-1:2 reward:risk" convention (a
    # 40% win rate at 3:1 beats a 70% win rate at 0.5:1; risk:reward
    # matters more than win rate alone for long-run expectancy).
    stock_target_stop_multiple: Decimal = Field(
        default=Decimal("1.8"),
        ge=Decimal("0.5"),
        le=Decimal("5"),
    )
    # By request: risk-based position sizing (the professional 1-2%
    # rule) - size an entry so hitting the stop costs no more than this
    # fraction of buying power, instead of however many shares a fixed
    # dollar budget happens to afford. Set deliberately above the
    # textbook 1-2% for THIS account's size: on a ~$200 account, 1%
    # is $2/trade - barely actionable once spread and fees are
    # accounted for. This is a documented, deliberate tradeoff for a
    # small account, not a hidden compromise on the underlying
    # principle. See TradingStrategy.risk_based_share_count.
    stock_risk_per_trade_fraction: Decimal = Field(
        default=Decimal("0.03"), gt=0, le=Decimal("0.25")
    )
    # Under this strategy's own stop discipline, a held position's live
    # price should never legitimately drift this far from its cost basis -
    # the adaptive stop (stock_stop_loss_max_percent, at most ~1.5%) would
    # have already closed it out long before a real move got anywhere near
    # 15%. If average_cost and the live quote diverge by more than this,
    # treat average_cost as untrustworthy (a bad broker read, not a real
    # price move) rather than deriving a target/stop from it - see
    # TradingStrategy.stock_decision. See quote_price_sanity_percent below
    # for the related but separately-calibrated bid/ask check.
    stock_price_sanity_percent: Decimal = Field(
        default=Decimal("0.15"),
        gt=0,
        le=1,
    )
    # A quote's bid/ask diverging this far from that SAME quote's own
    # last-trade price - not over time, one snapshot - means distrust it
    # (see WebullAPI._sane_bid_or_ask) rather than pricing an order off
    # it. Deliberately much tighter than stock_price_sanity_percent: this
    # account's own real quotes, including its thinnest PENNY-bucket
    # names, never showed a spread-side divergence anywhere near this
    # (worst observed ~5%), so 8% has real margin above genuine illiquid
    # spreads while still catching the live incident that motivated this -
    # FPE's ask sat 13-60% above its own last-trade price for hours,
    # repeatedly pricing an exit order that could never fill.
    quote_price_sanity_percent: Decimal = Field(
        default=Decimal("0.08"),
        gt=0,
        le=1,
    )
    # How long AutoTrader.entry_price_sanity_cooldown_ready backs off a
    # symbol after a price_sanity_ok rejection, before letting a fresh
    # entry attempt retry it. Live incident: one illiquid symbol's quote
    # sat just past PRICE_SANITY_TOLERANCE and got retried (and
    # re-rejected) on essentially every scan cycle for hours with no
    # backoff at all - not entering is always safe, so this only ever
    # delays a retry, it never forces one through the way the exit
    # side's stalled-order backstops do.
    price_sanity_cooldown_seconds: int = Field(default=30, ge=5, le=600)
    stock_entry_max_spread_percent: Decimal = Field(
        default=Decimal("0.50"),
        gt=0,
        le=Decimal("5"),
    )
    # By request, after live evidence: WNW (and, per the user's
    # account, WKHS) stopped out shortly after core hours ended,
    # consistent with a fresh entry opened with little runway left
    # before the session's liquidity/spread conditions get materially
    # worse - core_session_active was only ever a boolean (in/out of
    # the window), with no awareness of HOW MUCH of the window was
    # actually left when a brand-new position was opened. A fresh
    # entry this close to close has almost no time to reach its
    # profit target before conditions change, unlike one opened
    # earlier in the session. Fresh entries ONLY (general BUY/SHORT
    # and volatility-scalp) - averaging down on an existing position
    # (already committed to earlier, when there was more runway) and
    # every exit path are deliberately unaffected.
    stock_entry_blackout_minutes_before_close: int = Field(
        default=15, ge=0, le=120
    )
    # By request: "keep it separate, all the bp should be for option,
    # then at 9am cst whatever is remaining can be used for stocks."
    # Options and stocks draw from separate Webull buying-power pools
    # (see account_state), but a fresh stock entry still consumes
    # margin/equity that reduces the account's real option buying
    # power for the rest of the day - opening stock positions in the
    # first minutes of the session, before options have had a chance
    # to claim anything, works against giving options first priority.
    # Blocks FRESH stock entries only (general BUY/SHORT and
    # volatility-scalp, same scope as stock_entry_blackout_minutes_
    # before_close - averaging down and every exit path are
    # unaffected) for this many minutes after OPTION_MARKET_OPEN_TIME.
    # Default 30 = 9:30am ET option open + 30min = 10:00am ET = 9:00am
    # CT.
    stock_entry_options_priority_minutes: int = Field(
        default=30, ge=0, le=120
    )
    # By request: "do not allow more than 20% in stocks." A hard
    # portfolio-level ceiling on total stock (EQUITY) exposure as a
    # fraction of account value - see risk.stock_total_exposure's
    # stock_total_exposure_at_cap, which reuses the same fresh_entry_
    # blackout_active gate every fresh stock entry/averaging-down
    # check already goes through. Never affects exits.
    stock_max_total_exposure_fraction: Decimal = Field(
        default=Decimal("0.20"), gt=0, le=1
    )
    # By request: "start transitioning away from core hours strategy
    # around 30 minutes before end of core hours." Softer/earlier than
    # stock_entry_blackout_minutes_before_close above (a hard block on
    # every fresh entry, closer to the bell) - this window instead
    # makes entry QUALITY behave like extended hours (POPULAR-bucket-
    # only fresh entries, wider spread tolerance) without stopping
    # trading outright. Must stay >= the hard-blackout window above so
    # the softer transition always starts before (or exactly at) the
    # hard cutoff, never after it.
    late_core_session_transition_minutes: int = Field(
        default=30, ge=0, le=180
    )
    # By request: "we basically just want to be able to stay in a
    # significant profit until eod" -> clarified as "let winners run
    # further before taking profit" once the day is already
    # significantly ahead. Once today's realized pnl reaches this
    # fraction of account value, general-path (whole-share/fractional,
    # not the volatility-scalp cohort - see stock_decision's
    # profit_target_multiplier param) profit targets widen by
    # profit_target_widen_multiplier instead of taking the normal,
    # earlier target - the account is already ahead for the day, so a
    # working position gets more room to grow that lead instead of
    # being sold at the same target size it would use starting from
    # flat/behind.
    daily_significant_profit_fraction: Decimal = Field(
        default=Decimal("0.03"), gt=0, le=1
    )
    profit_target_widen_multiplier: Decimal = Field(
        default=Decimal("1.5"), ge=1, le=5
    )
    # By request: "when we have a certain profit we should also not
    # allow stops to be too low" - same daily_significant_profit_
    # fraction trigger as the target-widening above, but tightens the
    # general path's stop distance instead (< 1 = tighter) - see
    # AutoTrader.stop_tighten_multiplier/stock_decision's stop_
    # tighten_multiplier param. Combined with profit_target_widen_
    # multiplier above, once the account is already significantly
    # ahead for the day: smaller downside (tighter stop), bigger
    # upside (wider target) - an intentionally asymmetric risk:reward
    # once there's already a lead worth protecting.
    stop_tighten_multiplier: Decimal = Field(
        default=Decimal("0.7"), gt=0, le=1
    )
    stock_entry_max_extension_percent: Decimal = Field(
        default=Decimal("0.01"),
        ge=0,
        le=Decimal("0.20"),
    )
    # By request: "days high only matters when it is a straight jump
    # pattern, once it stabilizes it is fine." entry_extension_ok used
    # to unconditionally require price sit stock_entry_max_extension_
    # percent below today's high (1% by default) before allowing a
    # fresh entry - live evidence this was a top-3 gate rejection
    # reason on nearly half of scanned candidates every cycle, blocking
    # plenty of real momentum that simply sits close to the high
    # without still actively racing toward it. Now only enforced while
    # the stock is genuinely still jumping - recent_momentum (the same
    # RECENT_MOMENTUM_LOOKBACK_MINUTES-window signal used elsewhere)
    # over this fraction counts as "still a straight jump"; once that
    # cools below it, price sitting near the high is a stabilized
    # level, not chasing a spike, so the buffer is skipped entirely.
    # Same scale as RECENT_MOMENTUM_MAX_DECLINE_PERCENT (5%) - a
    # genuinely fast move, not routine chop.
    entry_extension_jump_momentum_percent: Decimal = Field(
        default=Decimal("0.03"), gt=0, le=1
    )
