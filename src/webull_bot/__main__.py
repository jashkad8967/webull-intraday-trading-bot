import sys

from webull_bot.bot import AutoTrader, force_close_all, log
from webull_bot.config import settings
from webull_bot.daily_logging import add_daily_file_logging


def main() -> None:
    try:
        runtime_config = settings()
        add_daily_file_logging(
            log,
            runtime_config.log_directory,
            runtime_config.trading_timezone,
        )
        if "--close-all" in sys.argv:
            force_close_all()
        else:
            AutoTrader().run()
    except KeyboardInterrupt:
        log.info("STOPPED")


if __name__ == "__main__":
    main()
