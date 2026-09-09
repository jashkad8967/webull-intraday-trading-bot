from decimal import Decimal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class PositionSizingSettings(BaseSettings):
    """Capital allocation across stock buckets/options, per-position and
    per-entry sizing caps, fractional-share sizing, idle-cash gate
    relaxation, and the opening-grace/extended-hours spread easing."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Directional short-selling in the main EMA/SMA stock strategy - a
    # fresh bearish EMA cross opens a short instead of just being skipped.
    # Off by default: the account needs margin/short approval, and Webull
    # rejections surface naturally (see is_broker_position_conflict-style
    # handling in bot.py) rather than being pre-checked here. Shorts always
    # flatten same-day regardless of OVERNIGHT_HOLD_ENABLED - overnight
    # gap/squeeze risk on a short is asymmetric (unbounded loss) unlike a
    # long's overnight risk.
    short_selling_enabled: bool = True
    stock_popular_capital_fraction: Decimal = Field(
        default=Decimal("0.70"),
        ge=0,
        le=1,
    )
    stock_penny_capital_fraction: Decimal = Field(
        default=Decimal("0.10"),
        ge=0,
        le=1,
    )
    stock_discovery_capital_fraction: Decimal = Field(
        default=Decimal("0.20"),
        ge=0,
        le=1,
    )
    # By request: "out of 7500 stocks it should easily be able to find
    # enough stocks to invest everything" - live evidence: a single
    # FDX entry consumed ~43% of the whole account's buying power in
    # one trade ($186 -> $106.51), because entry_budget (see the
    # general BUY/SHORT entry gates in trade_stocks) was only ever
    # capped by bucket_remaining (up to stock_popular_capital_fraction,
    # 70% by default, of the WHOLE bucket's allocation - not per
    # position) and the live buying_power itself, with no per-position
    # diversification cap. With capital this concentrated into 1-2
    # trades, there's little left for the other several thousand
    # scanned candidates to ever get funded, even though plenty of them
    # individually clear every other gate. Caps any single fresh
    # entry's budget at this fraction of the CURRENT (already cycle-
    # shrinking) buying_power - naturally self-reducing as more
    # capital gets deployed within the same cycle, spreading what's
    # left across more symbols instead of one candidate absorbing most
    # of a bucket's whole allocation.
    stock_max_position_fraction_of_buying_power: Decimal = Field(
        default=Decimal("0.15"),
        gt=0,
        le=1,
    )
    fractional_shares_enabled: bool = True
    fractional_shares_min_notional: Decimal = Field(default=Decimal("25"), ge=Decimal("5"))
    option_batch_size: int = Field(default=20, ge=1, le=20)
    # By request ("it is very slow with scanning option" / "are you
    # sure there is no way to load options more quickly"): discover_
    # option_contracts only runs once per full run() loop cycle, and
    # that outer cycle's own pace (universe scan/quote batching/trade_
    # stocks overhead) is the real bottleneck, not this throttle - live
    # evidence: with this at (5, 1s), successful discovery bursts
    # correctly examined ~5 at a time, but the bursts themselves were
    # 30s-2min+ apart, averaging well under 2 new underlyings/minute.
    # Raising the throttle interval further wouldn't help (the outer
    # loop is already calling far less often than once/second) - what
    # helps is covering MORE of the (now much smaller, curated - see
    # discover_option_contracts) candidate pool on each of those
    # infrequent bursts. Each discovery attempt is a lightweight per-
    # symbol contract lookup (plus, since the buying-power-
    # affordability fix, one small quote batch only when a cost cap is
    # actually passed), not a big batch call, so a higher per-cycle
    # ceiling doesn't meaningfully change per-cycle API request shape.
    option_discovery_per_cycle: int = Field(default=20, ge=1, le=50)
    option_discovery_seconds: Decimal = Field(default=Decimal("1"), ge=1, le=3600)

    stock_quantity: int = Field(default=1, ge=1)
    # By request: "you can buy multiple contracts, it does not have to
    # be only 1" - this used to default to 1 and, since option_order_
    # quantity takes the MIN of this against affordability/notional/
    # risk-cap, it was always the binding constraint regardless of how
    # much buying power was actually available. Raised to a high
    # ceiling so those real, buying-power-aware caps do the actual
    # sizing instead of a flat hardcoded 1.
    option_quantity: int = Field(default=20, ge=1)
    max_open_positions: int = Field(default=50, ge=1)
    max_order_notional: Decimal = Field(default=Decimal("1000"), gt=0)

    # During core trading hours, size new stock entries as this fraction of
    # total account buying power (a genuinely fractional/decimal quantity),
    # instead of the fixed STOCK_QUANTITY whole-share sizing - see
    # dollar_stock_quantity() and README "Fractional shares". ge=0
    # deliberately (not gt=0): 0 is the escape hatch back to fixed-quantity
    # sizing all day, same pattern as OPENING_GRACE_MINUTES=0.
    #
    # This and stock_whole_share_core_session_fraction below are sized to
    # sum to 1.0 (not less) - together they're the entire per-cycle entry
    # budget, not an extra throttle beneath MIN_CASH_RESERVE_DOLLARS. A
    # smaller sum here would leave qualifying candidates unfunded (and cash
    # sitting idle) even when MIN_CASH_RESERVE_DOLLARS' floor has plenty of
    # room left above it.
    stock_core_session_position_fraction: Decimal = Field(
        default=Decimal("0.30"),
        ge=0,
        le=1,
    )
    # Webull only allows fractional trading as a MARKET order during core
    # hours (see "Fractional shares" above), so during core hours capital
    # splits between two independent per-cycle budgets instead of one
    # entry style taking every candidate: STOCK_CORE_SESSION_POSITION_FRACTION
    # above for fractional dollar-sized entries, and this fraction of
    # buying power for ordinary whole-share entries running alongside it.
    # Outside core hours this cap doesn't apply at all - fractional isn't
    # usable then anyway, so whole-share sizing spends against the full
    # remaining entry budget instead of this slice of it.
    stock_whole_share_core_session_fraction: Decimal = Field(
        default=Decimal("0.70"),
        ge=0,
        le=1,
    )
    # Hard floor on idle cash: buying_power is reduced by this amount
    # before any sizing math runs each cycle (see AutoTrader.run()), so
    # nothing downstream - stock/option/pairs entries, manual dashboard
    # buys - ever plans to spend into the last MIN_CASH_RESERVE_DOLLARS of
    # the account. This bounds what the bot is willing to risk spending;
    # see idle_cash_relaxation_enabled below for how the bot tries to
    # actually keep spending down to this floor, not just permit it.
    min_cash_reserve_dollars: Decimal = Field(default=Decimal("10"), ge=0)
    # Keeping cash deployed outranks entry quality, but not entirely -
    # entry gates only progressively loosen the longer cash sits above
    # MIN_CASH_RESERVE_DOLLARS with nothing bought (see
    # AutoTrader.idle_cash_ramp_progress), snapping back to full strictness
    # the moment any entry fires. This never touches the directional
    # EMA/SMA signal itself (still no trade without a real crossover) -
    # only the secondary confirmation gates: max spread, extension from
    # today's high/low, VWAP band, and the tick-direction veto threshold.
    idle_cash_relaxation_enabled: bool = True
    # No relaxation at all for this long after the last entry - a burst of
    # trading shouldn't immediately start loosening gates again just
    # because the very next cycle still has leftover cash. Short on
    # purpose: keeping cash deployed outranks entry quality (see below),
    # so idle capital should barely get a breather before gates start
    # loosening again.
    idle_cash_grace_seconds: int = Field(default=60, ge=0, le=21600)
    # After the grace period, gates linearly loosen toward their max
    # multiplier/relaxation over this many additional seconds, then hold
    # at the max for as long as cash keeps sitting idle.
    idle_cash_ramp_seconds: int = Field(default=600, ge=1, le=21600)
    # Shared ceiling for every multiplicative gate (max spread, extension
    # from today's high/low, VWAP band) at full ramp - one knob, not three,
    # since there's no real reason to relax these three at different rates
    # and a fake extra layer of granularity is worse than none.
    idle_cash_max_gate_multiplier: Decimal = Field(
        default=Decimal("5"), ge=1, le=10
    )
    # Subtracted from tick_direction_veto_threshold at full ramp (e.g. the
    # default threshold 0 becomes -1.0 fully relaxed) - allows a
    # tick-negative entry through rather than requiring purely non-negative
    # recent tape direction.
    idle_cash_max_tick_relaxation: Decimal = Field(
        default=Decimal("1.0"), ge=0, le=2
    )
    # The opening print is naturally wider/choppier than mid-day trading, so
    # the normal spread/extension gates - tuned for profitable mid-day
    # scalping - reject almost everything in the first few minutes after the
    # bell. This grace window relaxes both gates only for that opening
    # stretch, then snaps back to the tighter full-day thresholds.
    opening_grace_minutes: int = Field(default=10, ge=0, le=120)
    opening_grace_spread_multiplier: Decimal = Field(
        default=Decimal("2"),
        ge=1,
        le=10,
    )
    opening_grace_extension_multiplier: Decimal = Field(
        default=Decimal("2"),
        ge=1,
        le=10,
    )
    # By request: "we do not want intense play in extended hours, but
    # we want play for sure." Live evidence: over a ~7.5 hour pre-
    # market stretch, "spread too wide to scalp profitably" (entry_
    # spread_ok, the GENERAL momentum path's entry gate - despite the
    # "scalp" wording in that decision reason string, it has nothing
    # to do with the separate volatility-scalp cohort, which is
    # already fully blocked outside core hours by its own hard gate)
    # rejected candidates roughly 2-3x more than every other reason
    # combined - STOCK_ENTRY_MAX_SPREAD_PERCENT's tight 0.5% default
    # is realistic for core hours' real liquidity, but genuinely hard
    # for almost anything to clear before real two-sided volume shows
    # up pre-market. Modestly loosens (not fully opens) the spread bar
    # outside core hours only - the existing "only established/
    # popular symbols trade outside core hours" bucket restriction
    # already keeps this to established names, not a free-for-all.
    # By request, recalibrated: "pre trading should not be too
    # intense, and it should just set up the main gainers for the
    # day" - pre-market's job shifts toward identifying candidates
    # (refresh_premarket_gainers/refresh_agent_predicted_gainers) for
    # when core hours actually start, not aggressive trading itself.
    # Lowered 3x -> 2x - still more room than the tight core-hours
    # 0.5% default (real pre-market liquidity genuinely can't clear
    # that), just less loosened than before.
    extended_hours_spread_multiplier: Decimal = Field(
        default=Decimal("2"),
        ge=1,
        le=10,
    )

    def stock_capital_fractions(self) -> dict[str, Decimal]:
        return {
            "POPULAR": self.stock_popular_capital_fraction,
            "PENNY": self.stock_penny_capital_fraction,
            "DISCOVERY": self.stock_discovery_capital_fraction,
        }

    def stock_bucket_slot_limits(self) -> dict[str, int]:
        fractions = self.stock_capital_fractions()
        buckets = list(fractions)
        limits = {bucket: 0 for bucket in buckets}
        remaining = self.max_open_positions
        if remaining >= len(buckets):
            for bucket in buckets:
                if fractions[bucket] > 0:
                    limits[bucket] = 1
                    remaining -= 1
        while remaining > 0:
            bucket = max(
                buckets,
                key=lambda item: (
                    float(fractions[item])
                    / max(1, limits[item]),
                    float(fractions[item]),
                ),
            )
            limits[bucket] += 1
            remaining -= 1
        return limits
