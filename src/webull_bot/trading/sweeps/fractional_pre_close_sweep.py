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
    # Cheap cached pre-check BEFORE spending a live positions() call.
    #
    # Live 2026-10-01 14:50, on a FLAT account: this swept every
    # eod_retry_seconds and logged
    #
    #   CLOSE | submitted=0 | remaining=0
    #   CLOSE | fractional pre-close sweep failed | HTTP 429 TOO_MANY_REQUESTS
    #
    # four times in 75 seconds and would have continued to the close.
    # The early return for "no fractional positions" sat BELOW the
    # positions() call, so a flat account still burned a live request
    # every cycle - and then retried at the same cadence on the 429,
    # making the rate limit worse while having nothing to do. Same shape
    # as the BMEA incident (~570 rejections over 5 hours, zero backoff).
    #
    # cached_positions is maintained by the main loop. If it is stale in
    # the direction that matters - it shows flat while a fractional
    # position actually exists - the next cycle catches it once the cache
    # refreshes, and the window here is minutes before core close, not
    # the close itself. Spending a request per cycle to learn "still
    # flat" is the worse trade.
    # A MISSING cache means "unknown" and falls through to the live call,
    # preserving the original behaviour; a PRESENT one is authoritative,
    # including when it is empty. Being one cycle late on a cache that
    # has not filled yet is harmless - this sweep runs repeatedly through
    # the whole pre-close window.
    cached = getattr(self, "cached_positions", None)
    if cached is not None and not any(
        item.get("instrument_type") == "EQUITY"
        and self.is_fractional_quantity(
            Decimal(str(item.get("quantity", "0") or "0"))
        )
        for item in cached
    ):
        return
    try:
        positions = self.api.positions()
    except Exception as exc:
        # Back off hard on a rate limit rather than returning to the
        # normal cadence - retrying a 429 every eod_retry_seconds is what
        # sustains it.
        if "429" in str(exc) or "TOO_MANY_REQUESTS" in str(exc).upper():
            self.last_fractional_sweep = now + float(
                self.config.eod_retry_seconds
            ) * 4
            log.warning(
                "CLOSE  | fractional pre-close sweep rate-limited | "
                "backing off | %s", exc,
            )
            return
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
