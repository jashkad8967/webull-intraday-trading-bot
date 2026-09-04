import logging
import time

log = logging.getLogger("webull-bot")


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
    # like in snp and dow, and some from nyse." Computed fresh from
    # config here (not read off self.stock_symbols) - self.
    # stock_symbols is legitimately mutated by MULTIPLE sources for
    # the general STOCK strategy's own purposes (pre-market gainers,
    # agent-predicted gainers, reinstated watchlist symbols), and a
    # background thread (resolve_targets) also reassigns it
    # concurrently with those. A live incident (this bug, caught
    # right after an earlier fix attempt): a one-time snapshot of
    # self.stock_symbols taken inside _resolve_targets_work_body
    # still leaked AIDX (a pre-market-gainer symbol, nowhere near
    # the curated list) into discovery, because the main thread's
    # refresh_premarket_gainers mutation landed in the race window
    # before the snapshot was taken - a genuine, unsynchronized
    # cross-thread race, not a logic bug in the filtering itself.
    # config.stocks() is a pure function of static config (just a
    # string split) - computing it fresh every call is cheap (at
    # most ~500 items) and immune to that race entirely, since it
    # never reads any thread-shared mutable state.
    requested_stocks = self.config.stocks()
    if requested_stocks == ["ALL"]:
        candidates = self.stock_symbols
    else:
        candidates = [
            symbol
            for symbol in requested_stocks
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
