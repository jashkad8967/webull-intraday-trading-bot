import logging
import time
from decimal import Decimal

log = logging.getLogger("webull-bot")


def close_instruments(
    self,
    instrument_types: set[str],
    apply_overnight_hold: bool = False,
) -> bool:
    now = time.monotonic()
    if now - self.last_close_attempt < self.config.eod_retry_seconds:
        return False
    self.last_close_attempt = now
    held_overnight = (
        self.overnight_hold_symbols() if apply_overnight_hold else set()
    )
    try:
        submitted = self.api.close_all_positions(
            instrument_types,
            loss_callback=self.wash_sales.block,
            exclude_symbols=held_overnight,
        )
        self.pending_stock_exits.clear()
        self.pending_option_exits.clear()
        remaining = [
            item
            for item in self.api.positions()
            if item.get("instrument_type") in instrument_types
            if Decimal(str(item.get("quantity", "0"))) != 0
            if str(item.get("symbol", "")).upper() not in held_overnight
        ]
        log.info(
            "CLOSE  | submitted=%s | remaining=%s%s",
            len(submitted),
            len(remaining),
            f" | held overnight={len(held_overnight)}" if held_overnight else "",
        )
        return not remaining
    except Exception as exc:
        log.error("CLOSE  | failed | %s", exc)
        return False
