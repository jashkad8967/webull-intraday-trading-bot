from collections import defaultdict, deque


class EMACrossStrategy:
    def __init__(self, fast: int, slow: int):
        if fast >= slow:
            raise ValueError("fast EMA must be lower than slow EMA")
        self.fast = fast
        self.slow = slow
        self.history = defaultdict(lambda: deque(maxlen=slow + 1))

    @staticmethod
    def _ema(values: list[float], period: int) -> float:
        weight = 2 / (period + 1)
        result = values[0]
        for value in values[1:]:
            result = value * weight + result * (1 - weight)
        return result

    def signal(self, key: str, price: float, reenter_on_trend: bool = False) -> str:
        values = self.history[key]
        values.append(price)
        if len(values) < self.slow + 1:
            return "HOLD"
        series = list(values)
        previous = series[:-1]
        old_spread = self._ema(previous[-self.slow :], self.fast) - self._ema(
            previous[-self.slow :], self.slow
        )
        new_spread = self._ema(series[-self.slow :], self.fast) - self._ema(
            series[-self.slow :], self.slow
        )
        if old_spread <= 0 < new_spread:
            return "BUY"
        if old_spread >= 0 > new_spread:
            return "SELL"
        if reenter_on_trend:
            return "BUY" if new_spread > 0 else "SELL"
        return "HOLD"
