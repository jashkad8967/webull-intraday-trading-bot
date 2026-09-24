import logging
import time
from datetime import date, timedelta
from decimal import Decimal

log = logging.getLogger("webull-bot")


def _wide_focus_contracts(self, underlying: str, stock_price: Decimal) -> list[dict]:
    """By explicit request, after a live incident where the focus
    symbol only ever had ONE call and ONE put discovered all session:
    "it should have found a lot more... at each price tick for
    different dates... regardless of affordability" and "my
    requirements were at least 2 weeks out, not only 2 weeks out."

    select_atm_options (the old discovery path here) deliberately
    narrows to a single best-guess CALL and PUT per underlying, driven
    by affordability AT DISCOVERY TIME - by its own docstring, when
    nothing near-ATM fits the budget it falls back to "the cheapest
    one quoted" rather than surfacing the rest of the board.
    option_min_dte (14 days) is a FLOOR, not a target - the real
    search window is the full [option_min_dte, option_max_dte] range
    (14-45 days by default), every valid strike within the existing
    moneyness cap.

    This returns ALL of them - regardless of affordability, which is
    correctly decided per-contract at ENTRY time (option_order_
    quantity) or by the proactive/reactive affordability checks below,
    not by narrowing what gets discovered down to one guess. A
    broader set gives trade_options' per-contract loop many more
    chances each cycle to find a strike/expiration combination that
    is both directionally right and actually affordable.

    Reuses the exact same moneyness cap, DTE window, tradable-status
    and symbol-prefix-sanity filters select_atm_options applies (see
    its own docstring for why each exists) - only the "narrow to one
    per type" step is removed.
    """
    minimum = date.today() + timedelta(days=self.config.option_min_dte)
    maximum = date.today() + timedelta(days=self.config.option_max_dte)
    option_types = (
        ("CALL", "PUT")
        if self.config.option_type == "BOTH"
        else (self.config.option_type,)
    )
    moneyness_cap = stock_price * self.config.option_max_moneyness_percent
    found = []
    for item in self.api.option_contracts(underlying=underlying):
        if not str(item.get("symbol", "")).startswith(underlying):
            continue
        if item.get("option_type") not in option_types:
            continue
        if item.get("tradable_status") != "OC":
            continue
        try:
            expiration = date.fromisoformat(item["expiration_date"])
        except (KeyError, ValueError):
            continue
        if not (minimum <= expiration <= maximum):
            continue
        try:
            strike = Decimal(str(item["strike_price"]))
        except (KeyError, TypeError, ValueError):
            continue
        if abs(strike - stock_price) > moneyness_cap:
            continue
        found.append(item)
    found.sort(
        key=lambda item: (
            date.fromisoformat(item["expiration_date"]),
            abs(Decimal(str(item["strike_price"])) - stock_price),
        )
    )
    return found


def _cheapest_affordable(
    self, contracts: list[dict], buying_power
) -> tuple[bool, Decimal | None]:
    """Shared by the proactive (pre-lock) and reactive (post-lock)
    affordability checks below. Quotes up to 40 contracts (2 Webull
    batches of 20) - the list passed in is already sorted nearest-
    expiration/nearest-ATM first by _wide_focus_contracts, so this
    samples the most realistic candidates rather than an arbitrary
    slice. Returns (True, price) for the first contract that sizes to
    at least 1 contract at the given buying power, else (False,
    cheapest price seen) for diagnostics.

    "Affordable" here means affordable AND actually enterable. Live
    2026-09-23: a 10-name cohort locked, 8 of those names produced
    ZERO entries all session, and the gate counters showed "sizing
    produced zero contracts=9" and "delta out of range=9" every cycle
    - the same wall from two sides. Probing all 6717 discovered
    contracts against the real gates showed why. On a $247 account
    the per-entry cap is $98.92, i.e. $0.99 of premium; AAPL's
    cheapest contract inside the delta window cost $2.00, NVDA's and
    AMZN's $1.85, PLTR's $2.85, AVGO's $3.10, MRNA's $5.40. The
    contracts those names DID have under $0.99 carried delta 0.08-
    0.16 - far-OTM lottery tickets that option_delta_ok correctly
    rejects. So each name passed this check on a contract that could
    never clear entry, locked into the cohort, and then died at the
    delta gate on every single attempt.

    Checking price alone answers "can the account buy something",
    which is not the question. The question is "can the account buy
    something it is allowed to enter", so this applies the same
    option_delta_ok filter the entry path applies. Names whose only
    affordable strikes are lottery tickets are now correctly reported
    unaffordable, and the cohort fills with names that can actually
    trade instead of names that merely quote.
    """
    if not contracts:
        return False, None
    symbols = [item["symbol"] for item in contracts[:40]]
    quotes: dict[str, dict] = {}
    for start in range(0, len(symbols), 20):
        chunk = symbols[start : start + 20]
        try:
            for row in self.api.option_quotes(chunk):
                quotes[str(row.get("symbol", ""))] = row
        except Exception:
            continue
    cheapest = None
    for contract in contracts[:40]:
        quote = quotes.get(contract["symbol"])
        if quote is None:
            continue
        try:
            limit_price = self.api.option_limit_price(quote, "BUY")
        except Exception:
            continue
        if limit_price is None:
            continue
        if not self.strategy.option_delta_ok(self.api.option_delta(quote)):
            continue
        if cheapest is None or limit_price < cheapest:
            cheapest = limit_price
        quantity, _ = self.strategy.option_order_quantity(limit_price, buying_power)
        if quantity >= 1:
            return True, limit_price
    return False, cheapest


