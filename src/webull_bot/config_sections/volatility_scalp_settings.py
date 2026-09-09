from decimal import Decimal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class VolatilityScalpSettings(BaseSettings):
    """The parallel, high-frequency volatility-scalp cohort strategy:
    eligibility, entry/exit signals (dip, breakout, Heikin-Ashi
    reversal, Parabolic SAR), averaging-down, sizing, and exposure caps."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Volatility-scalp: a parallel, much faster entry/exit path for
    # symbols whose own realized short-window volatility clears
    # volatility_scalp_min_stdev_percent - buy a small dip, sell a small
    # rip, repeatedly, all day. Runs alongside the normal EMA/VWAP trend
    # entries (unaffected for everything else); only the profit-take side
    # of the exit is overridden for a volatility-scalp position (see
    # TradingStrategy.volatility_scalp_target_price and its use in
    # AutoTrader.trade_stocks) - the normal stop-loss stays fully in
    # effect, since a "very volatile" stock is exactly where that
    # protection matters most.
    volatility_scalp_enabled: bool = True
    # By request, after pre-market losses ("no volatility scalp in
    # extended hours"): superseded the earlier dampened-intensity dial
    # that used to live here - fresh volatility-scalp entries and
    # averaging-down now simply never fire outside core hours at all
    # (see AutoTrader.trade_stocks), so there's no partial-intensity
    # case left to configure. Exits/repricing/position management for
    # anything already held are unaffected either way.
    volatility_scalp_lookback_samples: int = Field(default=20, ge=5, le=200)
    # By request: "regime-dependent" strategy switching - momentum in
    # trending conditions, mean-reversion in ranging ones (research:
    # neither is universally better; each thrives in the condition it
    # fits). Kaufman's Efficiency Ratio (net price movement over the
    # window / sum of the window's absolute tick-to-tick movement) is
    # the standard, well-documented regime input behind KAMA - reuses
    # the same tick-price window volatility-scalp eligibility already
    # maintains (TradingStrategy.volatility_price_history), so this
    # costs zero additional API calls. ER near 1 = price moved directly
    # (trending/efficient); near 0 = lots of back-and-forth with little
    # net progress (ranging/choppy). 0.5 is the common convention.
    trend_efficiency_trending_threshold: Decimal = Field(
        default=Decimal("0.5"), gt=0, le=1
    )
    trend_efficiency_lookback_samples: int = Field(default=10, ge=3, le=100)
    # Lowered from 1.5% -> 0.8% by request - "if the bar for entry is
    # too restrictive, lower the bar." This is the hard AND gate every
    # entry signal sits behind (is_volatility_scalp_eligible), so it's
    # the single biggest lever on how MANY symbols the whole strategy
    # even considers - a stock only needs to be moderately choppy now,
    # not extremely so, to qualify for the fast dip/breakout/HA-reversal
    # entry path.
    volatility_scalp_min_stdev_percent: Decimal = Field(
        default=Decimal("0.008"), gt=0, le=1
    )
    # By request: "the stocks being chosen have very low volume, thus
    # they do not fluctuate much, we need high volume stocks for more
    # volatility." is_volatility_scalp_eligible previously only checked
    # realized price stdev - a thin, illiquid name can show a large %
    # stdev purely from a few small prints knocking a wide, empty
    # spread around, not real tradeable movement. Requires cumulative
    # DOLLAR volume (price x share volume, regular + extended - see
    # TradingStrategy.metrics/prices) to also clear this floor, all day
    # (not just extended hours - see AutoTrader.trade_stocks' separate,
    # harder extended-hours cutoff).
    #
    # Live incident: a first version of this floor measured raw SHARE
    # count (500,000 shares) instead of dollar volume - meaningless for
    # a penny stock. SOAR cleared 500k shares of "volume" at ~$0.28/
    # share, only ~$140k of real dollar liquidity - too thin to absorb
    # this strategy's own repeated order flow, and its PROFIT exit
    # failed to fill even after three escalation-and-reprice cycles,
    # forcing a market-order exit at a loss the same day this shipped.
    # $5M/day is comfortably above that failure and scales correctly
    # at any price level, not just penny names.
    volatility_scalp_min_dollar_volume: Decimal = Field(
        default=Decimal("5000000"), ge=0
    )
    # Originally lowered from 0.5% -> 0.2% by request ("constantly buy
    # the dip... even a little rise"), then raised back up here as a
    # structural fix, not a same-day band-aid: 0.2% is noise-level on a
    # cheap, choppy penny stock - live incident, BTCT averaged down at
    # 1.79 then 1.78, essentially the same price, gaining no real risk
    # reduction per add. Compared against freqtrade's documented DCA
    # pattern, whose own docs warn a trigger this tight "runs out of
    # money" refilling into noise rather than a real dip. 1.0% is a
    # genuinely meaningful pullback - still well within reach multiple
    # times a day for a name that clears VOLATILITY_SCALP_MIN_STDEV_
    # PERCENT in the first place, but no longer fires on a single bid/
    # ask bounce. See volatility_scalp_averaging_step_multiplier for how
    # this widens further at each successive averaging level.
    volatility_scalp_dip_entry_percent: Decimal = Field(
        default=Decimal("0.01"), gt=0, le=1
    )
    # Each successive averaging-down buy requires a proportionally
    # bigger drop than the last, via required = dip_entry_percent * (1
    # + this * level) - level 0 (the first averaging buy) uses the base
    # threshold, level 1 needs 1.5x that, level 2 needs 2x, etc. A
    # position already several buys deep needs a genuinely bigger move
    # to justify yet another add, not just another noise-level tick -
    # spans the whole averaging ladder across a real range instead of
    # exhausting all VOLATILITY_SCALP_MAX_AVERAGING_BUYS attempts within
    # a percent or two of movement.
    volatility_scalp_averaging_step_multiplier: Decimal = Field(
        default=Decimal("0.5"), ge=0, le=5
    )
    # By request, after an end-of-day retrospective: "we just kept
    # buying at the wrong time." The SMA trend filter added earlier
    # only catches a MULTI-DAY downtrend (refreshed once daily from
    # daily-bar closes) - it does nothing for a stock simply having a
    # bad DAY today specifically, which is what repeated same-day
    # losses on one symbol (BTCT, three times in one session) actually
    # looks like: an intraday decline that hasn't shown up in the daily
    # SMA yet, since today's close hasn't happened. The scalp entry
    # path never checked the intraday VWAP at all (see vwap_supports_
    # entry, already used by the general strategy) - a stock trading
    # meaningfully below its own session VWAP is showing real intraday
    # weakness, not just a normal dip. Needs its own, much wider band
    # than VWAP_ENTRY_BAND_PERCENT (0.1%, tuned for the general
    # strategy's more liquid names) - a genuinely wide-spread, choppy
    # penny stock's normal dip-buy can easily sit several percent below
    # its own VWAP without that being a real warning sign.
    volatility_scalp_vwap_band_percent: Decimal = Field(
        default=Decimal("0.05"), ge=0, le=Decimal("0.5")
    )
    # Raised 0.2% -> 0.5% -> 1.0% by request. The first raise wasn't
    # enough - live incident: LHAI entered 1.185, averaged down to a
    # blended cost of ~1.17, and the 0.5% target closed it at 1.18,
    # barely above cost after fees ("closed too low and early"). 0.5%
    # of a ~$1 stock is a fraction of a cent in absolute terms - too
    # small to survive fees plus any real slippage. 1.0% keeps the
    # "quick" scalp character (still tiny relative to this cohort's
    # own volatility floor, MIN_HISTORICAL_VOLATILITY_PERCENT >= 3%)
    # while giving a real move room to actually register as profit.
    volatility_scalp_target_percent: Decimal = Field(
        default=Decimal("0.01"), gt=0, le=1
    )
    # By request: "when buying multiple shares, if needed be able to
    # sell them in parts as the value shifts, to maximize profits, or
    # minimize loss... buy 20, sell 5 every 5 cents it goes up... buy
    # 10, average down 10, and if it goes up a little sell 10 and keep
    # the rest for later." Scoped to PROFIT only - a stop-loss always
    # sells the full remaining quantity (real capital protection, no
    # partial on the downside, matching "minimize loss" specifically).
    # See TradingStrategy.volatility_scalp_partial_exit_quantity/
    # AutoTrader.evaluate_held_stock_exits.
    volatility_scalp_partial_exit_enabled: bool = True
    # Fraction of the CURRENTLY HELD quantity sold on each partial
    # exit (0.5 = sell half, keep half riding). Applied repeatedly as
    # price keeps climbing, so a held position naturally scales out in
    # a shrinking ladder rather than one all-or-nothing sale.
    volatility_scalp_partial_exit_fraction: Decimal = Field(
        default=Decimal("0.5"), gt=0, lt=1
    )
    # Required price move, from the price at the LAST partial exit,
    # before another one can fire - without this, the very next 0.25s
    # cycle would immediately sell again at essentially the same price
    # (the quick target itself doesn't move once a position is
    # partially closed, only the held quantity shrinks). This is what
    # actually implements "sell some more every N cents/percent it
    # keeps climbing" instead of dumping the whole position at once.
    volatility_scalp_partial_exit_reprice_percent: Decimal = Field(
        default=Decimal("0.01"), gt=0, le=1
    )
    # Once the remaining quantity after a partial sale would drop to
    # or below this many shares, sell the FULL remainder instead of
    # another partial - keeps the tail end of a ladder from grinding
    # down into odd-lot slivers too small to matter (or, for a sub-$1
    # stock, below Webull's own 100-share minimum order size).
    volatility_scalp_partial_exit_min_remainder_shares: int = Field(
        default=10, ge=1, le=1000
    )
    # Gates TradingStrategy.volatility_scalp_exit_override's fourth,
    # most-eager exit path (stalling momentum on a profitable position) -
    # by request: "too trigger happy to sell... not capturing the
    # profits when it can." Price must have already covered at least
    # this fraction of the full distance from cost to the quick target
    # before an early stall-triggered exit is allowed to fire - a small,
    # immediate profit alone isn't enough anymore, it has to actually be
    # most of the way to the real target first.
    volatility_scalp_momentum_stall_min_profit_fraction: Decimal = Field(
        default=Decimal("0.6"), gt=0, le=1
    )
    # Raised 3 -> 8 by request, after finding buying power sitting idle
    # ("not investing all the capital") - 3 concurrent slots capped how
    # much of the account's capital the scalp strategy could ever have
    # working at once, regardless of how much buying power remained.
    # Per-trade sizing (volatility_scalp_target_notional_buying_power_
    # fraction) and the total-exposure/position-value caps already
    # backstop this independently - raising the slot count lets sizing
    # actually reach those existing caps instead of stopping short of
    # them for lack of an open slot.
    #
    # By request, urgent, after live evidence: "it has sold twice for
    # heavy losses without averaging down." Traced the exact numbers -
    # FNGR (a sub-$1 stock, forced into Webull's 100-share minimum lot
    # regardless of account size, ~$45 notional at its price) needed
    # to average down with only $47.80 remaining buying power (5
    # concurrent scalp positions already open, spreading an ~$187
    # account thin). That single forced-minimum buy alone would have
    # consumed 94% of what was left, blowing straight past the 12%
    # per-symbol risk budget - averaging_down_capacity correctly
    # computed ZERO room before any averaging could even start, not
    # because of a broken cap, but because too many concurrent
    # positions had already spread the account's real capital too
    # thin to defend any single one of them. Lowered 8 -> 3 - fewer
    # concurrent slots means genuinely more capital stays available
    # behind each open position, so a forced-minimum-lot averaging buy
    # on a low-priced stock doesn't immediately exhaust the risk
    # budget the moment it's needed.
    volatility_scalp_max_concurrent_positions: int = Field(
        default=3, ge=1, le=20
    )
    # By request: "if they dip a lot after you buy, average it out with
    # another buy" - caps how many additional buys a single held cohort
    # position can make while averaging down, bounding worst-case
    # exposure per symbol to (this + 1) * volatility_scalp_share_count's
    # fixed lot size, instead of an unbounded chase. 0 disables
    # averaging entirely. Raised 3 -> 5 by request - "not averaging
    # down enough."
    #
    # By request, after urgent live evidence ("it is still not
    # averaging down properly, make sure it tries to average down
    # before the stop loss"): live AVGDOWN diagnostic showed FNGR
    # blocked on "averaging cap reached" for 6+ minutes straight before
    # its stop-loss fired - averaging_down_capacity's own account-risk-
    # derived ceiling (see volatility_scalp_max_symbol_risk_fraction)
    # came out well above 5 for this account's actual buying power at
    # the time, confirming the CONFIGURED "5" itself, not real risk
    # capacity, was the binding constraint. Raised 5 -> 10 (the field's
    # own max) - averaging_down_capacity's risk-based cap (still fully
    # in effect, unchanged) remains the real backstop on worst-case
    # exposure regardless of this ceiling.
    volatility_scalp_max_averaging_buys: int = Field(default=10, ge=0, le=10)
    # Research finding (compared against freqtrade's documented DCA
    # pattern after "basically only taking losses" was reported live):
    # a mature DCA implementation never fully suppresses the stop-loss
    # during averaging - it keeps a wide-but-always-active hard stop
    # live from entry as a catastrophic-loss backstop distinct from the
    # per-level re-buy logic. A drop beyond this means a real
    # breakdown, not a normal dip, and the actual stop-loss is let
    # through instead of staying suppressed until every averaging
    # attempt is exhausted.
    #
    # By request, after live evidence ("way too chill with stop loss
    # right now instead of averaging down first"): the DCA ladder's
    # per-level required drop (volatility_scalp_dip_entry_percent *
    # (1 + averaging_step_multiplier * level) - 1%, 1.5%, 2%, 2.5%, 3%
    # across the 5 default levels, each measured against the average
    # cost AFTER the prior add) barely fit inside the original 5%
    # backstop - by the time a fast decline reached the later levels'
    # required drop, the cumulative real move from the ORIGINAL entry
    # price could already exceed 5%, hitting the backstop before more
    # than 1-2 levels ever got a real chance to fire. Raised 5% -> 8%
    # to give the whole ladder genuine room to operate across a fast
    # decline, not just the first level or two, while still keeping a
    # real catastrophic-loss backstop rather than removing it.
    volatility_scalp_hard_stop_percent: Decimal = Field(
        default=Decimal("0.08"), gt=0, le=1
    )
    # By request: bound worst-case per-symbol exposure from averaging
    # down. Research finding acted on directly: "doubling down three
    # times can turn a 7% position into an 18% loss... in a bad market
    # that 50% can be 80%." Even fully averaged down to volatility_
    # scalp_max_averaging_buys and hitting the hard-stop floor, a
    # single symbol can't cost more than this fraction of buying
    # power - see TradingStrategy.averaging_down_capacity, which may
    # cap effective averaging BELOW the configured max on a small
    # account. The "5" above becomes a ceiling, not a target.
    volatility_scalp_max_symbol_risk_fraction: Decimal = Field(
        default=Decimal("0.12"), gt=0, le=1
    )
    # Live incident: GAUZ's routine 2-7% spread meant the general
    # STOCK_ENTRY_MAX_SPREAD_PERCENT (0.50%, tuned for a quote-glitch on
    # an otherwise normal, liquid stock) almost never let the exit
    # pricing fall back to the ask - exits depended entirely on the bid
    # alone clearing cost, a much harder bar than the entry side's dip
    # signal, so the position kept averaging down far faster than it
    # could ever exit. This cohort is deliberately wide-spread/choppy by
    # its own selection criterion, so its own exit pricing gets a wider,
    # separately-tunable bound instead of the general one.
    volatility_scalp_max_exit_spread_percent: Decimal = Field(
        default=Decimal("8"), gt=0, le=Decimal("50")
    )
    # Live incident: GAUZ alone grew to ~66% of total account value.
    # Caps any single cohort symbol's total position value (existing +
    # a prospective new buy, whether a fresh entry or an averaging-down
    # buy) to this fraction of account value - averaging is still
    # allowed up to VOLATILITY_SCALP_MAX_AVERAGING_BUYS, but never to
    # the point of concentrating most of the account in one name.
    #
    # By request, after live evidence ("even it is supposed to avg down
    # it is not executing it"): this check runs AFTER the real
    # averaging-down gate (which has its own AVGDOWN diagnostic) and
    # was silently zeroing the buy quantity with no logging at all - a
    # candidate could clear every gate condition and still never place
    # an order. The 35% default was calibrated for the OLD 5-buy
    # ladder (roughly 5 buys * ~7% of account value each); raising
    # volatility_scalp_max_averaging_buys to 10 without widening this
    # too meant a deep, real decline could hit THIS cap well before
    # exhausting the new averaging capacity, silently undoing that
    # fix. Raised 35% -> 50% - still meaningfully below the whole-
    # cohort ceiling just below (60%), so one symbol still can't
    # consume the entire cohort's exposure allowance alone.
    volatility_scalp_max_position_fraction: Decimal = Field(
        default=Decimal("0.50"), gt=0, le=1
    )
    # Per-symbol caps alone don't bound worst case: with up to
    # VOLATILITY_SCALP_MAX_CONCURRENT_POSITIONS symbols each individually
    # allowed to reach VOLATILITY_SCALP_MAX_POSITION_FRACTION, a
    # correlated selloff across the whole cohort (likely, since these are
    # explicitly the most volatile names selected together) had no
    # aggregate brake - three symbols could each legitimately reach 35%
    # of the account. This caps total cohort exposure across every
    # symbol combined, so an account-wide correlated move is still
    # bounded even when each individual symbol's own cap is satisfied.
    volatility_scalp_max_total_exposure_fraction: Decimal = Field(
        default=Decimal("0.60"), gt=0, le=1
    )
    # Opening-range-breakout entry signal, adapted from the classic Dual
    # Thrust strategy: fires when price pushes above the rolling window's
    # own recent local range (the same lookback the dip signal already
    # uses) by K times that range's own size - a fresh breakout to a new
    # high with real range behind it, not just a one-tick blip. This is
    # an ADDITIONAL alternative entry trigger (OR'd with the existing dip
    # signal, not a replacement) - by request, every extra qualifying
    # signal should mean MORE trading opportunities, not a stricter bar.
    # Lowered from 0.5 -> 0.2 - "if the bar for entry is too
    # restrictive, lower the bar" - a smaller K means a smaller push
    # past the recent range is enough to count as a real breakout.
    volatility_scalp_breakout_k: Decimal = Field(
        default=Decimal("0.2"), gt=0, le=Decimal("5")
    )
    # Heikin-Ashi reversal confirmation: synthetic OHLC bars are bucketed
    # from the same rolling tick-price window (HEIKIN_ASHI_BAR_SAMPLES
    # consecutive ticks per bar), then transformed to Heikin-Ashi
    # candles. A third, independent alternative entry trigger alongside
    # the dip and breakout signals - fires on a confirmed bullish
    # reversal (a green HA bar with little/no lower wick immediately
    # following a red one).
    # bar_samples * bar_count should stay <= volatility_scalp_lookback_
    # samples (default 20) so a full bar_count of bars can actually form
    # - _synthetic_bars degrades gracefully with fewer if not, but signal
    # quality is best with the full count. 3 * 6 = 18, comfortably under
    # the 20-sample window.
    heikin_ashi_bar_samples: int = Field(default=3, ge=2, le=50)
    heikin_ashi_bar_count: int = Field(default=6, ge=3, le=50)
    # Parabolic SAR trailing-stop exit: computed over the same synthetic
    # bars as the Heikin-Ashi signal. Used as an ADDITIONAL exit trigger
    # for a held volatility-scalp position, alongside (not instead of)
    # the existing quick profit target - either one can independently
    # close the position, locking in a trend reversal even if price
    # hasn't yet cleared the fixed percentage target.
    parabolic_sar_af_step: Decimal = Field(
        default=Decimal("0.02"), gt=0, le=1
    )
    parabolic_sar_af_max: Decimal = Field(default=Decimal("0.2"), gt=0, le=1)
    # Curated daily cohort, not the whole scanned universe: identify a
    # small handful of the cheapest, most volatile names and concentrate
    # on rapidly cycling just those until they cool off, instead of
    # spreading thin across every eligible symbol seen in passing. See
    # AutoTrader.select_volatility_scalp_symbols, re-run periodically
    # (VOLATILITY_SCALP_RESELECT_SECONDS) so a symbol that's slowed down
    # gets dropped and a newly-hot one takes its place.
    volatility_scalp_symbol_count: int = Field(default=4, ge=1, le=10)
    # Raised from $1.50 -> $5 by request - "the stocks in the strategy
    # can be below 5 [dollars]."
    volatility_scalp_max_price: Decimal = Field(
        default=Decimal("5"), gt=0, le=Decimal("50")
    )
    # Target dollar notional per volatility-scalp entry (see
    # TradingStrategy.volatility_scalp_share_count) - by request, don't
    # cap every penny stock at a flat 100 shares or every $1+ stock at a
    # flat 20-50 shares; size UP toward this dollar budget instead,
    # rounded to a clean lot (nearest 100 shares under $1, matching
    # Webull's own lot-restricted-band minimum there; nearest 10 shares
    # at $1+). This is a CEILING, not the actual per-trade target most
    # of the time - see volatility_scalp_target_notional_buying_power_
    # fraction just below, which usually binds first on a small
    # account. volatility_scalp_max_position_fraction/
    # volatility_scalp_max_total_exposure_fraction and the buying-power
    # affordability check already applied at every call site still
    # backstop this - raising the per-trade target doesn't remove
    # either cap, it just means sizing can actually reach them instead
    # of always landing far below.
    volatility_scalp_target_notional: Decimal = Field(
        default=Decimal("400"), gt=0, le=Decimal("100000")
    )
    # Live sanity check caught this: a flat dollar target alone doesn't
    # scale with account size - on a small account (live example:
    # $107.80 buying power), a $400 flat target gets silently zeroed by
    # the affordability check on nearly every attempt (scalp_quantity
    # forced to 0, not gracefully shrunk - see trade_stocks), meaning
    # close to ZERO trades instead of "high frequency." The actual
    # per-trade target is min(volatility_scalp_target_notional, buying_
    # power * this fraction) - scales down automatically on a small
    # account and up as the account grows, so this doesn't need
    # re-tuning by hand as the account's size changes.
    volatility_scalp_target_notional_buying_power_fraction: Decimal = Field(
        default=Decimal("0.15"), gt=0, le=Decimal("1")
    )
    # By request: "we don't only want it to stick with the same cohort
    # throughout the day, it should also add to the cohort, which is
    # why we also have such a big universe." select_volatility_scalp_
    # symbols already re-ranks the cohort from data across the WHOLE
    # scanned universe (not just today's starting picks) and fully
    # replaces stale members with newly-hot ones - the mechanism the
    # user is asking for already existed - but the original 1800s (30
    # minute) cadence made that feel static relative to how fast
    # everything else in this bot moves (1s scalp repricing, 0.25s
    # position protection). Lowered to 300s (5 minutes) - meaningfully
    # more dynamic without excessive churn. An open position in a
    # symbol that drops out of the cohort is never stranded either way -
    # volatility_scalp_positions (a separate, position-based set) keeps
    # it fully managed regardless of current cohort membership.
    volatility_scalp_reselect_seconds: int = Field(
        default=300, ge=60, le=86400
    )
    # Zeroed by explicit request - "orders can be made as frequently as
    # possible without a cooldown." The only remaining gap between a
    # position closing and re-entering the same symbol is however long
    # the fill/account-refresh round-trip itself actually takes (see
    # volatility_scalp_positions' synchronous, race-free tracking in
    # bot.py) - there's no artificial wait layered on top of that.
    volatility_scalp_reentry_cooldown_seconds: int = Field(
        default=0, ge=0, le=300
    )
    # By request, after the DAIC incident (3 stop-losses in ~9 minutes
    # on one symbol during a fast decline, erasing the day's gains):
    # a narrow, symbol-specific exception to the zeroed cooldown above -
    # only pauses fresh re-entry into a symbol that JUST stopped out,
    # for a few minutes, long enough for a fast decline to finish
    # shaking out. Deliberately NOT a same-day quarantine (rejected
    # earlier as "a bandaid") and compatible with "keep trading through
    # losses" - every other symbol and slot is unaffected. See
    # AutoTrader.post_stop_reentry_ready.
    volatility_scalp_post_stop_cooldown_seconds: int = Field(
        default=300, ge=0, le=3600
    )
    # Warm-starts a symbol's volatility window from real M1 bars the
    # moment it's first scanned, instead of needing several live scan
    # cycles just to accumulate enough snapshot-poll samples to become
    # eligible - see WebullAPI.recent_minute_closes and AutoTrader.
    # trade_stocks' bar-seed call. Self-limiting: only ever fetched once
    # per symbol (skipped the moment its window is non-empty), so this
    # never grows unbounded as the watchlist rotates through.
    volatility_scalp_bar_seed_enabled: bool = True
    # How often a resting volatility-scalp PROFIT order gets re-quoted to
    # the current ask - deliberately much faster than the generic
    # ORDER_MONITOR_SECONDS (order_monitor_seconds) reprice cadence every
    # other resting PROFIT order uses, since this strategy exists
    # specifically to capture a fast-moving, choppy stock's small moves
    # "cent by cent" rather than rest passively - see
    # AutoTrader.reprice_volatility_scalp_exits. Lowered 1 -> 0.25
    # (the field's own floor) by request: "bring repricing to the 0.25
    # lane as well" - matches poll_seconds/the rest of the fast
    # position-protection loop's cadence exactly.
    volatility_scalp_reprice_seconds: Decimal = Field(
        default=Decimal("0.25"), ge=Decimal("0.25"), le=Decimal("30")
    )
