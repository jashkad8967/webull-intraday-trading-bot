from decimal import Decimal


def record_realized_exit(
    self,
    average_cost: Decimal,
    exit_price: Decimal,
    quantity: Decimal,
    multiplier: int = 1,
) -> Decimal:
    """Track today's realized P&L from a submitted exit's limit price.

    This is an estimate (actual fill price can differ slightly), which
    is fine for a dashboard total and the daily-loss circuit breaker -
    both care about the running picture, not cent-perfect accounting.
    Returns the estimated pnl so callers can show it on the trade log.
    """
    # Options are billed per contract, not per trade - see
    # option_sell_fee_per_contract. Using the flat stock fee here made
    # every realized option P&L optimistic, and that number feeds the
    # dashboard, the trade log and the daily-loss circuit breaker.
    fee = (
        self.config.option_sell_fee_per_contract * abs(quantity)
        if multiplier == 100
        else self.config.sell_fee_dollars
    )
    pnl = (exit_price - average_cost) * quantity * multiplier - fee
    self.daily_realized_pnl += pnl
    if pnl < 0:
        self.daily_realized_loss += -pnl
    self.daily_pnl.record(self.daily_realized_pnl, self.daily_realized_loss)
    return pnl


def correct_realized_exit(
    self, order_id: str, estimated_pnl: Decimal | None, delta: Decimal
) -> None:
    """Re-price an already-recorded exit against its REAL fill.

    record_realized_exit books an exit's pnl at SUBMISSION time from
    the limit price, because that's all that's known then. For a
    limit order that's usually exact, but Webull routes FRACTIONAL
    stock orders as MARKET orders - live incident (FIGR, by explicit
    request): submitted at 35.81 on 1.2067 shares, actually filled at
    35.71, so a recorded +$0.05 "PROFIT" was really a loss. delta is
    (actual_fill - estimated_price) * quantity * multiplier, i.e. the
    exact amount the original estimate was off by, so this just shifts
    the running totals and the displayed trade by it rather than
    recomputing a cost basis this layer doesn't have.
    """
    if not delta:
        return
    previous = estimated_pnl or Decimal("0")
    corrected = previous + delta
    self.daily_realized_pnl += delta
    # daily_realized_loss only tracks the LOSING side, so it has to be
    # rebuilt from the before/after signs rather than shifted by delta
    # (a trade crossing zero changes it by a different amount).
    if previous < 0:
        self.daily_realized_loss = max(
            Decimal("0"), self.daily_realized_loss - (-previous)
        )
    if corrected < 0:
        self.daily_realized_loss += -corrected
    self.daily_pnl.record(self.daily_realized_pnl, self.daily_realized_loss)
    self.status.amend_trade_pnl(order_id, corrected)


def reverse_phantom_exit(
    self, pnl: Decimal | None, order_id: str | None = None
) -> None:
    """Undo a realized-exit pnl that was recorded at order SUBMISSION
    time (see record_realized_exit) once it's confirmed the order
    never actually filled - either it was cancelled/failed outright,
    or it was deliberately abandoned mid-flight (escalation cancels
    the gentle order and lets a fresh one fire its own pnl next
    cycle). Without this, an exit that never fills still permanently
    inflates the daily realized total as if it had.

    Also discards the matching entry from the dashboard's trade log
    (see StatusWriter.discard_trade) - record_trade wrote it
    optimistically at the same submission time as the phantom pnl, so
    without this a cancelled order stays visible on Recent Trades
    forever, labeled as a completed profit that never happened.
    """
    if order_id:
        self.status.discard_trade(order_id)
    if not pnl:
        return
    self.daily_realized_pnl -= pnl
    if pnl < 0:
        self.daily_realized_loss = max(
            Decimal("0"), self.daily_realized_loss - (-pnl)
        )
    self.daily_pnl.record(self.daily_realized_pnl, self.daily_realized_loss)