def focus_symbol_is_affordable(self, symbol: str) -> bool:
    """By explicit request: "we want affordable options only so the
    stocks should also be focused like that" - checked proactively,
    inside select_focus_cohort's candidate loop, so an established/
    liquid name whose cheapest realistic contract still exceeds
    buying power never locks in the first place (the reactive check
    below is the backstop for buying power changing mid-session, not
    the primary defense - locking then immediately re-picking wastes
    real trading time, confirmed live with GOOGL).

    By explicit request ("its options should all be discovered and
    analyzed [by 9:45]... I want the first order to go out at 9:45,
    not later"): the wide contract set this fetches to answer the
    affordability question is now PERSISTED into self.option_contracts
    for every candidate checked, not just the eventual winner. Two
    effects: every daily-batch candidate's options are genuinely
    discovered and analyzed before the lock, not only the one that
    wins; and whichever one DOES win already has its chain sitting in
    self.option_contracts the instant it locks, so ensure_focus_
    symbol_contracts' post-lock discovery becomes a same-cycle no-op
    instead of a fresh, separate discovery call adding its own delay
    right when the account most needs to start trading.

    Fails OPEN (True) on missing price/quote data or an API hiccup -
    same "no data, don't block" convention as every other gate in
    this codebase; a transient failure here should not eliminate a
    candidate that may well be perfectly affordable.
    """
    price = self.strategy.prices.get(symbol)
    if price is None or price <= 0:
        return True
    # select_focus_cohort retries every cycle until something locks -
    # once a candidate's chain is already discovered and persisted
    # (this call, or a prior retry), re-fetching it from the API every
    # single cycle until lock is pure repeated cost. Re-check
    # affordability freshly each time (buying power can shift) but
    # only ever do the real discovery fetch once per candidate.
    #
    # Keyed on focus_wide_discovered, NOT on "does this underlying have
    # any contracts at all". Live 2026-09-22: discover_option_contracts'
    # slow background rotation leaves exactly 2 contracts per name (one
    # CALL, one PUT, a single strike - see select_atm_options), and an
    # any-contracts test treats that as a discovered chain. The cohort
    # locked GME/TSLA/COIN/PLTR with 2 contracts each while NVDA - which
    # happened to have none when it was first evaluated, so it took the
    # branch below - had 180. Affordability was then decided off one ATM
    # strike, the most expensive point on the board, when cheaper OTM
    # strikes inside the moneyness cap were available and never fetched.
    if symbol in self.focus_wide_discovered:
        existing = [
            item
            for item in self.option_contracts
            if item["underlying_symbol"] == symbol
        ]
        if existing:
            buying_power = self.cached_option_buying_power or 0
            affordable, _ = _cheapest_affordable(self, existing, buying_power)
            return affordable
    try:
        contracts = _wide_focus_contracts(self, symbol, price)
    except Exception:
        return True
    if not contracts:
        return True
    self.focus_wide_discovered.add(symbol)
    already_known = {item["symbol"] for item in self.option_contracts}
    new_contracts = [c for c in contracts if c["symbol"] not in already_known]
    if new_contracts:
        self.option_contracts.extend(new_contracts)
        self.option_discovery_attempted.add(symbol)
        self.option_contracts_state.save(
            self.option_contracts,
            self.option_discovery_attempted,
            {
                sym: {
                    "count": count,
                    "last_buy_price": self.option_last_buy_price[sym],
                }
                for sym, count in self.option_average_down_count.items()
                if count > 0 and sym in self.option_last_buy_price
            },
        )
        log.info(
            "OPTIONS | %s | candidate contracts discovered | found=%s "
            "across %s expiration(s)",
            symbol,
            len(new_contracts),
            len({c["expiration_date"] for c in new_contracts}),
        )
    buying_power = self.cached_option_buying_power or 0
    affordable, _ = _cheapest_affordable(self, contracts, buying_power)
    return affordable


