import logging
import time
from decimal import Decimal

log = logging.getLogger("webull-bot")


def close_fractional_positions_before_core_close(self) -> None:
    """Fractional orders only work during core hours - once core
    session ends, a fractional position can't be bought, sold,
    stopped out, or profit-taken at all until the next session opens.
    Unlike a whole-share position (which OVERNIGHT_HOLD_ENABLED lets
    ride deliberately, still exitable pre/after-hours if needed), a
    fractional position caught past this boundary has zero downside
    protection for the rest of the day/overnight - overnight_hold_
    symbols() doesn't know about quantity at all, so a fractional
    position in an otherwise overnight-eligible bucket (POPULAR/
    PENNY/DISCOVERY) would silently ride along with no way to defend
    it.

    Only closes the ones currently sitting at a profit - locking in a
    gain before it becomes undefendable is the whole point, but a
    loser isn't forced out just because the window is closing (it's
    already undefendable either way, and forcing a realized loss here
    isn't necessary the way capturing a gain is). Called from the same
    option_closeout-to-option_close window the option EOD closeout
    already uses.
    """
    now = time.monotonic()
    if now - self.last_fractional_sweep < self.config.eod_retry_seconds:
        return
    self.last_fractional_sweep = now
    try:
        positions = self.api.positions()
    except Exception as exc:
        log.error("CLOSE  | fractional pre-close sweep failed | %s", exc)
        return
    fractional_positions = [
        item
        for item in positions
        if item.get("instrument_type") == "EQUITY"
        and self.is_fractional_quantity(Decimal(str(item.get("quantity", "0"))))
    ]
    if not fractional_positions:
        return
    profitable_symbols: set[str] = set()
    for item in fractional_positions:
        symbol = str(item.get("symbol", "")).upper()
        cost = Decimal(str(item.get("cost_price") or "0"))
        if cost <= 0:
            continue
        try:
            price = self.api.quote_price(self.api.stock_quote(symbol))
        except Exception as exc:
            log.warning(
                "CLOSE  | fractional pre-close sweep | %-8s | quote "
                "failed, skipping this cycle | %s",
                symbol,
                exc,
            )
            continue
        if price > cost:
            profitable_symbols.add(symbol)
    if not profitable_symbols:
        return
    exclude_symbols = {
        str(item.get("symbol", "")).upper()
        for item in positions
        if item.get("instrument_type") == "EQUITY"
    } - profitable_symbols
    try:
        submitted = self.api.close_all_positions(
            {"EQUITY"},
            loss_callback=self.wash_sales.block,
            exclude_symbols=exclude_symbols,
        )
    except Exception as exc:
        log.error("CLOSE  | fractional pre-close sweep failed | %s", exc)
        return
    self.pending_stock_exits -= profitable_symbols
    log.info(
        "CLOSE  | fractional pre-core-close sweep | submitted=%s | %s",
        len(submitted),
        ",".join(sorted(profitable_symbols)),
    )
