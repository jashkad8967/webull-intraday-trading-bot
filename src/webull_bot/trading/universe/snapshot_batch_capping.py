from webull_bot.webull_api import WebullAPI


def cap_batch_to_snapshot_limit(
    batch: list[str],
    unmanaged_held: list[str],
    limit: int = WebullAPI.STOCK_SNAPSHOT_MAX_SYMBOLS,
) -> list[str]:
    """Caps a scan batch at `limit` (defaults to WebullAPI's own
    hard 100-symbol snapshot limit for a single quote-fetch call -
    see trade_stocks, which passes a multiple of that when firing
    several concurrent quote batches this cycle - see
    stock_scan_concurrent_batches), always keeping every currently-
    held position first (a real position losing quote coverage -
    see the "fell out of the scanned universe" GUARD warning just
    above this call site - is the more severe failure mode) and
    only trimming the lower-priority remainder. Without this,
    force-injecting the curated cohort/eligible-symbol set on top
    of an already-full batch could push the combined size past
    what this cycle's quote fetch(es) can cover, losing price data
    for every symbol past the limit, not just the extra ones.
    """
    if len(batch) <= limit:
        return batch
    held_set = set(unmanaged_held)
    prioritized = [symbol for symbol in batch if symbol in held_set]
    rest = [symbol for symbol in batch if symbol not in held_set]
    room = max(0, limit - len(prioritized))
    return prioritized + rest[:room]
