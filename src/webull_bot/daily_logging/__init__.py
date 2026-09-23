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
    """Write logs live to logs/YYYY/MM/YYYY-MM-DD.log in trading time.

    Keeps the newest `retention_days` files (default 5) and deletes
    the rest.

    Without that this grew without bound: one file per day, forever,
    on a deploy host with an 8.7G root. That host filled to 99% on
    2026-09-23 and wedged Docker - the daemon reported "active" while
    `docker ps` hung and `docker ps -a` returned nothing, so the bot
    was DOWN mid-session with four open option positions and no stop,
    no profit-lock and no EOD close. Stale Docker images were the bulk
    of it (see deploy/gcp/deploy.sh), but these logs are the same
    unbounded-growth bug in the bot's own house, and the bot writes a
    SCAN and a GATES line every cycle all session.

    Pruning runs only when the file rolls to a new day, so the cost is
    once per day, not once per record.
    """

    def __init__(self, directory: str, timezone: str, retention_days: int = 5):
        super().__init__()
        self.directory = Path(directory)
        self.timezone = ZoneInfo(timezone)
        self.retention_days = retention_days
        self._date = None
        self._stream = None

    def _prune(self) -> None:
        """Delete all but the newest `retention_days` day files.

        Best-effort by design: a logging handler must never take the
        trading process down, so every failure here is swallowed. The
        glob matches the YYYY/MM/YYYY-MM-DD.log layout written below,
        and the names sort chronologically as strings.
        """
        if self.retention_days <= 0:
            return
        try:
            files = sorted(self.directory.glob("*/*/*.log"))
            for path in files[: -self.retention_days] if files else []:
                try:
                    path.unlink()
                except OSError:
                    continue
            # Drop the month/year directories left empty behind them.
            for parent in sorted(
                self.directory.glob("*/*"), reverse=True
            ) + sorted(self.directory.glob("*"), reverse=True):
                try:
                    if parent.is_dir() and not any(parent.iterdir()):
                        parent.rmdir()
                except OSError:
                    continue
        except Exception:
            pass

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
        self._prune()
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
    retention_days: int = 5,
) -> None:
    if any(isinstance(item, DatedDailyFileHandler) for item in logger.handlers):
        return
    handler = DatedDailyFileHandler(directory, timezone, retention_days)
    handler.setLevel(logging.INFO)
    handler.setFormatter(TradingTimezoneFormatter(handler.timezone))
    logger.addHandler(handler)
