import logging
import time

log = logging.getLogger("webull-bot")

CONSECUTIVE_ORDER_ERROR_LIMIT = 5
ORDER_ERROR_WINDOW_SECONDS = 60


def record_order_error(self, symbol: str, exc: Exception) -> None:
    """Order-error guard: distinct from the existing P&L-based circuit
    breakers (daily-loss, loss-spree) because it fires on *error
    rate*, not realized loss - the guard against a rogue loop or a
    systematically broken order path (bad auth, malformed payload,
    API outage) spinning through the whole symbol universe before any
    single trade even fills.

    Blacklists only the offending symbol (reusing
    broker_conflict_symbols - every entry path already skips symbols
    in that set), not the whole account. This used to trip a global
    kill switch that halted every symbol's entries AND exits until
    the process was restarted - in production, a single symbol stuck
    in a broker-side rejection (e.g. Webull's $0.10-$0.999 lot-size
    rule) repeatedly tripped this and froze the entire bot for the
    rest of the session over a problem confined to one symbol. The
    error-rate counter itself stays global (still the right signal
    for "something is systematically broken," e.g. bad auth spamming
    errors across many different symbols), but the consequence is now
    scoped to whichever symbol actually caused it.
    """
    now = time.monotonic()
    self.order_error_times.append(now)
    while (
        self.order_error_times
        and now - self.order_error_times[0] > ORDER_ERROR_WINDOW_SECONDS
    ):
        self.order_error_times.popleft()
    if len(self.order_error_times) >= CONSECUTIVE_ORDER_ERROR_LIMIT:
        self.order_error_times.clear()
        already_blacklisted = symbol in self.broker_conflict_symbols
        self.broker_conflict_symbols.add(symbol)
        self.pending_stock_exits.discard(symbol)
        self.pending_option_exits.discard(symbol)
        self.stop_exit_submitted.pop(symbol, None)
        self.stop_loss_escalated.discard(symbol)
        self.stop_condition_since.pop(symbol, None)
        if not already_blacklisted:
            log.critical(
                "GUARD  | %s order errors in %ss (last: %s | %s) | "
                "blacklisting %s from further automated action for "
                "the rest of the day - other symbols are unaffected",
                CONSECUTIVE_ORDER_ERROR_LIMIT,
                ORDER_ERROR_WINDOW_SECONDS,
                symbol,
                exc,
                symbol,
            )
