import logging
from datetime import datetime
from decimal import Decimal

log = logging.getLogger("webull-bot")


def log_day_end_summary(self, moment: datetime) -> None:
    if self.last_day_end_log_date == moment.date():
        return
    self.last_day_end_log_date = moment.date()
    try:
        buying_power = self.api.buying_power()
        positions = [
            item
            for item in self.api.positions()
            if Decimal(str(item.get("quantity", "0"))) != 0
        ]
        log.info(
            "DAYEND | date=%s | buying_power=$%.2f | positions=%s | working_orders=%s | popular_research=%s",
            moment.date().isoformat(),
            buying_power,
            len(positions),
            len(self.working_orders),
            ",".join(
                sorted(
                    self.seed_popular_symbols
                    | self.agent_popular_symbols
                )
            )
            or "NONE",
        )
    except Exception as exc:
        log.error("DAYEND | date=%s | summary failed | %s", moment.date(), exc)
