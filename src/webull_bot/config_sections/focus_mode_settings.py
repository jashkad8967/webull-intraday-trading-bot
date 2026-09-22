from decimal import Decimal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class FocusModeSettings(BaseSettings):
    """Focus mode: the once-daily researched candidate batch, the small
    cohort of symbols picked out of it that the account trades options
    on, the net buy/sell pressure read, the profit-lock trailing stop,
    and the daily profit throttle."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # By explicit request: "find one really good volatile stock to play
    # with and go all in on that for the day... make sure you use all of
    # the capital."
    #
    # The blocker this actually removes is NOT sizing - option_order_
    # quantity already sizes all-in (option_capital_fraction is 1.0, so
    # its risk cap equals plain affordability). It's max_open_positions
    # (50): buying power gets consumed first-come-first-served by
    # whatever the scanner surfaces first, so no single name ever
    # receives the account. Focus mode is a SELECTION/GATING change -
    # once the cohort is locked, option entries on every underlying
    # outside it are rejected and new stock entries are suspended, so
    # the whole account is available to the names that earned it.
    focus_mode_enabled: bool = True
    # By explicit request: "allow a cohort of 5-10 stocks then, that all
    # fit the criteria so that there are more options to play with."
    # Focus mode originally locked exactly ONE name for the session,
    # which made the whole day contingent on that single symbol having
    # a contract the account could actually afford - and with a small
    # account the affordable band is narrow ($50-$363/contract here),
    # so one unaffordable pick meant zero trades all session. A cohort
    # widens the tradeable option universe without loosening any gate:
    # every member clears the SAME structural checks the single pick
    # had to (price band, share volume, affordability, not wash-blocked
    # in both directions) - this is more candidates, not weaker ones.
    #
    # Capital is deliberately NOT divided across the cohort. At this
    # account size that would be actively harmful: $363 split 10 ways
    # is ~$36/slot, below the cheapest contract the premium floor
    # allows (option_min_premium_dollars $0.50 = $50/contract), which
    # would size EVERY entry to zero and trade nothing at all. Entries
    # stay first-come-first-served on the full balance, exactly as the
    # single-symbol version sized them - the cohort buys availability
    # of setups, not simultaneous positions. As the account grows,
    # more of the cohort can be held at once with no change here.
    focus_cohort_size: int = Field(default=10, ge=1, le=25)
    # By request: "only take a good batch of stocks to look out for
    # everyday." Two-stage funnel - research a batch each morning
    # (this), then pick the focus cohort out of it at the open.
    #
    # Raised 8 -> 16 when the second stage became a cohort of up to
    # focus_cohort_size (10) rather than a single pick. This is the
    # PRE-FILTER pool, and the lock-time gates are not free - live
    # batches of 8 routinely left only 3-5 names standing after price/
    # volume/affordability, which would cap the cohort well below the
    # 5-10 that was asked for. The research ceiling that matters (past
    # ~50 names, analysis paralysis and poor execution) is far above
    # this; the 5-10 watchlist guidance now describes the cohort, which
    # is the set actually traded.
    daily_batch_size: int = Field(default=16, ge=1, le=50)
    # 07:45 CT (08:45 ET), inside the 08:30-09:30 ET window where
    # pre-market volume and catalyst releases (earnings, guidance,
    # FDA, upgrades) actually cluster - early enough to have a batch
    # ready before the bell, late enough that the pre-market tape is
    # meaningful. By explicit request ("change everything to central
    # time") - see trading_timezone's own comment for why this had to
    # shift together with the zone, not just the zone alone.
    daily_batch_refresh_time: str = "07:45"
    # Minimum overnight gap for a name to make the batch. A gap is the
    # cheapest available proxy for "something happened" on an API that
    # exposes no news feed - a stock gapping on real volume is gapping
    # BECAUSE of a catalyst, which is the signal the batch is trying to
    # capture.
    daily_batch_min_gap_percent: Decimal = Field(
        default=Decimal("2"), gt=0, le=100
    )
    # By explicit request, after two live incidents (GRML at 286.7%
    # gap, GRAL) both locked as the focus symbol with no real options
    # market: "the stocks we pick should be like fortune 500, or snp,
    # or dow stocks, popular, known, established." When on (the
    # default), the daily batch is intersected with config.option_
    # candidates() - the same curated large-cap list discover_
    # option_contracts already trusts - before gap/volume/spread
    # scoring runs, so an obscure thin mover can no longer dominate
    # purely on a large percentage move. Off switches back to the
    # unrestricted screener union.
    daily_batch_require_established_symbols: bool = True
    # Live incident: refresh_daily_batch's give-up used to fire the
    # moment `moment` crossed focus_lock_time directly - which
    # meant any restart landing AFTER 09:45 (every mid-session
    # redeploy) gave up with zero real retries, since the first
    # attempt was already past the cutoff. How long, in real elapsed
    # minutes since this attempt-loop's own first try (not wall-clock
    # proximity to any fixed time), to keep retrying an empty batch
    # before giving up for the day. option_eod_close_time remains a
    # hard backstop regardless of this value.
    daily_batch_retry_minutes: int = Field(default=20, ge=1, le=180)
    # 08:45 CT (09:45 ET): after the opening range resolves. The first
    # hour has the best setups, but 09:30-09:45 ET is also where
    # opening-range fakeouts concentrate - committing the entire
    # account at the bell on pre-market ranking alone is exactly the
    # trap this avoids. Pre-market ranking frequently does not survive
    # the open, so the batch is re-measured on regular-session data
    # before the pick. By explicit request ("change everything to
    # central time") - see trading_timezone's own comment for the
    # shift reasoning.
    focus_lock_time: str = "08:45"
    # NOTE: there is deliberately no live-RVOL gate ON THE FOCUS PICK
    # itself, though the underlying research is real (below-average
    # RVOL averaged -0.02R per trade, above-average +0.08R). An
    # earlier version required select_focus_cohort to re-check live
    # RVOL on top of what the batch already filtered on - by request
    # ("it should just be scanning contracts by the momentum and
    # entering"), that was removed after it stalled the live account
    # for 20+ minutes: volume_delta resets to empty on every restart,
    # so the gate could sit unsatisfied on a perfectly good candidate
    # for a long stretch. Momentum belongs at ENTRY time instead,
    # where pressure_supports_entry/rsi_divergence/the direction
    # signal already gate it against a live contract quote rather
    # than a value that just reset to zero - see
    # _evaluate_option_entry.
    #
    # Price band for the focus symbol. This DELIBERATELY departs from
    # the usual gap-trading advice ($5-$50, low float under 20M shares
    # for maximum raw movement): that advice optimizes for trading the
    # SHARES. This bot trades the OPTIONS, and low-float small-caps
    # have thin, wide, or entirely absent option chains - the same
    # illiquidity that produced the ORCL repeated-stop-loss cycle
    # behind option_max_entry_spread_percent. Chain liquidity outranks
    # share-price volatility when the instrument is a contract.
    focus_min_price: Decimal = Field(default=Decimal("10"), gt=0)
    focus_max_price: Decimal = Field(default=Decimal("600"), gt=0)
    # NOTE: the share-volume floor and the spread ceiling deliberately
    # have no knobs of their own here - popular_stock_min_volume
    # (already 1_000_000, the conventional active-trading minimum, and
    # a decent option-chain liquidity proxy: a name nobody trades has a
    # chain nobody quotes) and popular_stock_max_spread_percent already
    # express exactly these limits, and a second near-duplicate pair
    # would just be two places to disagree.
    # By request: "once you hit a certain profit slow down." Measured
    # as (realized + unrealized) against the equity captured at the
    # first cycle of the day.
    #
    # Deliberately NOT the existing daily_loss_circuit_breaker, which
    # halts all trading and was turned off by earlier explicit request
    # ("we do not want the circuit breaker to stop all trading"). This
    # only stops OPENING new risk: exits, the profit-lock trail and the
    # EOD close all stay fully active. That matches the documented
    # failure mode - fixed daily targets push traders into low-quality
    # trades, and accounts typically die inside the final 1% of a
    # target from size increases and revenge trades.
    focus_daily_profit_target_fraction: Decimal = Field(
        default=Decimal("0.05"), gt=0, le=1
    )
    # How many CONSECUTIVE equity readings must clear the target
    # before the throttle actually arms.
    #
    # Live incident, first session this shipped: the bot recorded
    # equity of $409.54 (+12.52%) and armed the throttle four minutes
    # into the open, disabling new entries for the whole day - while
    # the broker showed $363 flat with zero open positions. The cause
    # is settlement timing, as described by the account owner: "when a
    # trade goes through for a second the calculations occur and the
    # account spikes, but that doesn't mean anything." For a moment
    # after a closing fill, the sale proceeds are ALREADY in
    # buying_power while the position is STILL present in the
    # positions list, so buying_power + market value double-counts it.
    #
    # A spike like that lasts a reading or two. Requiring several
    # consecutive confirmations (write_status_snapshot refreshes
    # equity about every 20s, so 3 readings is roughly a minute of
    # sustained gain) makes the throttle respond to real P&L only.
    # Any single reading below the target resets the streak.
    profit_throttle_confirm_readings: int = Field(default=3, ge=1, le=20)
    # By request: "make sure when there is a profit to not let on too
    # much loss" - don't let a winner round-trip into a loser.
    #
    # Nothing in this codebase trailed a stop off peak profit before
    # this (stop_loss_guard's "trailing" is a time window for counting
    # stop-outs, unrelated). Arms only once the position has a real
    # gain, then holds a floor that can never sit below cost+fee - so
    # when it fires it is always a genuine PROFIT exit, never a loss
    # dressed up as one.
    profit_lock_enabled: bool = True
    profit_lock_arm_percent: Decimal = Field(
        default=Decimal("0.10"), gt=0, le=10
    )
    # How much of the peak gain may be handed back before the lock
    # fires. 0.5 = give back at most half of what the position was up
    # at its best.
    profit_lock_giveback_fraction: Decimal = Field(
        default=Decimal("0.50"), gt=0, le=1
    )
    # Tighter leash once the daily profit throttle has armed - the
    # day is already good, so open winners are held more defensively.
    profit_lock_giveback_fraction_after_throttle: Decimal = Field(
        default=Decimal("0.25"), gt=0, le=1
    )
    # By request: "see the momentum by the buys and sells."
    #
    # HONEST CONSTRAINT, recorded here because it bounds what this can
    # be: update_volume_delta (vwap_trend.py) is UNSIGNED - it diffs
    # Webull's cumulative day-volume snapshot, so it measures
    # participation, not aggressor side. This API exposes no bid/ask-
    # tagged tape, so true buy-vs-sell volume is not obtainable. What
    # net_pressure builds instead is a proxy: DIRECTION from the price
    # change over the same interval, CONVICTION from volume_delta
    # against its own EMA baseline. Price up on a volume spike reads as
    # buyers in control; price down on a spike as sellers; any move on
    # thin volume reads near zero regardless of size.
    pressure_enabled: bool = True
    # How strongly one side must dominate before it can trigger an
    # entry - a call needs >= this, a put needs <= its negative.
    pressure_min_for_entry: Decimal = Field(
        default=Decimal("0.15"), ge=0, le=1
    )
    # Opposing pressure that reads as momentum exhaustion on an open
    # position, joining the existing rsi_divergence and resistance
    # checks as an exit trigger. Profit-protection only, same as those.
    pressure_flip_exit_enabled: bool = True
    pressure_flip_exit_threshold: Decimal = Field(
        default=Decimal("0.25"), ge=0, le=1
    )
    # Rolling window of pressure samples kept per symbol - long enough
    # to see a flip, short enough that stale readings never drive a
    # live decision.
    pressure_history_seconds: int = Field(default=300, ge=30, le=3600)
    # By request: "remember we will be trading the same stock as it
    # goes up and down through the day" AND "we want wash sale intact
    # as well." Those two pull against each other, and this is how
    # they're reconciled WITHOUT weakening the wash-sale tracker.
    #
    # The collision: every option STOP-LOSS exit calls wash_sales.block
    # (f"{underlying}:{option_type}") and wash_sale_block_days has a
    # floor of 31. In focus mode a stopped-out CALL locks CALLs on the
    # ONE symbol the account is committed to for a month; a stopped-out
    # PUT locks the other side. With both sides blocked and every other
    # underlying rejected by the focus gate, the bot would go silent
    # for the rest of the session.
    #
    # Resolution, in two parts, both of which LEAVE the 31-day block
    # untouched:
    #   1. select_focus_cohort skips any candidate whose CALL and PUT
    #      are both already wash-blocked - never commit the day to a
    #      name that cannot be traded in either direction.
    #   2. If the live focus symbol later becomes blocked on BOTH
    #      sides, re-pick a replacement from the batch instead of
    #      idling. This is the one sanctioned exception to the
    #      otherwise-deliberate "lock once, don't re-pick" rule: it
    #      fires only on genuine untradeability, never on a symbol
    #      merely going quiet.
    # Note the block only fires on STOP-LOSS exits - a session that
    # exits at a profit accrues no blocks at all.
    focus_repick_when_blocked: bool = True
    # Live incident ("grml has no contracts why is it in the batch" /
    # "if discovery failed why is it still on that stock"): GRML, a
    # 286.7% gapper with no listed option chain at all, locked as the
    # focus symbol and the account sat stuck on it - unable to trade
    # anything else, since focus mode rejects every other underlying
    # and suspends new stock entries - with ensure_focus_cohort_
    # contracts retrying forever. "No options chain" is a PERMANENT
    # condition for a symbol (it will not develop one later today),
    # so repeated discovery failure disqualifies it and triggers a
    # re-pick. Not 1, so a genuine transient API error doesn't falsely
    # burn a good symbol on its first hiccup.
    focus_contract_discovery_max_failures: int = Field(default=3, ge=1, le=20)
    # By explicit request ("i want this to be faster more high
    # frequency trades"): the general stock-scan batch defaults to
    # 100 symbols quoted every cycle (stock_batch_size), sized for
    # feeding the multi-symbol strategy that focus mode suspends.
    # Live evidence showed the option entry-timing signal refreshing
    # only every 40-90+ seconds despite a 0.25s poll interval - real
    # per-cycle wall-clock time, not signal logic, was the gate. Only
    # the locked focus symbol (guaranteed into every scan regardless
    # of this cap - see prioritized_stock_batch's force_include) and
    # a handful of daily-batch candidates need to stay fresh while
    # entries are suspended; this caps the batch to that instead of
    # paying for 100 quotes nothing can currently act on, freeing
    # real cycle time for the option pipeline to run again sooner.
    focus_mode_stock_batch_size: int = Field(default=20, ge=1, le=100)
