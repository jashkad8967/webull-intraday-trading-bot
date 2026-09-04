import logging
import time
from decimal import Decimal

from webull_bot.trading.orders.locks import _working_orders_lock

log = logging.getLogger("webull-bot")


def record_trade(
    self,
    key: str,
    order_id: str,
    action: str,
    limit_price: Decimal | None = None,
    pnl: Decimal | None = None,
    entry_price: Decimal | None = None,
    quantity: Decimal | None = None,
    counts_toward_idle_cash_ramp: bool = True,
) -> None:
    submitted_at = time.monotonic()
    if action in ("MANUAL_BUY", "MANUAL_SELL") and key.startswith("STOCK:"):
        self.manual_touch_at[key.split(":", 1)[1]] = submitted_at
    self.last_trade[key] = submitted_at
    self.submitted_order_ids_today.add(order_id)
    if action == "PARTIAL_PROFIT":
        # By request: "sell 5 every 5 cents it goes up... keep the
        # rest for later profit" - a partial exit closes SOME of a
        # held position, not all of it. Unlike PROFIT/STOP/MANUAL_
        # SELL below, this must NOT clear position_opened_at or
        # last_exit_at (the position is still open) and must
        # DECREMENT, not zero, the matching cached_positions entry -
        # zeroing here would make the fast loop's PROFIT/LOSS
        # evaluator think the whole position closed and stop
        # protecting the shares that are still held.
        if key.startswith("STOCK:") and quantity is not None:
            exited_symbol = key.split(":", 1)[1]
            for item in getattr(self, "cached_positions", None) or []:
                if (
                    item.get("instrument_type") == "EQUITY"
                    and str(item.get("symbol", "")).upper() == exited_symbol
                ):
                    try:
                        remaining = Decimal(str(item.get("quantity", "0"))) - quantity
                    except Exception:
                        remaining = None
                    if remaining is not None:
                        item["quantity"] = str(max(remaining, Decimal("0")))
        if pnl is not None:
            self.symbol_pnl_history[key].append((submitted_at, pnl))
    if action in ("PROFIT", "STOP", "MANUAL_SELL"):
        self.last_exit_at[key] = submitted_at
        self.position_opened_at.pop(key, None)
        # Live incident (this bug, caught right after shipping
        # evaluate_held_stock_exits): self.cached_positions only
        # gets refreshed once per SLOW trade_stocks cycle (30-90s+
        # live), but evaluate_held_stock_exits runs on the fast
        # 0.25s loop and decides whether to submit a fresh sell
        # purely from that cached snapshot's quantity. A position
        # exited here (fast loop or slow loop, either one) stayed
        # showing its OLD positive quantity in cached_positions
        # until the next slow refresh - so the fast loop kept
        # seeing it as still held and tried to sell it again,
        # several times, on symbols already fully closed (INN, OIS,
        # RDHL, SOAR all hit OPENAPI_NEW_NO_POSITION_MARGIN_
        # ACCOUNT_CAN_NOT_SELL_SHORT_FOR_LT_2K - Webull correctly
        # read "sell with nothing held" as an attempted short).
        # Zeroing the matching entry in place here, on every exit
        # record regardless of which loop triggered it, keeps
        # cached_positions self-correcting immediately instead of
        # waiting out the slow refresh window.
        if key.startswith("STOCK:"):
            exited_symbol = key.split(":", 1)[1]
            for item in getattr(self, "cached_positions", None) or []:
                if (
                    item.get("instrument_type") == "EQUITY"
                    and str(item.get("symbol", "")).upper() == exited_symbol
                ):
                    item["quantity"] = "0"
        if pnl is not None:
            # Feeds symbol_quarantined() - every realized exit's P&L,
            # partitioned per-key so one bad symbol can't drag down
            # another's entry eligibility.
            self.symbol_pnl_history[key].append((submitted_at, pnl))
    if action == "STOP":
        # Feeds stop_loss_guard_active() - a real stop-loss fill (not a
        # manual sell or a profit-take), tracked regardless of symbol.
        self.recent_stop_losses.append(submitted_at)
        # Feeds post_stop_reentry_ready() - see its docstring for
        # the DAIC incident this guards against.
        if key.startswith("STOCK:"):
            self.last_volatility_stop_loss_at[key.split(":", 1)[1]] = submitted_at
    if (
        action in ("BUY", "SHORT", "MANUAL_BUY")
        and counts_toward_idle_cash_ramp
    ):
        # Resets the idle-cash gate-relaxation ramp (see
        # idle_cash_ramp_progress) - capital just got deployed, so
        # quality gates snap back to their normal strictness until
        # cash sits idle above MIN_CASH_RESERVE_DOLLARS again.
        #
        # Live incident (this bug, caught while investigating "not
        # investing all the capital"): the ramp is only ever read
        # by the GENERAL strategy's own entry gates (see the
        # idle_relaxation_multiplier/amount passed to stock_
        # decision in trade_stocks) - it has no effect on
        # volatility-scalp's own, separate entry conditions. But a
        # volatility-scalp BUY/average-down was resetting this same
        # clock anyway, since both paths call record_trade with the
        # same "BUY" action. With scalp trading firing every few
        # minutes, the general strategy's idle-cash grace/ramp
        # timer effectively never advanced, keeping ITS gates
        # (spread, VWAP, SMA) at full strictness indefinitely even
        # while real buying power sat unused for hours - scalp
        # capital being deployed doesn't mean the general
        # strategy's own capital pool isn't idle. Callers opt out
        # via counts_toward_idle_cash_ramp=False (the volatility-
        # scalp entry/averaging-down call sites) so only a genuine
        # general-strategy deployment resets this clock.
        self.last_capital_deployed_at = submitted_at
    if action in ("BUY", "SHORT"):
        # Feeds TradingStrategy.adaptive_stop_percent's time-aware
        # widen window - see position_opened_at.
        self.position_opened_at[key] = submitted_at
        # A fresh position starts with a clean exit-failure count -
        # see consecutive_exit_failures.
        if key.startswith("STOCK:"):
            self.consecutive_exit_failures.pop(key.split(":", 1)[1], None)
    self.trade_times[key].append(submitted_at)
    # Position-protection now runs on its own background thread
    # (see _position_protection_loop) - lock the dict write itself
    # since that thread's repricers/escalator also create/replace
    # working_orders entries concurrently.
    with _working_orders_lock(self):
        self.working_orders[order_id] = {
            "submitted_at": submitted_at,
            "key": key,
            "action": action,
            "cancel_requested_at": None,
            "limit_price": limit_price,
            "pnl": pnl,
            # Needed to resubmit a like-for-like replacement order when
            # actively repricing a resting entry - see
            # reprice_volatility_scalp_entries.
            "quantity": quantity,
        }
    instrument_type, symbol = key.split(":", 1)
    self.status.record_trade(
        instrument_type,
        symbol,
        action,
        limit_price,
        order_id,
        pnl,
        entry_price=entry_price,
        quantity=quantity,
    )
    limit_text = (
        f" | limit={limit_price}"
        if limit_price is not None
        else ""
    )
    log.info(
        "ORDER  | %-11s | %-6s | %-8s%s | id=%s",
        instrument_type,
        action,
        symbol,
        limit_text,
        order_id,
    )
