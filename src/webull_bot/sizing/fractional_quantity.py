from decimal import Decimal


def is_fractional_quantity(quantity: Decimal) -> bool:
    return quantity != quantity.to_integral_value()
