import logging
import time
from datetime import date
from decimal import Decimal

log = logging.getLogger("webull-bot")


def evaluate_held_option_exits(self) -> None:
    """Run held-option exit management on the FAST loop instead of
    once per full universe scan.

    This is the single biggest reason a winner round-tripped into a
    loser. _evaluate_option_exit is what computes the profit target,
    the stop, the profit-lock trail and the stale exit, AND what
    records option_peak_price - and it was only ever called from
    trade_options, inside the slow scan. Measured live 2026-09-22,
    consecutive SCAN lines were 13:13:02, 13:17:50 and 13:24:33: the
    entire exit ladder was sampled roughly every five to seven
    minutes.

    A trail cannot protect a high it never observed. GME ran from
    $1.40 to $1.46 and back to $1.37 between cycles, so the peak that
    should have armed the trail was very likely never recorded at all.
    The user closed those positions by hand, correctly, and said so:
    "the only reason we had good profit was because I had to do some
    manual sells at the right time".

    The original design said as much - option_peak_price was specified
    as "updated in the fast _position_protection_loop (0.25s cadence)
    - peak tracking on the 30-90s main scan would miss the spike it
    exists to capture". It ended up on the slow path instead.

    Deliberately narrow, so this is safe to add to a live account:
    - Only positions that are actually HELD (quantity > 0).
    - Only contracts already resolvable from the position, so no new
      discovery work happens on the fast thread.
    - Skipped entirely when an exit order already exists for the key,
      or when the symbol is manually touched - identical guards to
      evaluate_held_stock_exits.
    - Reuses _evaluate_option_exit unchanged. Not a second pricing or
      decision path, just the existing one running when it matters.

    The slow loop still calls _evaluate_option_exit too. That is
    intentional and safe: every action it can take is already guarded
    by pending_option_exits / has_pending_sell_order, so whichever
    thread sees the condition first acts and the other finds the work
    done. Removing it from the slow loop would leave averaging-down
    and entry handling without a caller.
    """
    if not self.config.held_option_exit_enabled:
        return
    now = time.monotonic()
    if now - self.last_held_option_exit_scan < float(
        self.config.held_option_exit_seconds
    ):
        return
    self.last_held_option_exit_scan = now
    positions = self.cached_positions or []
    candidates: list[tuple[dict, dict]] = []
    for item in positions:
        if item.get("instrument_type") != "OPTION":
            continue
        try:
            quantity = Decimal(str(item.get("quantity", "0")))
        except Exception:
            continue
        if quantity <= 0:
            continue
        symbol = str(item.get("symbol", "")).upper()
        if not symbol:
            continue
        try:
            contract = self.api.contract_from_position(item)
        except Exception:
            continue
        if not contract:
            continue
        option_symbol = str(contract.get("symbol", ""))
        if not option_symbol:
            continue
        key = f"OPTION:{option_symbol}"
        if option_symbol in self.pending_option_exits:
            continue
        if self.has_pending_sell_order(key):
            continue
        candidates.append((item, contract))
    if not candidates:
        return
    symbols = [contract["symbol"] for _, contract in candidates][:20]
    try:
        rows = self.api.option_quotes(symbols)
    except Exception as exc:
        log.debug("PROTECT| held-option quote batch failed | %s", exc)
        return
    quote_by_symbol = {str(row.get("symbol", "")): row for row in rows}
    today = date.today()
    for item, contract in candidates:
        option_symbol = str(contract["symbol"])
        quote = quote_by_symbol.get(option_symbol)
        if quote is None:
            continue
        try:
            price = self.api.quote_price(quote)
        except Exception:
            price = None
        if price is None or price <= 0:
            continue
        try:
            quantity = Decimal(str(item.get("quantity", "0")))
            cost = Decimal(str(item.get("cost_price") or "0"))
            if cost <= 0:
                continue
            expiration = contract.get("expiration_date")
            days_to_expiration = (
                (date.fromisoformat(expiration) - today).days
                if expiration
                else 0
            )
            self._evaluate_option_exit(
                contract,
                option_symbol,
                f"OPTION:{option_symbol}",
                quote,
                price,
                quantity,
                cost,
                days_to_expiration,
                self.cached_option_buying_power or Decimal("0"),
            )
        except Exception as exc:
            log.error("PROTECT| held-option exit failed | %s | %s", option_symbol, exc)