def _prune_stale_contracts(self) -> None:
    """Live incident ("we both know something is not working here"):
    NFLX locked, direction signals fired repeatedly, nothing ever
    entered. Direct inspection of persisted state found the cause -
    NFLX's already-discovered contracts (from a prior session,
    persisted across restarts via OptionContractsStateStore) expired
    2026-09-25, four days out, far inside the 14-day floor. Nothing
    ever re-validated a persisted contract was still within the
    tradeable DTE window before treating it as real - so a stale
    contract from days ago sat in self.option_contracts forever,
    silently rejected every cycle by _evaluate_option_entry's own DTE
    gate as "too close to expiration." This wasn't scoped to the
    focus symbol alone - the same staleness affects every underlying
    ever discovered, which is why that exact rejection reason has
    been the dominant, near-constant noise in every gate-rejection
    summary logged today regardless of which symbol was locked.

    Throttled to once per option_discovery_seconds (the same cadence
    discover_option_contracts already uses) - this is a full scan of
    self.option_contracts, not worth repeating every single cycle.

    last_stale_contract_prune's "never run yet" sentinel is None, not
    0.0 - time.monotonic() is seconds since an arbitrary epoch
    (typically system/process boot), not wall-clock time, so it can
    genuinely be small on a machine with low uptime (a fresh CI
    runner, or this bot's own deploy host right after a restart -
    which happened several times the same day this bug was found).
    Comparing against a hardcoded 0.0 would then read as "already
    pruned moments ago" and skip pruning entirely on the very first
    call - caught by a CI run that failed where the same test passed
    locally on a longer-uptime machine.
    """
    now = time.monotonic()
    if (
        self.last_stale_contract_prune is not None
        and now - self.last_stale_contract_prune
        < float(self.config.option_discovery_seconds)
    ):
        return
    self.last_stale_contract_prune = now
    today = date.today()
    live = []
    dropped = 0
    for item in self.option_contracts:
        try:
            expiration = date.fromisoformat(item["expiration_date"])
        except (KeyError, ValueError):
            live.append(item)
            continue
        if (expiration - today).days > self.config.option_min_hold_dte:
            live.append(item)
        else:
            dropped += 1
    if dropped:
        log.warning(
            "OPTIONS | dropped %s stale (too-close-to-expiration) "
            "contract(s) from prior sessions",
            dropped,
        )
        self.option_contracts = live
        # Persist immediately - otherwise the next restart reloads
        # the same stale contracts from disk and this whole prune
        # has to rediscover it live all over again.
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


