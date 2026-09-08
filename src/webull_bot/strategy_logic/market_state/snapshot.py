import math
from decimal import Decimal


def clear_market_state(self) -> None:
    self.activity.clear()
    self.prices.clear()
    self.metrics.clear()
    self.selection_buckets.clear()
    self.vwap_state.clear()
    self.crossover_counts.clear()
    self.tick_history.clear()
    self.volatility_price_history.clear()
    self.recent_tick_history.clear()
    self.volume_delta_baseline.clear()
    self.volume_delta_ema.clear()
    self.volume_delta_latest.clear()


def rotating_batch(items: list, cursor: int, batch_size: int) -> tuple[list, int]:
    if not items:
        return [], 0
    size = min(batch_size, len(items))
    batch = [items[(cursor + offset) % len(items)] for offset in range(size)]
    return batch, (cursor + size) % len(items)


def quote_number(quote: dict, *fields: str) -> float:
    for field in fields:
        try:
            value = float(quote.get(field, ""))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            return value
    return 0.0


def update_stock_snapshot(self, quote: dict, price: Decimal) -> None:
    symbol = str(quote.get("symbol", "")).upper()
    if not symbol:
        return
    regular_volume = self.quote_number(quote, "volume")
    extended_volume = self.quote_number(quote, "extend_hour_volume")
    volume = max(0.0, regular_volume + extended_volume)
    movement = max(
        abs(self.quote_number(quote, "change_ratio")),
        abs(self.quote_number(quote, "extend_hour_change_ratio")),
    )
    high = self.quote_number(quote, "extend_hour_high", "high")
    low = self.quote_number(quote, "extend_hour_low", "low")
    bid = self.quote_number(quote, "bid")
    ask = self.quote_number(quote, "ask")
    midpoint = (bid + ask) / 2 if bid > 0 and ask >= bid else 0.0
    spread_percent = (
        ((ask - bid) / midpoint) * 100
        if midpoint > 0
        else 0.0
    )
    range_ratio = (
        (high - low) / float(price)
        if price > 0 and high >= low
        else 0.0
    )
    activity = (
        math.log10(1.0 + volume)
        + 50.0 * movement
        + 25.0 * max(0.0, range_ratio)
    )
    self.prices[symbol] = price
    self.activity[symbol] = activity
    self.metrics[symbol] = {
        "volume": int(volume),
        "change_ratio": movement,
        "bid": bid,
        "ask": ask,
        "spread_percent": round(spread_percent, 4),
        "range_ratio": round(max(0.0, range_ratio), 4),
        "high": high,
        "low": low,
        "activity_score": activity,
    }
    self._update_vwap(symbol, price, volume)
    if price > 0:
        self.volatility_price_history[symbol].append(float(price))
