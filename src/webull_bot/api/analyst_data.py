from decimal import Decimal


def analyst_target_price(self, symbol: str) -> Decimal | None:
    """Analyst mean target price, or None on no coverage (common for
    penny/micro-cap names) or any other failure - callers treat that
    the same as "no signal" rather than an error.
    """
    response = self._call(
        lambda: self.data.instrument.get_analyst_target_price(symbol),
        "stock_instrument",
    )
    mean = response.get("mean") if response else None
    if mean in (None, ""):
        return None
    try:
        return Decimal(str(mean))
    except Exception:
        return None


def analyst_rating(self, symbol: str) -> dict[str, int] | None:
    """Analyst rating consensus counts, or None on no coverage/failure -
    see analyst_target_price.
    """
    response = self._call(
        lambda: self.data.instrument.get_analyst_rating(symbol),
        "stock_instrument",
    )
    if not response:
        return None
    try:
        return {
            field: int(response.get(field) or 0)
            for field in (
                "strong_buy",
                "buy",
                "hold",
                "sell",
                "under_perform",
            )
        }
    except Exception:
        return None
