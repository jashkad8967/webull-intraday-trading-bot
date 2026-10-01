import logging
from decimal import Decimal

log = logging.getLogger("webull-bot")


def process_ui_commands(
    self,
    positions: list[dict],
    buying_power: Decimal = Decimal("0"),
    core_session_active: bool = False,
) -> Decimal:
    """Executes dashboard-initiated actions (close all, sell one
    position, buy one symbol, cancel one pending order, add a watchlist
    symbol). The dashboard has no Webull credentials or API access of
    its own - it can only enqueue a request, which is executed here
    through the same order-placement, wash-sale, and position-tracking
    code every other entry/exit uses. Runs before the circuit-breaker
    gate so a manual risk-reducing action (Sell, Cancel, Close All) is
    never blocked by a paused/halted state - a manual Buy still is,
    naturally, since handle_portfolio_circuit_breaker/handle_daily_loss_
    breaker only gate the automatic entry paths that run after this.
    """
    try:
        commands = self.commands.pop_all()
    except Exception as exc:
        log.error("CMD    | queue read failed | %s", exc)
        return buying_power
    # Logged the moment they are CLAIMED, before any of them runs.
    #
    # pop_all clears the queue, so a command taken here and then lost -
    # the process restarting for a deploy mid-cycle, a crash, a kill -
    # leaves no trace at all. Live 2026-10-01: a manual NVDA buy was
    # queued, the container restarted for a deploy seconds later, and the
    # order simply never existed. Nothing warned; it was only caught by
    # checking positions afterwards and finding the account still flat.
    #
    # Deliberately NOT changed to at-least-once delivery. Re-running a
    # claimed command after a crash could place a SECOND live order, and
    # for a trading queue a dropped order is far safer than a duplicated
    # one - the stale-position race already showed what duplicate sells
    # cost. So the fix is to make a drop VISIBLE rather than to retry:
    # this line plus the per-command result below bracket every command,
    # and a claim with no matching outcome is a dropped order.
    if commands:
        log.info(
            "CMD    | claimed %s command(s) | %s",
            len(commands),
            ", ".join(
                f"{c.get('type')}:{c.get('symbol') or c.get('order_id') or '-'}"
                for c in commands
            ),
        )
    for command in commands:
        command_type = command.get("type")
        try:
            if command_type == "close_all":
                self.close_instruments({"EQUITY", "OPTION"})
                log.warning("CMD    | manual close-all executed from dashboard")
            elif command_type == "sell":
                self._manual_sell(command, positions, core_session_active)
            elif command_type == "buy":
                buying_power = self._manual_buy(
                    command, positions, buying_power, core_session_active
                )
            elif command_type == "watchlist_add":
                self.add_to_watchlist(command.get("symbol", ""))
            elif command_type == "cancel_order":
                self._manual_cancel_order(command)
            else:
                log.warning("CMD    | unknown command type=%s", command_type)
        except Exception as exc:
            log.error("CMD    | %s failed | %s", command_type, exc)
    return buying_power
