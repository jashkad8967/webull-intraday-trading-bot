import time


def _is_rate_limited(exc: Exception) -> bool:
    """True for Webull's 429 TOO_MANY_REQUESTS rejection - live evidence
    this session: CLOSE (fractional pre-close sweep) and RECON (order
    history reconciliation) both hit it right after a restart's initial
    burst of setup calls.
    """
    text = str(exc).upper()
    return "429" in text or "TOO_MANY_REQUESTS" in text


def _retry_once_on_rate_limit(fn, *args, delay: float = 0.3, **kwargs):
    """By request: "if there is any 429, make sure to refire that order
    asap" - a single quick retry (not an unbounded loop, which would
    itself contribute to the rate limit it's trying to recover from)
    after a brief pause, specifically for the order-placement/
    cancellation calls in the position-protection loop where a missed
    action costs real money/opportunity (unlike a quote/position
    lookup, which already fails soft and just retries next cycle
    regardless). Re-raises whatever the second attempt raises (a non-
    429 exception immediately, or the 429 again after the one retry) -
    callers keep their own existing try/except handling unchanged.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        if not _is_rate_limited(exc):
            raise
        time.sleep(delay)
        return fn(*args, **kwargs)