def ensure_focus_cohort_contracts(self) -> None:
    """By request ("you should be able to request contract by stock
    in webull openapi"): the moment the cohort locks, the account is
    committed to trading those names, so their option chains must
    exist NOW rather than depending on discover_option_contracts'
    generic rotation eventually reaching them.

    That rotation cannot be relied on here for two independent
    reasons: (1) its candidate pool is config.option_candidates()
    union agent_popular_symbols union agent_predicted_gainers, which
    is NOT the same set select_focus_cohort picks from (the daily
    batch draws from premarket_gainers/seed_popular_symbols too) - a
    locked symbol may simply never be in that pool; (2) even if it
    is, option_discovery_attempted is permanent per session (and
    persisted across restarts) - one prior attempt with zero results
    (common; not every name has a listed chain, or didn't clear a
    filter that day) silently forecloses it forever, with no
    awareness that the symbol has since become one of the few things
    the account is allowed to trade.

    Uses _wide_focus_contracts (every valid strike/expiration in the
    configured window, not select_atm_options' single best-guess CALL
    and PUT) - see that function's docstring for why. A cheap no-op
    once a chain already exists (checked first) or on a symbol with
    no price yet (retries next cycle, same as everything else in
    focus mode). Prunes stale (too-close-to-expiration) contracts
    first - see _prune_stale_contracts - so "already exists" means a
    genuinely tradeable chain, not a leftover from a prior session.

    Failures are tracked and disqualified PER SYMBOL: one cohort
    member with no listed chain must not consume the retry budget of
    the others, and removing it leaves the rest of the cohort trading.
    """
    if not self.config.focus_mode_enabled or not self.focus_cohort:
        return
    _prune_stale_contracts(self)
    # Chain discovery is bounded per cycle, for the same reason
    # select_focus_cohort bounds it: _wide_focus_contracts takes ~30
    # SECONDS per underlying, and this runs inline in the main loop.
    # Live 2026-09-23 with a 10-name cohort that meant up to 5 minutes
    # inside a single cycle - consecutive SCAN lines went 08:56:53 ->
    # 09:02:25 - which is catastrophic for everything downstream that
    # is measured per cycle.
    #
    # Already-discovered members are still visited every cycle: that
    # path only re-checks affordability and costs nothing. Only NEW
    # discovery is rationed, so a freshly locked cohort fills in over
    # the next minute or two instead of stalling the loop outright.
    budget = self.config.focus_lock_discovery_per_pass
    for underlying in list(self.focus_cohort):
        if underlying not in self.focus_wide_discovered:
            if budget <= 0:
                continue
            budget -= 1
        _ensure_one_focus_symbol_contracts(self, underlying)


def _ensure_one_focus_symbol_contracts(self, underlying: str) -> None:
    # Gated on focus_wide_discovered rather than "has any contracts",
    # for the reason spelled out in focus_symbol_is_affordable: the
    # background rotation leaves 2 contracts per name, and treating
    # that as a discovered chain meant a cohort member kept a single
    # ATM strike all session instead of the full board.
    if underlying in self.focus_wide_discovered:
        self.focus_contract_discovery_failures.pop(underlying, None)
        _check_focus_symbol_affordable(self, underlying)
        return
    if underlying not in self.strategy.prices:
        return
    try:
        contracts = _wide_focus_contracts(
            self, underlying, self.strategy.prices[underlying]
        )
        if not contracts:
            raise RuntimeError(f"No matching options found for {underlying}")
        self.option_contracts.extend(contracts)
        self.option_discovery_attempted.add(underlying)
        self.focus_wide_discovered.add(underlying)
        self.focus_contract_discovery_failures.pop(underlying, None)
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
        log.info(
            "OPTIONS | %s | cohort contracts discovered | found=%s "
            "across %s expiration(s)",
            underlying,
            len(contracts),
            len({c["expiration_date"] for c in contracts}),
        )
        _check_focus_symbol_affordable(self, underlying)
    except Exception as exc:
        # By request ("if discovery failed why is it still on that
        # stock") - live incident: GRML (a 286.7% gapper with no
        # listed option chain at all) locked as the focus symbol and
        # the account sat stuck on it, retrying forever, unable to
        # trade anything else for the rest of the session (focus mode
        # rejects every other underlying and suspends new stock
        # entries). "No options chain" is a PERMANENT condition for a
        # symbol - it will not develop one later today - so repeated
        # failure is disqualifying, not a transient blip to keep
        # waiting out. A small streak (not 1) still absorbs a genuine
        # transient API error without falsely burning a good symbol.
        # Counted per symbol so one dead name cannot spend the retry
        # budget of the rest of the cohort.
        self.focus_contract_discovery_failures[underlying] += 1
        failures = self.focus_contract_discovery_failures[underlying]
        log.error(
            "OPTIONS | %s | cohort contract discovery failed "
            "(%s/%s) | %s",
            underlying,
            failures,
            self.config.focus_contract_discovery_max_failures,
            exc,
        )
        if failures >= self.config.focus_contract_discovery_max_failures:
            log.warning(
                "OPTIONS | %s | no discoverable option chain after %s "
                "attempts - dropping it from today's cohort",
                underlying,
                failures,
            )
            _disqualify_from_cohort(self, underlying)
            self.focus_contract_discovery_failures.pop(underlying, None)


