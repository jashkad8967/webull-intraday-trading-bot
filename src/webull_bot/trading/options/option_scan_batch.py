import logging
from collections import defaultdict
from datetime import date
from decimal import Decimal

from webull_bot.strategy import OPTION_VIXY_SYMBOL
from webull_bot.webull_api import MarketDataPermissionError

log = logging.getLogger("webull-bot")


def _prepare_option_scan_batch(self, positions: list[dict]):
    """Builds this cycle's option-contract scan batch: refreshes the
    per-underlying direction signal from a fresh underlying quote batch,
    then picks the contracts to actually fetch option quotes for this
    cycle (see the priority/rotation comments below), and fetches those
    option quotes.

    Returns (open_count, guard_active, directions, batch, quote_by_symbol,
    today, current_vixy) on success, or None if this cycle's option scan
    batch could not be built at all - callers should treat None the same
    as the "return buying_power unchanged" cases it replaces.
    """
    # By request ("why is UBER not averaging down") - live incident:
    # discover_option_contracts treats an underlying as "already
    # discovered" the moment ANY contract for it exists in self.
    # option_contracts (see its own `discovered` set), so a restart
    # that re-discovers a DIFFERENT strike for that underlying (the
    # underlying's price moved between the original entry and the
    # restart) never adds the originally-HELD strike back. Since
    # trade_options' whole per-contract loop only ever iterates
    # self.option_contracts, a held position whose exact contract
    # fell out of that list was completely invisible to it from then
    # on - no PROFIT/LOSS check, no averaging-down, nothing, just an
    # unmanaged position sitting there. Backfills any open OPTION
    # position's contract that isn't currently in self.option_
    # contracts using contract_from_position (already used by the
    # manual-sell/stall-boost paths for the same reason), every
    # cycle - cheap, since it only touches the handful of positions
    # actually held, not the whole discovery candidate pool.
    known_symbols = {item["symbol"] for item in self.option_contracts}
    for position in positions:
        if position.get("instrument_type") != "OPTION":
            continue
        symbol = str(position.get("symbol", ""))
        if not symbol or symbol in known_symbols:
            continue
        try:
            contract = self.api.contract_from_position(position)
        except Exception as exc:
            contract = None
            log.warning(
                "OPTIONS | %-8s | held-contract backfill raised | %s",
                symbol, exc,
            )
        if contract is None:
            # contract_from_position itself swallows exact_option's
            # own exception internally (falls through to a legs-
            # based lookup instead), so a bare None here gives no
            # detail on which of its two paths actually failed -
            # logged anyway so a persistently-unbackfilled position
            # is at least VISIBLE instead of silently never managed
            # again, the exact failure mode this whole backfill
            # exists to fix.
            log.warning(
                "OPTIONS | %-8s | held-contract backfill found nothing "
                "(neither exact_option nor a legs-based match)",
                symbol,
            )
        if contract is not None:
            self.option_contracts.append(contract)
            known_symbols.add(symbol)
            log.info(
                "OPTIONS | %-8s | re-added held contract missing from "
                "discovery | %s",
                contract.get("underlying_symbol", symbol),
                symbol,
            )
    open_count = self.strategy.open_position_count(positions)
    # See stop_loss_guard_active() / trade_stocks - same freqtrade-
    # style frequency-based entry pause, applied here too.
    guard_active = self.stop_loss_guard_active()
    # A fresh underlying quote per cycle, decoupled from whatever batch
    # the stock-scanning path happens to be covering this cycle - the
    # direction signal must never silently run on stale/absent state.
    # VIXY rides along in the same batched call (real VIX/CGIF index
    # data isn't reachable through the OpenAPI - confirmed live) to
    # track a market-wide volatility regime gate every cycle.
    #
    # By request ("still isn't buying options"): deliberately reads
    # from self.option_contracts (every discovered contract), NOT
    # the option-QUOTE rotation (hard-capped at 20 by Webull's own
    # per-call option-snapshot limit). Live incident: with 106
    # contracts discovered (~53 underlyings), option_direction_
    # signal's EMA(3/8) needs 9 price samples per underlying, but
    # each underlying was only getting ONE new sample every ~5
    # cycles (20 contracts / 2-per-underlying = 10 underlyings
    # covered per rotation) - meaning ~45+ cycles before ANY
    # underlying could even show a crossover, and that number only
    # gets worse as discovery finds more contracts. Stock quotes
    # have a much higher per-call batch limit than option quotes
    # (stock_quotes_resilient already chunks internally), so
    # there's no reason to starve direction-signal history at the
    # option-quote batch's much tighter pace - every known
    # underlying now gets a fresh sample every single cycle,
    # completely decoupled from the option-contract quote rotation.
    underlyings = sorted(
        {contract["underlying_symbol"] for contract in self.option_contracts}
    )
    quote_symbols = sorted(set(underlyings) | {OPTION_VIXY_SYMBOL})
    underlying_quote_by_symbol: dict[str, dict] = {}
    current_vixy: Decimal | None = None
    try:
        fetched_quotes, _ = self.api.stock_quotes_resilient(
            quote_symbols, "US_STOCK"
        )
        for fetched_quote in fetched_quotes:
            symbol = str(fetched_quote.get("symbol", "")).upper()
            if symbol == OPTION_VIXY_SYMBOL:
                current_vixy = self.api.quote_price(fetched_quote)
                self.vixy_history.append(current_vixy)
            else:
                underlying_quote_by_symbol[symbol] = fetched_quote
    except Exception as exc:
        log.warning(
            "OPTIONS | underlying quote batch failed | new entries "
            "skipped this cycle, exits unaffected | %s", exc,
        )
    directions: dict[str, str] = {}
    for underlying, underlying_quote in underlying_quote_by_symbol.items():
        underlying_price = self.api.quote_price(underlying_quote)
        directions[underlying] = self.strategy.option_direction_signal(
            f"OPTU:{underlying}", underlying_price
        )
    # By request ("watch... why didn't you let me know"): direct
    # visibility into whether the direction-signal pipeline is
    # actually alive, instead of inferring health from the absence
    # of an order - a real CALL/PUT crossover being genuinely rare
    # on calm blue-chip names looks IDENTICAL, from the outside, to
    # something silently broken (a quote batch quietly returning
    # too few symbols, a history dict never actually accumulating).
    # Logged once/cycle regardless of outcome, same "state, not
    # just events" visibility SCAN/GATES already give the stock side.
    signal_counts: dict[str, int] = defaultdict(int)
    for value in directions.values():
        signal_counts[value] += 1
    log.info(
        "OPTIONS | direction signals | quoted=%s/%s | CALL=%s PUT=%s HOLD=%s",
        len(underlying_quote_by_symbol),
        len(underlyings),
        signal_counts.get("CALL", 0),
        signal_counts.get("PUT", 0),
        signal_counts.get("HOLD", 0),
    )
    # Live incident (this bug): the fix above made direction signals
    # fire constantly (CALL/PUT counts logged nonzero repeatedly),
    # yet option_gate_rejections NEVER recorded a single rejection
    # past "no direction signal for this underlying" - because the
    # per-CONTRACT gate-check loop below only ever evaluated a
    # blind round-robin rotation of option_batch_size (20) out of
    # the full, continuously-growing option_contracts list (136+
    # and climbing while discovery is still running). A contract
    # whose underlying briefly signals CALL/PUT this cycle has no
    # guarantee of being IN that cycle's 20-wide rotation - by the
    # time round-robin reaches it again, the fast EMA(3/8) signal
    # has often already reverted to HOLD. Signals were real; they
    # just almost never lined up with the narrow gate-check window.
    # Fix: put every contract whose underlying has a LIVE, matching
    # signal into this cycle's batch first (capped at option_batch_
    # size, since option_quotes hard-rejects a request over 20
    # symbols), then fill any remaining room from the normal
    # rotating cursor so non-signaling contracts still get their IV
    # history refreshed and stay in exit-management coverage.
    priority_contracts = [
        contract
        for contract in self.option_contracts
        if directions.get(contract["underlying_symbol"]) == contract.get(
            "option_type"
        )
    ][: self.config.option_batch_size]
    fill_size = max(
        0, self.config.option_batch_size - len(priority_contracts)
    )
    rotation, self.option_cursor = self.strategy.rotating_batch(
        self.option_contracts, self.option_cursor, fill_size
    )
    # By request ("why is UBER not averaging down") - being IN self.
    # option_contracts (see the backfill above) isn't enough on its
    # own: this batch is still capped at option_batch_size (20,
    # Webull's own per-call option-snapshot limit) and shared with
    # the direction-signal-priority/rotation contracts above, so an
    # actively-HELD position could still lose out to the rotation on
    # any given cycle purely by bad luck, leaving its PROFIT/LOSS/
    # averaging-down check skipped that cycle. A position that's
    # actively risking real capital must never be skippable just
    # because it didn't win this cycle's rotation slot - listed
    # first, ahead of both, so it only ever gets bumped out of the
    # 20-wide cap by having more than 20 open option positions at
    # once (a real ceiling, not this bug).
    held_symbols = {
        str(position.get("symbol", ""))
        for position in positions
        if position.get("instrument_type") == "OPTION"
    }
    held_contracts = [
        contract
        for contract in self.option_contracts
        if contract["symbol"] in held_symbols
    ]
    seen_symbols: set[str] = set()
    batch: list[dict] = []
    for contract in held_contracts + priority_contracts + rotation:
        symbol = contract["symbol"]
        if symbol not in seen_symbols:
            seen_symbols.add(symbol)
            batch.append(contract)
    batch = batch[: self.config.option_batch_size]
    try:
        quotes = self.api.option_quotes(
            [contract["symbol"] for contract in batch]
        )
    except Exception as exc:
        if isinstance(exc, MarketDataPermissionError):
            self.options_enabled = False
            log.warning(
                "OPTIONS | disabled | OPRA OpenAPI quotes not subscribed"
            )
            return None
        log.error("OPTIONS | quote batch failed | %s", exc)
        return None
    quote_by_symbol = {
        str(quote.get("symbol", "")).upper(): quote for quote in quotes
    }
    today = date.today()
    return open_count, guard_active, directions, batch, quote_by_symbol, today, current_vixy
