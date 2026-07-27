import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


class TradingTimezoneFormatter(logging.Formatter):
    def __init__(self, timezone: ZoneInfo):
        super().__init__(
            "%(asctime)s %(levelname)-8s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
        self.timezone = timezone

    def formatTime(self, record, datefmt=None):
        moment = datetime.fromtimestamp(record.created, self.timezone)
        return moment.strftime(datefmt) if datefmt else moment.isoformat()


class DatedDailyFileHandler(logging.Handler):
    """Write logs live to logs/YYYY/MM/YYYY-MM-DD.log in trading time."""

    def __init__(self, directory: str, timezone: str):
        super().__init__()
        self.directory = Path(directory)
        self.timezone = ZoneInfo(timezone)
        self._date = None
        self._stream = None

    def _stream_for_today(self):
        today = datetime.now(self.timezone).date()
        if self._stream is not None and today == self._date:
            return self._stream
        if self._stream is not None:
            self._stream.close()
        path = (
            self.directory
            / f"{today:%Y}"
            / f"{today:%m}"
            / f"{today:%Y-%m-%d}.log"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = path.open("a", encoding="utf-8")
        self._date = today
        return self._stream

    def emit(self, record: logging.LogRecord) -> None:
        try:
            stream = self._stream_for_today()
            stream.write(self.format(record) + "\n")
            stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        super().close()


def add_daily_file_logging(
    logger: logging.Logger,
    directory: str,
    timezone: str,
) -> None:
    if any(isinstance(item, DatedDailyFileHandler) for item in logger.handlers):
        return
    handler = DatedDailyFileHandler(directory, timezone)
    handler.setLevel(logging.INFO)
    handler.setFormatter(TradingTimezoneFormatter(handler.timezone))
    logger.addHandler(handler)
