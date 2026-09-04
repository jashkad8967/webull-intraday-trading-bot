import logging
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("webull-bot")

# By request: "do not wait for the response to fire another request" -
# scoped to the position-protection loop only (not the universe scan,
# which already hit live 429 TOO_MANY_REQUESTS rate-limit errors - see
# the CLOSE/RECON incidents this same session). Each repricer's per-
# candidate cancel+place (and stock_position lookup) previously ran
# ONE order at a time, each waiting out a full network round-trip
# before the next candidate's requests even started - with N stale
# orders needing action in the same cycle, that's N sequential round-
# trips instead of ~1. Bounded worker count (not unbounded) so a cycle
# with many candidates still can't multiply the account's real request
# rate past what a human clicking through the same N actions by hand
# would generate.
_POSITION_PROTECTION_MAX_WORKERS = 4


def _dispatch_concurrently(items: list, worker) -> None:
    """Runs worker(item) for every item without waiting for one to
    finish before starting the next (bounded by
    _POSITION_PROTECTION_MAX_WORKERS) - worker is expected to handle
    its own exceptions internally (every caller's per-candidate body
    already does, via its own try/except), same as the sequential
    for-loop this replaces. A single item's exception here would
    otherwise only surface (and stop the whole batch) when its future
    is collected - re-raising defeats "one bad candidate shouldn't
    block the rest," so any exception a worker doesn't catch itself is
    logged and swallowed here instead.
    """
    if not items:
        return
    with ThreadPoolExecutor(
        max_workers=min(_POSITION_PROTECTION_MAX_WORKERS, len(items))
    ) as pool:
        futures = [pool.submit(worker, item) for item in items]
        for future in futures:
            try:
                future.result()
            except Exception as exc:  # pragma: no cover - workers self-handle
                log.error("PROTECT| concurrent dispatch worker failed | %s", exc)
