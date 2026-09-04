def _compact_number(value, digits: int | None = None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if digits is None:
        return int(number) if number == int(number) else round(number, 4)
    rounded = round(number, digits)
    return int(rounded) if digits == 0 else rounded
