from decimal import Decimal


def accounts(self) -> list[dict]:
    return self._call(self.trade.account_v2.get_account_list, "account")


def balance(self) -> dict:
    return self._call(
        lambda: self.trade.account_v2.get_account_balance(self.config.account_id),
        "account",
    )


def buying_power(self) -> Decimal:
    return self.buying_power_from_balance(self.balance())


def buying_power_from_balance(balance: dict) -> Decimal:
    usd = next(
        (
            item
            for item in balance.get("account_currency_assets", [])
            if item.get("currency") == "USD"
        ),
        {},
    )
    buying_power_values: list[Decimal] = []
    for field in (
        "day_buying_power",
        "buying_power",
        "overnight_buying_power",
    ):
        if usd.get(field) not in (None, ""):
            buying_power_values.append(Decimal(str(usd[field])))
    if buying_power_values:
        return max(Decimal("0"), *buying_power_values)
    cash = usd.get("cash_balance")
    return Decimal(str(cash)) if cash not in (None, "") else Decimal("0")


def option_buying_power(self) -> Decimal:
    return self.option_buying_power_from_balance(self.balance())


def option_buying_power_from_balance(balance: dict) -> Decimal:
    """Live incident: every real option order attempt this session
    failed with OPENAPI_DAY_BUYING_POWER_INSUFFICIENT regardless of
    how cheap the contract was - Webull tracks option buying power
    as a COMPLETELY SEPARATE pool from stock buying power
    (balance()'s account_currency_assets carries both
    "day_buying_power" and "option_buying_power" as independent
    fields), and this codebase's buying_power_from_balance only
    ever read the stock-side fields. Sizing option orders against
    stock buying power meant every option entry this session was
    sized against the wrong number entirely.
    """
    usd = next(
        (
            item
            for item in balance.get("account_currency_assets", [])
            if item.get("currency") == "USD"
        ),
        {},
    )
    value = usd.get("option_buying_power")
    return Decimal(str(value)) if value not in (None, "") else Decimal("0")


def account_day_pnl_from_balance(balance: dict) -> Decimal | None:
    """Webull's own account-level today's total P&L (realized +
    unrealized since the prior session's close) - see
    total_day_profit_loss. Ground truth for the dashboard's headline
    P&L Today total, sourced from the same balance() call
    account_state already makes every cycle for buying_power (no
    extra network call). None if unreported.
    """
    reported = balance.get("total_day_profit_loss")
    if reported in (None, ""):
        return None
    try:
        return Decimal(str(reported))
    except Exception:
        return None


def account_value_from_balance(balance: dict) -> Decimal | None:
    """Total net liquidation value (cash + market value of every
    held position) - the account's actual full worth, distinct from
    buying_power (spendable cash only). Sourced from the same
    balance() call account_state already makes every cycle.
    """
    reported = balance.get("total_net_liquidation_value")
    if reported in (None, ""):
        return None
    try:
        return Decimal(str(reported))
    except Exception:
        return None


def positions(self) -> list[dict]:
    return self._call(
        lambda: self.trade.account_v2.get_account_position(self.config.account_id),
        "account",
    )


def stock_quantity(symbol: str, positions: list[dict]) -> int:
    return sum(
        int(Decimal(str(item.get("quantity", "0"))))
        for item in positions
        if item.get("instrument_type") == "EQUITY" and item.get("symbol") == symbol
    )


def stock_position(symbol: str, positions: list[dict]) -> tuple[Decimal, Decimal]:
    """Quantity is a Decimal, not an int - a fractional-share position
    (e.g. 0.5) would otherwise truncate to 0 here and become invisible
    to every decision/exit/closeout path that checks this quantity.
    """
    match = next(
        (
            item
            for item in positions
            if item.get("instrument_type") == "EQUITY"
            and item.get("symbol") == symbol
        ),
        None,
    )
    if not match:
        return Decimal("0"), Decimal("0")
    return (
        Decimal(str(match.get("quantity", "0"))),
        Decimal(str(match.get("cost_price", "0"))),
    )
