def _note_exit_failure(self, key: str) -> None:
    """Tracks a confirmed never-filled exit attempt - see
    consecutive_exit_failures and CONSECUTIVE_EXIT_FAILURE_MARKET_
    THRESHOLD. Stock-only: this class of endless-retry loop was seen
    for stocks specifically, and options' own defined-risk sizing
    already bounds the exposure a stuck options exit represents
    differently enough that folding it into the same counter isn't
    clearly right without its own evidence.
    """
    if not key.startswith("STOCK:"):
        return
    symbol = key.split(":", 1)[1]
    if symbol:
        self.consecutive_exit_failures[symbol] += 1
