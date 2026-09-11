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
    # By explicit request ("the options chosen are not as volatile...
    # nike again is failing"): a strictly higher, option-specific
    # volatility floor on top of is_volatility_scalp_eligible's own
    # stdev bar (0.8%, tuned for the stock-side cohort and already
    # lowered once at that side's own request) - SPY (a broad index
    # ETF, inherently dampened relative to a single stock) and NKE (a
    # historically calm blue-chip) were both clearing the stock bar
    # repeatedly without being genuinely volatile enough to justify
    # the premium risked on an option.
    option_min_volatility_percent: Decimal = Field(
        default=Decimal("0.02"), gt=0, le=1
    )
    # By explicit request ("is something wrong with the options
    # strategy that it keeps making losing entries" / "do research
    # online... to see what you are missing") - research confirmed
    # the exact pattern showing up in this account's real positions
    # ($0.13-0.30 contracts): buying cheap, far-out-of-the-money
    # options is a well-documented small-account failure mode (the
    # "lottery ticket" trap) - a near-zero-delta contract needs a
    # huge underlying move just to become profitable, while theta
    # bleeds the whole time. The INTENDED guard against this,
    # option_delta_ok (OPTION_DELTA_MIN/MAX), turned out to be
    # completely inert in practice: Webull's option snapshot doesn't
    # return a delta field on this account, so option_delta() always
    # returns None and the gate always fails OPEN (24h of live logs,
    # zero "delta out of range" rejections and zero confirmed delta
    # readings). Moneyness (strike distance from the current
    # underlying price) is a reliable, always-available proxy for
    # the same thing delta was meant to filter - unlike delta, it
    # never depends on the broker actually returning a greek. Used
    # to cap BOTH select_atm_options' primary near-ATM shortlist and
    # its further-OTM affordability fallback search (the fallback
    # previously had no distance ceiling at all, only a count cap -
    # exactly how a "can't afford anything near the money" underlying
    # could end up trading a genuine lottery ticket instead of simply
    # being skipped that cycle).
    option_max_moneyness_percent: Decimal = Field(
        default=Decimal("0.15"), gt=0, le=1
    )
