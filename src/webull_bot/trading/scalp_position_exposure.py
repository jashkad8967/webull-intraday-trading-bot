from decimal import Decimal


def volatility_scalp_position_value_ok(
    self,
    current_quantity,
    additional_quantity,
    price: Decimal,
) -> bool:
    """True as long as a cohort symbol's total position value
    (existing + a prospective new buy, fresh entry or averaging-
    down) would stay within VOLATILITY_SCALP_MAX_POSITION_FRACTION
    of total account value. Live incident: GAUZ alone grew to ~66%
    of a small account's total value - averaging is still allowed
    up to the separate VOLATILITY_SCALP_MAX_AVERAGING_BUYS cap, but
    never to the point of concentrating most of the account in one
    name. Fails open (True) if account value isn't known yet - a
    missing/stale account-value read should never itself block
    trading.
    """
    account_value = self.cached_account_value
    if account_value is None or account_value <= 0:
        return True
    projected_value = (
        Decimal(str(current_quantity)) + Decimal(str(additional_quantity))
    ) * price
    cap = account_value * self.config.volatility_scalp_max_position_fraction
    return projected_value <= cap