def _disqualify_from_cohort(self, underlying: str) -> None:
    """Remove one name from today's cohort permanently.

    The rest of the cohort keeps trading - that is the whole point of
    holding more than one name. select_focus_cohort backfills from the
    daily batch on its next pass, and focus_symbol_no_chain keeps this
    symbol out of that backfill for the remainder of the session.
    """
    self.focus_symbol_no_chain.add(underlying)
    self.focus_cohort = [
        symbol for symbol in self.focus_cohort if symbol != underlying
    ]


def _check_focus_symbol_affordable(self, underlying: str) -> None:
    """By explicit request, after a live incident: "it is your
    responsibility to make sure the stock was chosen correctly so
    that option trades go through throughout the day." GOOGL locked
    as the established, liquid focus symbol and NOTHING traded all
    session - confirmed live by pulling its actual quote directly:
    bid $7.55/ask $7.90 (a genuinely tight, liquid market - open
    interest 711, real volume), but at ~$790/contract against a $363
    account, option_order_quantity silently sizes it to ZERO
    contracts every single cycle, regardless of how good direction/
    momentum look.

    This is now the BACKSTOP, not the primary defense -
    focus_symbol_is_affordable (above) checks this proactively before
    a symbol ever joins the cohort. This still matters because buying
    power can shrink mid-session (a realized loss, an averaging-down
    buy) after a symbol already qualified.

    Runs once per symbol (not every cycle - an extra option_quotes
    call each cycle for a symbol already confirmed affordable is pure
    waste) via focus_symbol_affordability_checked. If not even one
    discovered contract sizes to >=1 contract at current buying power,
    drops it from the cohort exactly like "no chain" - unaffordable
    today is just as untradeable as nonexistent.

    CRITICAL, and the reason for the open-position guard below: with a
    cohort, capital is first-come-first-served, so the moment one
    member's contract is bought the remaining buying power falls -
    often below what every OTHER member costs. Disqualifying on that
    reading would permanently delete the rest of the cohort as a
    side effect of successfully trading, collapsing it to nothing
    after the first fill. Deployed capital comes back when the
    position closes, so "unaffordable while money is at work" is
    transient and must not be treated as a verdict. Only a symbol that
    is unaffordable with NOTHING deployed is genuinely out of reach.
    """
    if underlying in self.focus_symbol_affordability_checked:
        return
    contracts = [
        item
        for item in self.option_contracts
        if item["underlying_symbol"] == underlying
    ]
    if not contracts:
        return
    buying_power = self.cached_option_buying_power or 0
    affordable, cheapest = _cheapest_affordable(self, contracts, buying_power)
    if affordable:
        self.focus_symbol_affordability_checked.add(underlying)
        return
    if any(
        position.get("instrument_type") == "OPTION"
        for position in (self.cached_positions or [])
    ) and _could_afford_when_flat(self, cheapest):
        # Capital is deployed, not missing. Deliberately NOT memoised
        # as checked, so this is re-evaluated for real once positions
        # close and the money comes back.
        #
        # Gated on _could_afford_when_flat because "deployed" is only
        # the right reading when the shortfall really is transient.
        # Live 2026-09-23: one SOFI contract was open, so this branch
        # shielded EVERY unaffordable cohort member from being
        # dropped - including AAPL, AVGO, MRNA and PLTR, whose
        # cheapest in-delta contracts ($2.00-$5.40) the account could
        # not have bought with the position closed and every dollar
        # free. They sat in the cohort taking up slots and rejecting
        # on every cycle. A name out of reach at FULL capital is out
        # of reach, position or no position.
        return
    self.focus_symbol_affordability_checked.add(underlying)
    # Reports the PER-ENTRY CAP, not raw buying power.
    #
    # It used to print "cheapest=$1.70 vs buying_power=$254.74", which
    # reads as a contradiction - $1.70 is obviously less than $254.74,
    # so the gate looks broken. The real bound is
    # buying_power * option_capital_fraction / 100, because a contract
    # costs premium x 100 and only that fraction of the account may go
    # into one entry. On 2026-09-24 that was $254.74 x 0.4 / 100 =
    # $1.01, which is what actually evicted AMZN, HOOD, PLTR, BABA,
    # AVGO and TSLA within 90 seconds of the cohort locking.
    #
    # Printing the number that is genuinely being compared makes the
    # constraint legible: at a glance you can see whether a name is
    # unreachable because the account is small or because
    # option_capital_fraction is set low.
    per_entry = buying_power * self.config.option_capital_fraction
    log.warning(
        "OPTIONS | %s | no affordable contract | cheapest=$%s vs "
        "max premium $%s (buying power $%s x cap %s / 100) | "
        "dropping it from today's cohort",
        underlying,
        cheapest,
        (per_entry / 100).quantize(Decimal("0.01")),
        buying_power,
        self.config.option_capital_fraction,
    )
    _disqualify_from_cohort(self, underlying)


