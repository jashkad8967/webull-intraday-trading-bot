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
    pnl = (exit_price - average_cost) * quantity * multiplier - self.config.sell_fee_dollars
    self.daily_realized_pnl += pnl
    if pnl < 0:
        self.daily_realized_loss += -pnl
    self.daily_pnl.record(self.daily_realized_pnl, self.daily_realized_loss)
    return pnl


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
