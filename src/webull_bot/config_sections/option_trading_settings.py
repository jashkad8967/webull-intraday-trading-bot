from decimal import Decimal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class OptionTradingSettings(BaseSettings):
    """Option-contract profit-target/stop-loss, the smoke-test bypass,
    forced-exit DTE floor, per-trade risk cap, affordability shortlist
    sizing, averaging-down knobs, and the straddle opt-in."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # By request: "if there is immediate profit after a buy, why are
    # you waiting to sell it, just capture the profit" / "it just
    # ends up going down then." Options routinely move 10-30%+
    # intraday off a modest underlying move (leverage), but a 75%
    # target was rare enough to hit before the premium round-tripped
    # back down - a real, genuinely-favorable move was regularly
    # never captured because the bar was set for a much bigger,
    # rarer run. Lowered to a bar an actual quick pop can realistically
    # clear, so a real gain gets locked in instead of being held out
    # for a 75% move that usually never comes before reversing.
    option_take_profit_percent: Decimal = Field(default=Decimal("0.15"), gt=0)
    # By request: "it doesn't buy puts while there is a dip, or a
    # call on a dip entry and quickly sell it. This should happen
    # for quick profit." Lets a CALL enter on the underlying's own
    # volatility-scalp dip signal (and a PUT on the mirror-image rip
    # signal) as an ADDITIONAL entry trigger alongside the existing
    # EMA-trend direction signal - see _evaluate_option_entry. On by
    # default since it reuses the same already-vetted stock-side
    # dip/rip signals and eligibility bar, not a new speculative
    # mechanism.
    option_scalp_enabled: bool = True
    option_stop_loss_percent: Decimal = Field(default=Decimal("0.50"), gt=0, le=1)
    # By explicit request, for a one-off diagnostic: "make sure it
    # fires... no barrier, quickly sell it, and then change the option
    # strategy again." Off by default (real gates always apply) - when
    # explicitly turned on, trade_options skips the direction signal,
    # delta, IV percentile, market-regime, wash-sale, stop-loss-guard,
    # and quarantine checks for entries (structural checks - DTE,
    # affordability/sizing, cooldown, rate cap, max open positions -
    # still apply, so this can't spam unlimited orders), and
    # option_take_profit_percent is meant to be turned down alongside
    # it (live .env only, not this default) so the position closes
    # again almost immediately via the normal exit path instead of
    # being held. This is a temporary smoke-test switch to prove the
    # order-placement pipeline works end to end on a real contract -
    # not a permanent strategy change.
    option_smoke_test_mode: bool = False
    # Forced exit once a held contract is this many days or fewer from
    # expiration, regardless of target/stop - theta/gamma accelerate sharply
    # in the final days and holding through that stops being a directional
    # bet and becomes pin-risk roulette. By request: "closed out at least
    # 1 week before" expiration.
    option_min_hold_dte: int = Field(default=7, ge=0, le=30)
    # Never risk more than this fraction of buying power on a single options
    # entry - a defined-risk-per-trade cap layered on top of (not instead
    # of) OPTION_QUANTITY and MAX_ORDER_NOTIONAL.
    option_capital_fraction: Decimal = Field(default=Decimal("0.05"), gt=0, le=1)
    # By request: "look for cheaper options to buy in to." select_atm_
    # options always picked the single strike nearest the money -
    # correct for delta, but on a small account often unaffordable
    # outright (option_order_quantity silently rounds to 0 contracts
    # when a single contract's premium*100 exceeds what the risk cap
    # allows, quietly producing zero real option trades). This is how
    # many of the NEAREST-to-ATM candidate strikes (per expiration/
    # type) get quoted at discovery time so a cheaper, still-reasonably
    # -close strike can be picked instead when the true ATM one doesn't
    # fit - see WebullAPI.select_atm_options's own docstring.
    option_affordability_shortlist_size: int = Field(default=6, ge=1, le=20)
    # By request: "you can also use averaging down... for options as
    # well" - options analog of the volatility-scalp averaging-down
    # knobs, but wider, since options routinely move a much larger
    # percentage than the underlying stock does.
    option_averaging_down_dip_percent: Decimal = Field(
        default=Decimal("0.20"), gt=0, le=1
    )
    option_averaging_step_multiplier: Decimal = Field(
        default=Decimal("0.5"), ge=0, le=5
    )
    option_max_averaging_buys: int = Field(default=2, ge=0, le=10)
    option_averaging_reentry_cooldown_seconds: Decimal = Field(
        default=Decimal("60"), ge=0, le=3600
    )
    # By request: "you can... use call and put simultaneously type
    # strategies for options as well" - off by default (a genuine
    # straddle risks paying two premiums instead of one when the
    # underlying doesn't move enough), opt-in via this flag. See its
    # use in trade_options' direction-match gate.
    option_straddle_enabled: bool = False
