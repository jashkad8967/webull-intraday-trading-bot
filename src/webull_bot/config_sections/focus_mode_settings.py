from decimal import Decimal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class FocusModeSettings(BaseSettings):
    """Single-symbol "focus mode": the once-daily researched candidate
    batch, the one focus symbol picked out of it, the net buy/sell
    pressure read, the profit-lock trailing stop, and the daily
    profit throttle."""

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
    # once a focus symbol is locked, option entries on every other
    # underlying are rejected and new stock entries are suspended, so
    # the whole account is available to the one name that earned it.
    focus_mode_enabled: bool = True
    # By request: "only take a good batch of stocks to look out for
    # everyday." Two-stage funnel - research a small batch each morning
    # (this), then pick the single focus symbol out of it at the open.
    #
    # Sized from day-trading practice rather than guessed: a daily
    # momentum watchlist that changes every day should run 5-10 names
    # (on top of a stable core list of 10-15 liquid ones), and past ~50
    # the documented result is analysis paralysis and poor execution.
    # 8 sits in the middle of that 5-10 band.
    daily_batch_size: int = Field(default=8, ge=1, le=50)
    # 08:45 ET, inside the 08:30-09:30 window where pre-market volume
    # and catalyst releases (earnings, guidance, FDA, upgrades) actually
    # cluster - early enough to have a batch ready before the bell,
    # late enough that the pre-market tape is meaningful.
    daily_batch_refresh_time: str = "08:45"
    # Minimum overnight gap for a name to make the batch. A gap is the
    # cheapest available proxy for "something happened" on an API that
    # exposes no news feed - a stock gapping on real volume is gapping
    # BECAUSE of a catalyst, which is the signal the batch is trying to
    # capture.
    daily_batch_min_gap_percent: Decimal = Field(
        default=Decimal("2"), gt=0, le=100
    )
    # 09:45 ET: after the opening range resolves. The first hour has
    # the best setups, but 09:30-09:45 is also where opening-range
    # fakeouts concentrate - committing the entire account at the bell
    # on pre-market ranking alone is exactly the trap this avoids.
    # Pre-market ranking frequently does not survive the open, so the
    # batch is re-measured on regular-session data before the pick.
    focus_lock_time: str = "09:45"
    # Relative volume floor, applied to both the batch and the focus
    # pick. The evidence here is unusually clean: below-average RVOL
    # averaged -0.02R per trade while above-average averaged +0.08R -
    # i.e. trading a name that isn't unusually active is a negative-
    # expectancy activity before any strategy is applied. Conventional
    # day-trading practice puts the useful band at 2x-5x.
    focus_min_rvol: Decimal = Field(default=Decimal("2.0"), gt=0, le=50)
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
    #   1. select_focus_symbol skips any candidate whose CALL and PUT
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
