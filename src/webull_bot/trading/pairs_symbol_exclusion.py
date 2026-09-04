from webull_bot.pairs import PAIRS


def exclude_pairs_symbols(symbols: list[str]) -> tuple[list[str], list[str]]:
    """A pairs leg can be short (a negative broker-reported quantity),
    and stock_decision treats any non-positive quantity as "flat,
    eligible to BUY" - left in the main scan, the EMA/OBI strategy would
    try to buy into a position trade_pairs is deliberately holding
    short.
    """
    pairs_symbols = {symbol for pair in PAIRS for symbol in pair}
    if not pairs_symbols:
        return symbols, []
    excluded = [symbol for symbol in symbols if symbol in pairs_symbols]
    if not excluded:
        return symbols, []
    remaining = [symbol for symbol in symbols if symbol not in pairs_symbols]
    return remaining, excluded
