import logging
import time
from decimal import Decimal

log = logging.getLogger("webull-bot")

SHORT_SELLING_MIN_EQUITY = Decimal("2000")


def account_state(self) -> tuple[Decimal, list[dict]]:
    now = time.monotonic()
    if (
        now - self.last_account_refresh
        >= float(self.config.account_refresh_seconds)
    ):
        # MIN_CASH_RESERVE_DOLLARS is subtracted right here, once, at
        # the fresh broker read - not at every call site that reads
        # cached_buying_power. This value stays cached (and this
        # already-reduced) for ACCOUNT_REFRESH_SECONDS; subtracting
        # the reserve again on every cache hit within that window
        # would compound each cycle and drive spendable capital to
        # zero almost immediately.
        balance = self.api.balance()
        # Raw, before MIN_CASH_RESERVE_DOLLARS - the dashboard should
        # show the account's real buying power (what Webull's own app
        # shows), not the internally-reserved figure trading logic
        # actually sizes against below. The reserve is a real safety
        # margin for order sizing, not something the display should
        # silently subtract and show as a gap against Webull's app.
        self.cached_raw_buying_power = self.api.buying_power_from_balance(
            balance
        )
        self.cached_buying_power = max(
            Decimal("0"),
            self.cached_raw_buying_power - self.config.min_cash_reserve_dollars,
        )
        # Live incident: Webull tracks option buying power as a
        # COMPLETELY SEPARATE pool from stock buying power (a real
        # order attempt with plenty of stock buying power available
        # still failed with OPENAPI_DAY_BUYING_POWER_INSUFFICIENT,
        # because option_buying_power on the same balance() read
        # was $0) - option sizing must never use the stock-side
        # cached_buying_power above.
        self.cached_option_buying_power = self.api.option_buying_power_from_balance(
            balance
        )
        self.cached_account_day_pnl = self.api.account_day_pnl_from_balance(
            balance
        )
        self.cached_account_value = self.api.account_value_from_balance(balance)
        self.cached_positions = self.api.positions()
        self.last_account_refresh = now
        if (
            self.short_selling_supported
            and self.cached_account_value is not None
            and self.cached_account_value < SHORT_SELLING_MIN_EQUITY
        ):
            # Same threshold Webull's own rejection enforces - catch
            # it here, proactively, instead of spending a live order
            # attempt (certain to fail) to discover it. Once equity
            # clears the minimum on a later refresh, no code re-
            # enables this automatically (matches handle_short_
            # selling_unsupported's existing "restart to re-enable"
            # behavior) - intentionally conservative rather than
            # flapping short-selling on and off around the threshold.
            self.short_selling_supported = False
            log.warning(
                "SHORT  | account equity ($%s) is under Webull's $%s "
                "minimum for short selling - disabling new short "
                "entries for the rest of this run",
                self.cached_account_value,
                SHORT_SELLING_MIN_EQUITY,
            )
    return self.cached_buying_power, [dict(item) for item in self.cached_positions]
