from abc import ABC, abstractmethod
from app.models.domain import Signal
class Strategy(ABC):
    @abstractmethod
    def generate_signal(self, data: list[float]) -> Signal: ...
