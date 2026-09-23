import threading

# One lock for the whole claim table. Held only for a set membership
# test plus an add - microseconds, never across an API call - so
# contention between the scan thread and the 0.5s protection thread is
# irrelevant. Module-level rather than per-instance because there is
# exactly one AutoTrader per process and this must be shared by every
# caller that can place a closing order.
_CLAIM_LOCK = threading.Lock()


def _claim_option_exit(self, option_symbol: str) -> bool:
    """Atomically reserve the right to place a closing order for one
    option contract. True means the caller owns it; False means
    somebody else already does.

    Exits are now reached from TWO threads: the 0.5s protection loop
    (evaluate_held_option_exits) and the slow scan (trade_options),
    plus boost_stalled_positions and the dashboard's manual sell. The
    existing guards were all plain check-then-act -

        if option_symbol not in self.pending_option_exits:
            ...
            order_id = self.api.place_option(...)   # hundreds of ms
            self.pending_option_exits.add(option_symbol)

    - so two threads could both pass the test before either added, and
    the window spans a whole broker round-trip rather than a few
    microseconds. The result is two SELL orders against a single
    position: at best the broker rejects the second, at worst it
    reverses the position into a short.

    Test-and-set under one lock closes that window. Releasing is just
    pending_option_exits.discard, which already happens wherever an
    exit completes, fails or is superseded - so no new lifecycle is
    introduced, only a safe way to enter it.
    """
    with _CLAIM_LOCK:
        if option_symbol in self.pending_option_exits:
            return False
        self.pending_option_exits.add(option_symbol)
        return True


def _release_option_exit(self, option_symbol: str) -> None:
    """Hand the claim back when an order could not be placed, so a
    transient failure does not lock the contract out of every future
    exit attempt for the rest of the session.
    """
    with _CLAIM_LOCK:
        self.pending_option_exits.discard(option_symbol)
