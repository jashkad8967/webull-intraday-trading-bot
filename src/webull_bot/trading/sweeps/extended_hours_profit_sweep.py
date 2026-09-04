import logging
import time
from decimal import Decimal

log = logging.getLogger("webull-bot")


def close_profitable_positions_during_extended_hours(self) -> None:
    """By request, after pre-market losses: "capturing any profits
    to close out the day as much as possible" outside core hours -
    proactively closes any equity position currently sitting at a
    profit during extended hours (pre-market or after-hours),
    instead of waiting for its normal PROFIT target or letting it
    ride toward an overnight hold. Only ever fires outside core
    hours (see the call site's core_session_active check) and only
    ever closes a confirmed GAIN - same reasoning as
    close_fractional_positions_before_core_close: locking in a
    profit before the session gets even thinner is the point,
    forcing a realized loss isn't.
    """
    now = time.monotonic()
    if (
        now - self.last_extended_hours_profit_sweep
        < float(self.config.extended_hours_profit_sweep_seconds)
    ):
        return
    self.last_extended_hours_profit_sweep = now
    try:
        positions = self.api.positions()
    except Exception as exc:
        log.error("CLOSE  | extended-hours profit sweep failed | %s", exc)
        return
    equity_positions = [
        item
        for item in positions
        if item.get("instrument_type") == "EQUITY"
        and Decimal(str(item.get("quantity", "0"))) != 0
    ]
    if not equity_positions:
        return
    profitable_symbols: set[str] = set()
    for item in equity_positions:
        symbol = str(item.get("symbol", "")).upper()
        cost = Decimal(str(item.get("cost_price") or "0"))
        if cost <= 0:
            continue
        # Live incident: UBER's extended-hours close order was
        # rejected with OPENAPI_FRACT_ONLT_CORE_TIME (fractional
        # orders are only accepted during core hours) EVERY cycle
        # for hours straight - this function only ever runs
        # outside core hours (see its own docstring), so a
        # fractional-quantity position here will ALWAYS hit this
        # same rejection, never just occasionally. Skip it outright
        # instead of retrying a guaranteed failure every cycle;
        # close_fractional_positions_before_core_close already
        # handles fractional exits once core hours actually start.
        quantity = Decimal(str(item.get("quantity", "0")))
        if self.is_fractional_quantity(quantity):
            if symbol not in self.extended_hours_fractional_skip_logged:
                self.extended_hours_fractional_skip_logged.add(symbol)
                log.info(
                    "CLOSE  | extended-hours profit sweep | %-8s | "
                    "fractional position, deferring to core hours",
                    symbol,
                )
            continue
        try:
            price = self.api.quote_price(self.api.stock_quote(symbol))
        except Exception as exc:
            log.warning(
                "CLOSE  | extended-hours profit sweep | %-8s | quote "
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
        str(item.get("symbol", "")).upper() for item in equity_positions
    } - profitable_symbols
    try:
        submitted = self.api.close_all_positions(
            {"EQUITY"},
            loss_callback=self.wash_sales.block,
            exclude_symbols=exclude_symbols,
        )
    except Exception as exc:
        log.error("CLOSE  | extended-hours profit sweep failed | %s", exc)
        return
    self.pending_stock_exits -= profitable_symbols
    log.info(
        "CLOSE  | extended-hours profit sweep | submitted=%s | %s",
        len(submitted),
        ",".join(sorted(profitable_symbols)),
    )
