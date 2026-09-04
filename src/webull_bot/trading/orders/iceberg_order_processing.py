import logging
import time
from decimal import Decimal

from webull_bot.trading.orders.scaled_order_placement import (
    ICEBERG_SLICE_INTERVAL_SECONDS,
    ICEBERG_SLICE_SHARES,
)

log = logging.getLogger("webull-bot")


def process_iceberg_orders(self) -> None:
    now = time.monotonic()
    for iceberg_key in list(self.iceberg_orders):
        entry = self.iceberg_orders[iceberg_key]
        if now - entry["last_slice_at"] < ICEBERG_SLICE_INTERVAL_SECONDS:
            continue
        symbol = entry["symbol"]
        side = entry["side"]
        try:
            quote = self.api.stock_quote(symbol)
            last_price = self.api.quote_price(quote)
            # Same lot-restriction guard as place_stock_scaled - price
            # can drift into the $0.10-$0.999 band between the first
            # clip and a later slice even if it wasn't there at
            # submission time.
            clip = (
                entry["remaining"]
                if self.strategy.minimum_lot_size(last_price) > 1
                else min(entry["remaining"], Decimal(ICEBERG_SLICE_SHARES))
            )
            limit_price = self.api.stock_limit_price(quote, side)
            if not self.price_sanity_ok(symbol, self.api.quote_price(quote), limit_price):
                entry["last_slice_at"] = now
                continue
            order_id = self.api.place_stock(
                symbol,
                side,
                clip,
                limit_price=limit_price,
            )
        except Exception as exc:
            self.record_order_error(symbol, exc)
            log.error("ICEBERG| %s | slice failed | %s", symbol, exc)
            entry["last_slice_at"] = now
            continue
        self.record_trade(
            entry["key"], order_id, side, entry_price=limit_price, quantity=clip
        )
        entry["remaining"] -= clip
        entry["last_slice_at"] = now
        log.info(
            "ICEBERG| %s | %s | slice=%s | remaining=%s",
            symbol,
            side,
            clip,
            entry["remaining"],
        )
        if entry["remaining"] <= 0:
            del self.iceberg_orders[iceberg_key]
