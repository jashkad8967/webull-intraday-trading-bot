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
    #
    # Lowered 0.15 -> 0.10 by explicit request ("15% gain is also a
    # lot"), alongside the stop coming in to 0.20. On a $363 account a
    # typical 2-contract position is ~$280, so 15% was +$42 against a
    # 50% stop of -$140: the downside was over 3x the upside on every
    # single trade. 10% banks +$28 against -$56, which a normal win
    # rate can actually carry.
    option_take_profit_percent: Decimal = Field(default=Decimal("0.10"), gt=0)
    # By request: "it doesn't buy puts while there is a dip, or a
    # call on a dip entry and quickly sell it. This should happen
    # for quick profit." Lets a CALL enter on the underlying's own
    # volatility-scalp dip signal (and a PUT on the mirror-image rip
    # signal) as an ADDITIONAL entry trigger alongside the existing
    # EMA-trend direction signal - see _evaluate_option_entry. On by
    # default since it reuses the same already-vetted stock-side
    # dip/rip signals and eligibility bar, not a new speculative
    # mechanism.
    # By explicit request, after three positions sat open for over two
    # hours going nowhere: "if something is not going for much profit
    # at all then make it sell for even cents" / "now it is in a loss,
    # why didn't it sell".
    #
    # Nothing else in the exit ladder can act on a trade that simply
    # does not work. The target needs +10%, the profit-lock trail must
    # first arm at +2.5%, the stop needs -20%, and
    # boost_stalled_positions is explicitly "never sells at a loss". A
    # position that drifts a few percent negative and stalls there
    # hits NONE of them. Live 2026-09-22: MARA (-8.3%), SOFI (-3.5%)
    # and NFLX (-1.0%) were all opened at 10:46 and still open past
    # 12:52, holding capital that could have funded setups that did
    # work. On a $363 account with ~$100 positions, three dead trades
    # is the entire account doing nothing.
    option_stale_exit_enabled: bool = True
    # How long a position may go nowhere before it is closed. Measured
    # from entry, and only ever consulted for a position that never
    # armed the trail - a trade that is genuinely working is never cut
    # short by this clock.
    option_stale_exit_minutes: int = Field(default=45, ge=1, le=390)
    # The most this is willing to give up to free the capital. Past
    # this the position is not stalled, it is losing, and
    # option_stop_loss_percent owns it. Without this bound the timer
    # would dump every loser at whatever the market offered the
    # instant it expired.
    option_stale_exit_max_loss_percent: Decimal = Field(
        default=Decimal("0.08"), ge=0, le=1
    )
    # Held-option exit management on the fast protection thread.
    # _evaluate_option_exit computes the profit target, stop,
    # profit-lock trail and stale exit, and records the peak the trail
    # rides - but it was only ever called from trade_options inside
    # the slow scan. Live 2026-09-22 consecutive SCAN lines were
    # 13:13:02, 13:17:50 and 13:24:33, so the whole exit ladder was
    # sampled every 5-7 minutes. A trail cannot protect a high it
    # never observed.
    # Whether the time-aware stop widening applies to OPTIONS. OFF by
    # explicit request, after the numbers were compared directly.
    #
    # The widening (1.5x for the first time_aware_stop_widen_seconds)
    # made sense paired with the old 50% option stop. Against the 20%
    # that replaced it, it turns the stop into 30% for the first
    # minute - and focus mode sizes a position at nearly the whole
    # balance, so a 2x$1.80 position could lose $108 of a $369 account
    # (29%) inside 60 seconds, under a stop deliberately set to cap
    # losses at 20%.
    #
    # The NKE incident that motivated widening does not justify it
    # here either: that stop fired SIX MINUTES after fill, well
    # outside the 60-second window, so widening would not have
    # prevented it.
    #
    # The stock side keeps time_aware_stop_enabled untouched - this
    # only changes options, where premium leverage makes the same
    # multiplier cost far more.
    option_time_aware_stop_enabled: bool = False
    held_option_exit_enabled: bool = True
    # Seconds between fast-loop held-option exit scans. By explicit
    # request ("it needs to be subsecond for options as well") -
    # options move far faster than the underlying, and a trail that
    # samples slowly cannot protect a spike it never observes.
    #
    # 0.5s rather than poll_seconds, and the reason is a hard budget,
    # not caution: market_requests_per_minute is 240, i.e. 4 market
    # calls per SECOND for the entire process. One option_quotes call
    # covers every held position at once (the endpoint takes 20
    # symbols), so 0.5s costs 2 calls/sec and leaves 2/sec for the
    # scan loop's own quotes. At 0.25s this alone would consume the
    # whole allowance and starve the scanner that finds the next
    # trade - trading one bottleneck for another.
    #
    # NOTE: the fast loop cannot tick faster than poll_seconds, so
    # this only takes effect if poll_seconds <= this value.
    held_option_exit_seconds: Decimal = Field(
        default=Decimal("0.5"), gt=0, le=60
    )
    option_scalp_enabled: bool = True
    # Tightened 0.50 -> 0.20 -> 0.10, each by explicit request. The
    # original 0.50 was not a stop so much as a catastrophe gate; 0.20
    # capped a 2-contract $280 position at -$56.
    #
    # 0.10 was requested immediately after this fired live on
    # 2026-09-24: an NVDA put entered at 1.74 stopped at 1.39, exactly
    # the configured -20%, for -$35. The stop worked perfectly - the
    # problem was that option_capital_fraction is now 1.0, so that one
    # contract was 65% of the account and a routine stop became a 13%
    # ACCOUNT loss. At 0.10 the same trade loses ~$17.
    #
    # 0.10 -> 0.05 by explicit request, and this time the number comes
    # from measured results rather than judgement. Every option round
    # trip on 2026-09-24:
    #
    #   WINS   n=12  median  5.3%  best 16.6%  worst  2.5%
    #          reached the 10% take-profit: 1 of 12
    #   LOSSES n=6   median -13.0%  worst -26.3%  smallest -10.1%
    #   win rate 67%, avg win $6.18 vs avg loss $17.07
    #   breakeven needed 73% -> NEGATIVE EXPECTANCY
    #
    # The bot picks direction well and still lost money, because losses
    # ran ~2.8x the size of wins. A 10% stop is sized against a win
    # that essentially never arrives: the median favourable move is
    # 5.3%, so winners are taken by the profit-lock trail around 5%
    # while losers travel the full stop. At a 5% stop and a 5.3% median
    # win, a 67% win rate is roughly +1.9% expectancy per trade.
    #
    # The real risk, stated plainly: option spreads run 2-3%, so 5%
    # leaves only about two spreads of room. Expect noticeably more
    # stop-outs, and expect some of today's winners to become losers.
    # That is the trade being made deliberately - smaller, more
    # frequent losses against wins that are finally larger than them.
    # Anything much tighter (3-4%) sits inside one spread and would
    # stop out on the bid/ask alone, collapsing the win rate the whole
    # calculation depends on.
    #
    # NOTE: option_stale_exit_max_loss_percent (0.08) is now looser
    # than this stop, so it can no longer bind - a position can never
    # be 8% down without having already stopped out. That leaves the
    # stale exit free to act on any stalled position, which is what it
    # is for.
    option_stop_loss_percent: Decimal = Field(default=Decimal("0.05"), gt=0, le=1)
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
    # The dollar budget a single option position targets, as a fraction
    # of buying power - option_order_quantity turns it into a contract
    # count exactly the way the stock side turns its own budget into a
    # share count (buying_power * fraction / cost), layered on top of
    # (not instead of) OPTION_QUANTITY and MAX_ORDER_NOTIONAL.
    #
    # Raised in two steps, both by explicit request. First 0.05 ->
    # 0.15 ("how do you decide quantity for stocks, do same with
    # options") to match the stock side's own stock_max_position_
    # fraction_of_buying_power. Then 0.15 -> 1.0 ("no cap for options,
    # i trust your algorithms judgement on entry" / "i told you it
    # should spend all the money if needed"): at 0.15 a ~$373 account
    # budgets ~$56 per option position, which affords exactly ONE $50
    # contract no matter how much buying power is actually free, so
    # the bot could never scale into a position it liked.
    #
    # At 1.0 this stops being a cap at all - affordability,
    # MAX_ORDER_NOTIONAL and OPTION_QUANTITY become the only real
    # bounds, and a single entry can consume most of the account.
    # That concentration is the deliberate, requested tradeoff: the
    # entry QUALITY gates (premium floor, moneyness cap, IV
    # percentile, volatility/RVOL floors, spread ceiling) are what
    # carry the risk budget now, not a blanket sizing cap.
    # Maximum share of buying power ONE option entry may take.
    #
    # Lowered 1.0 -> 0.4 by explicit request after a single position
    # swallowed the account. Live 2026-09-23: BABA took 2 contracts at
    # $1.45 = $290 of a $369 balance - 79% - in one name, and MARA
    # took most of what was left. One wrong direction call was
    # therefore the whole account, and both were CALLS, so they were
    # not even independent bets.
    #
    # 1.0 was set for "use the entire account" back when focus mode
    # meant ONE symbol, where concentration was the whole point. With
    # a cohort of ten the same setting just means the first contract
    # evaluated eats everything and the other nine never get funded -
    # it defeats the cohort rather than expressing it.
    #
    # Raised 0.4 -> 1.0 by explicit request ("should it not be using
    # all the capital"), and the reasoning behind 0.4 no longer holds.
    #
    # 0.4 was chosen to stop ONE position swallowing the account. The
    # thing that actually caused that - three separate SOFI contracts
    # opened because every guard was keyed on the exact option_symbol
    # and none had a per-underlying view - now has its own dedicated
    # fix in option_max_positions_per_underlying below. A sizing cap
    # was the blunt instrument standing in for a guard that did not
    # exist yet.
    #
    # Meanwhile 0.4 was actively degrading contract QUALITY, not just
    # size. Live 2026-09-24 on a $258 balance it capped each entry at
    # $103, i.e. $1.03 of premium, which excluded AMZN ($1.85), HOOD
    # ($2.25), PLTR ($2.50), BABA ($2.65), AVGO ($2.85) and TSLA
    # ($3.45) - six of the eight researched names, evicted within 90
    # seconds of the cohort locking. What is left under $1.03 is the
    # cheapest, furthest-OTM end of the board: precisely the low-delta
    # lottery tickets option_delta_ok exists to reject. The cap was
    # pushing the account toward the worst contracts available.
    #
    # The honest tradeoff at 1.0: one position at a time, so a wrong
    # direction call is the whole account. What carries the risk now
    # is the per-underlying cap, the profit-lock trail, the stop, and
    # the entry quality gates - not a blanket sizing limit that also
    # priced the account out of every liquid contract.
    option_capital_fraction: Decimal = Field(default=Decimal("1.0"), gt=0, le=1)
    # Concentration cap per UNDERLYING, the companion to the fraction
    # above: that one bounds how much each position costs, this one
    # bounds how many of them can ride on the same stock.
    #
    # Live 2026-09-23, minutes after the cohort fix let the account
    # start trading again: a verified 5-name cohort put 3 of its 4
    # open positions into SOFI - three different contracts, bought at
    # 0.63, 0.70 and 0.80 (averaging UP) - and took buying power from
    # $247 to $17. Nothing caught it. option_average_down_count is
    # keyed on the exact option_symbol, so each new SOFI strike
    # opened with a fresh budget and could not see the others;
    # max_open_positions is account-wide and was nowhere near its
    # limit. There was no per-underlying view at all.
    #
    # 1 by default: the cohort exists so the account is spread across
    # 5-10 researched names, and one contract per name is what
    # actually expresses that. Raising it re-admits exactly the
    # concentration the cohort was built to prevent. 0 disables the
    # cap entirely.
    option_max_positions_per_underlying: int = Field(default=1, ge=0, le=10)
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
    # By explicit request, after real account data confirmed it: 15 of
    # 19 recent exits were losses, every single one an option bought
    # under $0.20/share, averaging -$3.90 against $1.02 average wins.
    # Cheap option premiums swing 30-50%+ on routine noise (near-zero
    # intrinsic/extrinsic value, all leverage/decay), so a stop tight
    # enough to matter still gives back several times what a win
    # captures - a structural mismatch, not a timing/pricing bug. A
    # small account's own affordability ceiling was mechanically
    # forcing select_atm_options into exactly this cohort every time
    # nothing near-ATM fit. Applied as a hard floor BEFORE
    # affordability gets a vote (see select_atm_options) - a contract
    # this cheap is excluded outright, never merely deprioritized.
    option_min_premium_dollars: Decimal = Field(default=Decimal("0.50"), gt=0)
    # By explicit request ("how to immediately sell call and buy a
    # put at the tip of momentum and vice versa") - the original
    # options vision this whole strategy was built around: "take a
    # volatile, volume stock and keep buying calls on the rise,
    # selling calls, buying puts as the dip starts, sell puts, buy
    # calls as the dip ends... for multiple stocks simultaneously."
    # How long a momentum-exhaustion exit's flip signal (see
    # option_momentum_flip) stays valid before it's considered stale
    # and ignored - long enough to let the entry evaluation catch up
    # within the same or next scan cycle, short enough that it never
    # fires on unrelated later movement.
    option_momentum_flip_window_seconds: int = Field(default=180, ge=10, le=1800)
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
    # By explicit request ("lots of orders are being cancelled, make
    # me think that the pricing is not working correctly"): a resting
    # option BUY starts at the passive midpoint (per the earlier "mid
    # price is also fine" request) and reprice_resting_option_entries
    # kept tracking that same midpoint indefinitely - live evidence
    # showed NKE/RIVN/SNAP entries sitting at mid with ZERO entry-side
    # REPRICE activity until the generic order_timeout_seconds (120s)
    # hard-cancelled them unfilled. A passive mid order simply has
    # nothing to cross on a thin/wide-spread contract. Once an entry
    # has been resting this long unfilled, reprice_resting_option_
    # entries escalates its chase target from mid halfway toward the
    # ask (not the full ask - a first version of this did jump
    # straight to the full ask and a VZ put dip-entry filled at the
    # max-spread price as a result, paying the full spread cost on
    # what's meant to be a cheap scalp entry) instead of staying
    # pinned to mid all the way to the hard cancel. Mirrors stop_loss_
    # escalate_seconds' existing shape for stalled STOCK stop-loss
    # exits, which
    # had no options/entry-side equivalent until now.
    option_entry_escalate_seconds: int = Field(default=30, ge=5, le=120)
    # By explicit request ("just do not trade contracts that are not
    # easy to liquidify"): ORCL was stuck in a repeated STOP-loss
    # cycle - the position's own contract had a genuinely wide 40%+
    # bid/ask spread, so no exit price was ever "sane" enough to find
    # a buyer, and the stop-loss just kept resubmitting and timing out
    # unfilled while the position sat exposed. This never should have
    # been entered in the first place. A normal LIQUID option spread
    # runs wide by stock standards (a $1.00 contract quoting
    # $0.90/$1.10 - 20% - is fine, not broken), so this is deliberately
    # much looser than stock_entry_max_spread_percent (0.5%), but it
    # still draws a real line before OPTION_PRICE_SANITY_TOLERANCE's
    # 30% backstop would even engage - the goal is to never buy into a
    # contract that backstop would already struggle to exit later.
    option_max_entry_spread_percent: Decimal = Field(
        default=Decimal("25"), gt=0, le=100
    )
