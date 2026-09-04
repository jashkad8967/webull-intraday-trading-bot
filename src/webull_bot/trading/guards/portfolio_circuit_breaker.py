import logging
import time
from decimal import Decimal

log = logging.getLogger("webull-bot")


def handle_portfolio_circuit_breaker(
    self,
    positions: list[dict],
    buying_power: Decimal,
) -> bool:
    if not self.config.loss_circuit_breaker_enabled:
        return False

    now = time.monotonic()
    if self.entries_paused:
        old_enough = (
            now - self.circuit_breaker_time
            >= self.config.loss_reevaluation_seconds
        )
        if old_enough:
            self.entries_paused = False
            log.warning(
                "CIRCUIT | resumed after %ss reevaluation pause",
                self.config.loss_reevaluation_seconds,
            )
            return False
        if (
            self.market_agent
            and now - self.last_circuit_research
            >= self.config.loss_reevaluation_seconds
        ):
            self.last_circuit_research = now
            self.submit_strategy_review(
                positions,
                buying_power,
                force=True,
                event="POST_LIQUIDATION_REEVALUATION",
            )
        return True

    states = []
    for position in positions:
        if Decimal(str(position.get("quantity", "0"))) == 0:
            continue
        symbol = str(position.get("symbol", "")).upper()
        states.append(
            {
                "symbol": symbol,
                "unrealized_pnl": self.strategy.position_unrealized_pnl(
                    position
                ),
            }
        )
    decision = self.strategy.portfolio_decision(
        states,
        self.config.loss_spree_position_count,
        self.config.loss_spree_total_dollars,
    )
    if decision.action != "LIQUIDATE":
        return False

    log.critical(
        "CIRCUIT | LIQUIDATE | losers=%s | loss=$%.2f | %s",
        decision.losing_positions,
        decision.total_loss,
        decision.reason,
    )
    submitted = self.api.close_all_positions(
        loss_callback=self.wash_sales.block,
    )
    log.warning("CIRCUIT | close orders submitted=%s | entries paused", len(submitted))
    self.entries_paused = True
    self.circuit_breaker_time = now
    self.last_circuit_research = now
    self.last_account_refresh = 0.0
    self.submit_strategy_review(
        positions,
        buying_power,
        force=True,
        event="LOSS_CIRCUIT_BREAKER_LIQUIDATION",
    )
    return True