def _could_afford_when_flat(self, cheapest: Decimal | None) -> bool:
    """Would this name be affordable with every position closed and
    the whole account free? Decides whether an unaffordable reading
    taken while capital is deployed is transient (wait for the money
    to come back) or structural (drop it now).

    Unknown account value or unknown price answers True - the caller
    then takes the conservative path and keeps the symbol, same as
    every other best-effort check here.
    """
    if cheapest is None:
        return True
    account_value = self.cached_account_value
    if account_value is None or account_value <= 0:
        return True
    ceiling = account_value * self.config.option_capital_fraction
    return cheapest * 100 <= ceiling


def discover_option_contracts(self) -> None:
    # Live incident: dispatching this onto its own background
    # thread (tried this session, immediately reverted) produced
    # zero discovery output at all for 12+ minutes straight - no
    # progress/found/error logs, while stock trading kept working
    # fine on the main thread. Strong signal of a thread-safety
    # issue with the shared API client under concurrent use
    # (main-loop trade_options calls + this new thread's own calls
    # hitting the same client at once), not safely diagnosable live
    # against real capital. Reverted to synchronous - the real fix
    # for "don't block the main loop for too long" is keeping
    # OPTION_DISCOVERY_PER_CYCLE modest (this file's own comment on
    # that field has the full story), not threading this call.
    # By request: "we want options for more popular stocks only
    # like in snp and dow, and some from nyse" / "make sure the
    # stocks selected for options are popular like snp500" / "look
    # at the top gainers, most volatile, similar criteria for
    # stocks, and then look the popular options contracts from
    # those." Draws from TWO sources: config.option_candidates() (a
    # curated, real, large-cap-heavy list - see its own comment for
    # why this isn't a literal S&P 500 enumeration) union self.
    # agent_popular_symbols (today's actual top-gainer/most-active
    # movers - refresh_agent_discoveries already builds this from
    # the same deterministic market_pulse screeners, so this adds no
    # new API calls). Neither is self.stock_symbols, which can be
    # the ENTIRE scanned universe (thousands of symbols, including
    # penny/micro-cap names, in STOCK_SYMBOLS=ALL mode).
    # option_candidates() is computed fresh from static config here
    # (not cached) for the same cross-thread-race-immunity reason
    # the old self.stock_symbols read had to be abandoned; agent_
    # popular_symbols is safe to read directly since discover_
    # option_contracts, like refresh_agent_discoveries, only ever
    # runs on the main thread (see run()) - never the separate
    # position-protection thread, so there's no cross-thread race on
    # it the way there was on self.stock_symbols.
    # By request: "perhaps the correct stocks are not being chosen,
    # maybe we could ask the research agent to provide stocks to do
    # analysis on" - agent_predicted_gainers (the AI research
    # agent's own daily pick list, from refresh_agent_predicted_
    # gainers) already gets merged into the STOCK universe, but
    # option discovery never drew from it - only the static curated
    # list and agent_popular_symbols (despite the name, that one is
    # the deterministic market_pulse screener data, not the AI
    # agent). Unioned in here so the AI agent's own volatile/volume
    # picks - the exact kind of name the rise/dip cycling behavior
    # needs - become option candidates too, not just stock ones.
    candidates = [
        symbol
        for symbol in (
            set(self.config.option_candidates())
            | self.agent_popular_symbols
            | self.agent_predicted_gainers
        )
        if symbol not in self.invalid_symbols
    ]
    if not self.options_enabled or not self.discover_all_options or not candidates:
        return
    if (
        time.monotonic() - self.last_option_discovery
        < float(self.config.option_discovery_seconds)
    ):
        return
    self.last_option_discovery = time.monotonic()
    # By request: "we want options with volume and volatility to be
    # chosen." Re-ranks the (already small, curated) candidate list
    # by volume + realized volatility every call, using data already
    # collected during normal scanning (self.strategy.metrics/
    # volatility_price_history) - no extra API calls, same "free"
    # convention select_volatility_scalp_symbols already uses for its
    # own volatility ranking. A symbol with no scan data yet (metrics/
    # volatility not populated) sorts last, not excluded - it still
    # gets examined once the cursor reaches it, just without a
    # priority boost until it's actually been scanned once.
    def _priority(symbol: str) -> tuple[float, float]:
        volume = float(self.strategy.metrics.get(symbol, {}).get("volume", 0) or 0)
        volatility = self.strategy.realized_volatility_percent(symbol)
        return (volume, float(volatility) if volatility is not None else 0.0)

    candidates = sorted(candidates, key=_priority, reverse=True)
    discovered = {item["underlying_symbol"] for item in self.option_contracts}
    attempts = 0
    examined = 0
    while (
        attempts < self.config.option_discovery_per_cycle
        and examined < len(candidates)
    ):
        underlying = candidates[self.option_discovery_cursor % len(candidates)]
        self.option_discovery_cursor = (
            self.option_discovery_cursor + 1
        ) % len(candidates)
        examined += 1
        if (
            underlying in self.option_discovery_attempted
            or underlying in discovered
            or underlying not in self.strategy.prices
        ):
            continue
        self.option_discovery_attempted.add(underlying)
        attempts += 1
        try:
            # By request: "look for cheaper options to buy in to" -
            # then clarified: "options do not have to necessarily
            # be cheap anymore, but just within buying power." The
            # first version used buying_power * OPTION_CAPITAL_
            # FRACTION (the much smaller RISK-per-trade cap
            # option_order_quantity applies at actual sizing time)
            # as the affordability ceiling here too - meaning even
            # once the account's real buying power grew, strike
            # selection was still capped at a tiny fraction of it,
            # biasing toward far-OTM/cheap contracts long after
            # that was necessary. select_atm_options already prefers
            # the CLOSEST-TO-ATM affordable strike, not the
            # cheapest one available - so raising this ceiling to
            # the real buying power (not the risk fraction) lets it
            # pick a strike as close to the money as the account can
            # actually afford, while option_order_quantity's own
            # risk_cap still separately bounds how much of that
            # buying power any one trade is allowed to risk.
            # Live incident: option buying power is a separate pool
            # from stock buying power (see account_state) - using
            # the stock-side figure here meant strike selection
            # could pick a contract the account's real option
            # buying power could never actually afford.
            max_contract_cost = (
                self.cached_option_buying_power
                if self.cached_option_buying_power
                else None
            )
            contracts = self.api.select_atm_options(
                underlying,
                self.strategy.prices[underlying],
                max_contract_cost=max_contract_cost,
            )
            self.option_contracts.extend(contracts)
            discovered.add(underlying)
            # By request: "is there a way to save these option
            # contracts" - persist after every real discovery so a
            # restart resumes from here instead of an empty list. See
            # OptionContractsStateStore. Also carries the current
            # averaging-down ladder state along (see that store's
            # docstring for why - "do a full on options sanity check"
            # found it was previously lost across a restart).
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
            log.info(
                "OPTIONS | %s | found=%s | progress=%s/%s",
                underlying,
                ",".join(contract["symbol"] for contract in contracts),
                len(self.option_discovery_attempted),
                len(candidates),
            )
        except Exception as exc:
            # By request, after observing discovery growth stay
            # slow with no visibility into why: lowered 100 -> 10 -
            # at the old threshold, a symbol with no listed options
            # chain (common - not every stock has one) silently
            # consumed its one attempt with zero log output, making
            # "genuinely no options available" indistinguishable
            # from "something is actually broken."
            if len(self.option_discovery_attempted) % 10 == 0:
                log.info(
                    "OPTIONS | progress=%s/%s | latest=%s | %s",
                    len(self.option_discovery_attempted),
                    len(candidates),
                    underlying,
                    exc,
                )
