from datetime import time

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SessionScheduleSettings(BaseSettings):
    """Trading-day session windows (stock and option), the end-of-day
    retry/extended-hours profit-sweep cadences, and the market-holiday
    calendar."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    trading_timezone: str = "America/New_York"
    market_open_time: str = "04:00"
    eod_close_time: str = "19:50"
    market_close_time: str = "20:00"
    option_market_open_time: str = "09:30"
    option_eod_close_time: str = "15:50"
    option_market_close_time: str = "16:00"
    eod_retry_seconds: int = Field(default=10, ge=2, le=120)
    # By request, after pre-market losses: "capturing any profits to
    # close out the day as much as possible" outside core hours - how
    # often AutoTrader.close_profitable_positions_during_extended_hours
    # checks open equity positions and closes anything currently
    # sitting at a profit. Slower than eod_retry_seconds (that one's a
    # tight end-of-day retry loop) since this runs continuously through
    # the whole pre-market/after-hours session, not just a final
    # closeout window.
    extended_hours_profit_sweep_seconds: int = Field(
        default=60, ge=5, le=3600
    )
    # Live incident: is_trading_day only ever checked weekday (Mon-Fri)
    # against this list, which defaulted to empty - on Labor Day 2026
    # (a Monday), the bot spent the whole session repeatedly attempting
    # doomed orders every ~30s, each one rejected by Webull with
    # OPENAPI_CAN_NOT_TRADING_FOR_NON_TRADING_HOURS, until manually
    # fixed live via a MARKET_HOLIDAYS env override. Defaulting to a
    # real NYSE holiday calendar (rather than leaving new deploys to
    # rediscover this the same way) closes the gap out of the box;
    # .env can still override/extend this for a year not covered here.
    market_holidays: str = (
        "2026-01-01,2026-01-19,2026-02-16,2026-04-03,2026-05-25,"
        "2026-06-19,2026-07-03,2026-09-07,2026-11-26,2026-12-25,"
        "2027-01-01,2027-01-18,2027-02-15,2027-03-26,2027-05-31,"
        "2027-06-18,2027-07-05,2027-09-06,2027-11-25,2027-12-24"
    )

    def holidays(self) -> set[str]:
        return {item.strip() for item in self.market_holidays.split(",") if item.strip()}

    def session_time(self, value: str) -> time:
        hour, minute = (int(part) for part in value.split(":"))
        return time(hour, minute)
