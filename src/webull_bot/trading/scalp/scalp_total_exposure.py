from decimal import Decimal


def volatility_scalp_total_exposure_ok(
    self,
    positions: list[dict],
    additional_value: Decimal,
) -> bool:
    """True as long as the WHOLE cohort's total position value
    (every currently-held volatility-scalp symbol combined, plus a
    prospective new buy) would stay within
    VOLATILITY_SCALP_MAX_TOTAL_EXPOSURE_FRACTION of account value.

    volatility_scalp_position_value_ok only bounds one symbol at a
    time - up to VOLATILITY_SCALP_MAX_CONCURRENT_POSITIONS symbols
    could each independently satisfy that cap while the account as a
    whole is almost entirely concentrated in this cohort during a
    correlated selloff (these are explicitly the most volatile names,
    selected together, so correlation during a broad move is likely
    rather than a tail case). Fails open (True) if account value
    isn't known yet, same as the per-symbol check.
    """
    account_value = self.cached_account_value
    if account_value is None or account_value <= 0:
        return True
    total = additional_value
    for position in positions:
        symbol = str(position.get("symbol", "")).upper()
        # self.volatility_scalp_positions (every symbol this
        # strategy has an in-process-tracked open position in), not
        # the narrower curated self.volatility_scalp_symbols cohort
        # list - entries now open for any eligible symbol, not just
        # the curated top handful, so exposure has to be summed
        # against the same broadened set.
        if symbol not in self.volatility_scalp_positions:
            continue
        quantity = Decimal(str(position.get("quantity", "0") or "0"))
        cost_price = Decimal(str(position.get("cost_price") or "0"))
        total += quantity * cost_price
    cap = account_value * self.config.volatility_scalp_max_total_exposure_fraction
    return total <= cap
