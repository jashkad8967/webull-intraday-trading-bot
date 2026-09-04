from webull_bot.trading.compact_number import _compact_number


def _market_pulse_entries(data: dict[str, dict]) -> list[dict]:
    return [
        {
            "symbol": symbol,
            "chg": _compact_number(
                item.get("change_ratio", 0) * 100, 2
            ),
            "vol": _compact_number(item.get("volume", 0)),
        }
        for symbol, item in data.items()
    ]
