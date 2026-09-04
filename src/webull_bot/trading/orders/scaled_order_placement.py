import logging
import time
from decimal import ROUND_DOWN, Decimal

log = logging.getLogger("webull-bot")

# Iceberg / scaled order execution (see place_stock_scaled).
ICEBERG_MIN_SHARES = Decimal("50")
ICEBERG_SLICE_SHARES = 10
ICEBERG_SLICE_INTERVAL_SECONDS = 3

# Automated risk guardrails.
HARD_ORDER_NOTIONAL_CEILING = Decimal("2000")


def place_stock_scaled(
    self,
    symbol: str,
    side: str,
    quantity: int | Decimal,
    key: str,
    quote: dict,
    fractional: bool = False,
    limit_price_override: Decimal | None = None,
) -> str | None:
    """Slices a large order into smaller clips instead of dumping the
    whole size in one order - large firms never do that because it
    moves the price against them. Below ICEBERG_MIN_SHARES this is
    identical to calling api.place_stock directly (today's behavior,
    unchanged for the bot's normal small scalp sizes); at/above it,
    places the first clip now and schedules the remainder to trickle
    out via process_iceberg_orders() on later cycles - never blocks
    the polling loop with a sleep, since that would stall order
    monitoring, the dashboard, and every other symbol for the whole
    slice duration.

    Also enforces price_sanity_cooldown_ready here, universally, for
    every order this function submits - entries AND exits alike.
    Live incident: BMEA's profit-take order re-escalated and
    resubmitted every ~15-20s continuously for over 5 hours, hitting
    the price-sanity rejection ~570 times with zero backoff, because
    only entry code paths checked this cooldown themselves before
    calling here - nothing stopped an exit from hammering the same
    unreachable price forever.
    """
    if not self.price_sanity_cooldown_ready(symbol):
        return None
    total = Decimal(str(quantity))
    last_price = self.api.quote_price(quote)
    # Webull requires a 100-share minimum lot for any order (either
    # side) while price sits in the $0.10-$0.999 band - slicing into
    # ICEBERG_SLICE_SHARES=10-share clips there guarantees every
    # single slice gets rejected (live incident: HOWL, 417
    # OAUTH_OPENAPI_CANT_TRADE_FOR_PRICE_BETWEEN_0099_AND_0999, on
    # every iceberg slice attempt). A lot-restricted order is cheap
    # enough in absolute notional (100 shares of a sub-$1 stock) that
    # it doesn't need price-impact slicing anyway - place it whole.
    clip = (
        total
        if total < ICEBERG_MIN_SHARES
        or fractional
        or self.strategy.minimum_lot_size(last_price) > 1
        else Decimal(ICEBERG_SLICE_SHARES)
    )
    if not fractional and clip * last_price > HARD_ORDER_NOTIONAL_CEILING:
        clip = (HARD_ORDER_NOTIONAL_CEILING / last_price).to_integral_value(
            rounding=ROUND_DOWN
        )
        if clip <= 0:
            log.error(
                "GUARD  | %s | order notional exceeds the hard ceiling "
                "($%s) even at 1 share | order skipped",
                symbol,
                HARD_ORDER_NOTIONAL_CEILING,
            )
            return None
        clip = min(clip, total)
    elif fractional and clip * last_price > HARD_ORDER_NOTIONAL_CEILING:
        log.error(
            "GUARD  | %s | fractional order notional exceeds the hard "
            "ceiling ($%s) | order skipped",
            symbol,
            HARD_ORDER_NOTIONAL_CEILING,
        )
        return None
    limit_price = (
        limit_price_override
        if limit_price_override is not None
        else self.api.stock_limit_price(quote, side)
    )
    if not self.price_sanity_ok(symbol, last_price, limit_price):
        return None
    try:
        order_id = self.api.place_stock(
            symbol,
            side,
            clip if not fractional else total,
            limit_price=limit_price,
            fractional=fractional,
        )
    except Exception as exc:
        self.record_order_error(symbol, exc)
        raise
    remaining = total - (clip if not fractional else total)
    if remaining > 0:
        self.iceberg_orders[f"{symbol}:{side}"] = {
            "symbol": symbol,
            "side": side,
            "key": key,
            "remaining": remaining,
            "last_slice_at": time.monotonic(),
        }
        log.info(
            "ICEBERG| %s | %s | first clip=%s | remaining=%s over %s "
            "more slice(s)",
            symbol,
            side,
            clip,
            remaining,
            -(-remaining // ICEBERG_SLICE_SHARES),
        )
    return order_id
