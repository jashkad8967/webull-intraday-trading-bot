from datetime import datetime


def now(self) -> datetime:
    return datetime.now(self.timezone)


def is_trading_day(self, moment: datetime) -> bool:
    return (
        moment.weekday() < 5
        and moment.date().isoformat() not in self.config.holidays()
    )


def session_moment(self, moment: datetime, value: str) -> datetime:
    return datetime.combine(
        moment.date(),
        self.config.session_time(value),
        tzinfo=self.timezone,
    )
