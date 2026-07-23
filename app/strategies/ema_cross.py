from app.models.domain import Signal
from app.strategies.base import Strategy
class EMACross(Strategy):
    def __init__(self, fast: int = 9, slow: int = 21): self.fast, self.slow = fast, slow
    @staticmethod
    def _ema(values, span):
        weight=2/(span+1); current=values[0]
        for value in values[1:]: current=value*weight+current*(1-weight)
        return current
    def generate_signal(self, data: list[float]) -> Signal:
        if len(data) < self.slow + 1: return Signal.HOLD
        previous = self._ema(data[:-1][-self.slow:], self.fast) - self._ema(data[:-1][-self.slow:], self.slow)
        current = self._ema(data[-self.slow:], self.fast) - self._ema(data[-self.slow:], self.slow)
        return Signal.BUY if previous <= 0 < current else Signal.SELL if previous >= 0 > current else Signal.HOLD
