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
            # OptionContractsStateStore.
            self.option_contracts_state.save(
                self.option_contracts, self.option_discovery_attempted
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
