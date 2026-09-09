from decimal import Decimal
from functools import lru_cache

from pydantic_settings import SettingsConfigDict

from webull_bot.config_sections.connection_settings import ConnectionSettings
from webull_bot.config_sections.entry_filter_settings import EntryFilterSettings
from webull_bot.config_sections.entry_timing_settings import EntryTimingSettings
from webull_bot.config_sections.option_trading_settings import OptionTradingSettings
from webull_bot.config_sections.order_execution_settings import OrderExecutionSettings
from webull_bot.config_sections.position_sizing_settings import PositionSizingSettings
from webull_bot.config_sections.rate_limit_settings import RateLimitSettings
from webull_bot.config_sections.research_agent_settings import ResearchAgentSettings
from webull_bot.config_sections.risk_circuit_breaker_settings import (
    RiskCircuitBreakerSettings,
)
from webull_bot.config_sections.session_schedule_settings import SessionScheduleSettings
from webull_bot.config_sections.state_and_tuning_settings import StateAndTuningSettings
from webull_bot.config_sections.stock_exit_settings import StockExitSettings
from webull_bot.config_sections.universe_settings import UniverseSettings
from webull_bot.config_sections.volatility_scalp_settings import VolatilityScalpSettings


class Settings(
    ConnectionSettings,
    UniverseSettings,
    EntryFilterSettings,
    VolatilityScalpSettings,
    PositionSizingSettings,
    EntryTimingSettings,
    StockExitSettings,
    OptionTradingSettings,
    OrderExecutionSettings,
    RateLimitSettings,
    ResearchAgentSettings,
    RiskCircuitBreakerSettings,
    SessionScheduleSettings,
    StateAndTuningSettings,
):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def validate_runtime(self) -> None:
        self.validate_connection(require_account=True)
        if self.ema_fast_period >= self.ema_slow_period:
            raise ValueError("EMA_FAST_PERIOD must be lower than EMA_SLOW_PERIOD")
        if self.stock_stop_loss_min_percent > self.stock_stop_loss_max_percent:
            raise ValueError(
                "STOCK_STOP_LOSS_MIN_PERCENT must not exceed STOCK_STOP_LOSS_MAX_PERCENT"
            )
        if self.option_min_dte > self.option_max_dte:
            raise ValueError("OPTION_MIN_DTE must not exceed OPTION_MAX_DTE")
        if self.option_min_hold_dte >= self.option_max_dte:
            raise ValueError(
                "OPTION_MIN_HOLD_DTE must be lower than OPTION_MAX_DTE, or "
                "every discovered contract would fall inside its own "
                "forced-exit window and never be enterable"
            )
        if self.stock_priority_fraction + self.stock_penny_fraction > 0.90:
            raise ValueError(
                "STOCK_PRIORITY_FRACTION + STOCK_PENNY_FRACTION must be <= 0.90"
            )
        capital_total = sum(self.stock_capital_fractions().values())
        if capital_total != Decimal("1"):
            raise ValueError(
                "Stock capital fractions must add up to exactly 1.0"
            )
        if not (
            self.session_time(self.market_open_time)
            < self.session_time(self.eod_close_time)
            < self.session_time(self.market_close_time)
        ):
            raise ValueError(
                "Stock session times must be ordered: MARKET_OPEN_TIME, "
                "EOD_CLOSE_TIME, MARKET_CLOSE_TIME"
            )
        if not (
            self.session_time(self.option_market_open_time)
            < self.session_time(self.option_eod_close_time)
            < self.session_time(self.option_market_close_time)
        ):
            raise ValueError(
                "Option session times must be ordered: OPTION_MARKET_OPEN_TIME, "
                "OPTION_EOD_CLOSE_TIME, OPTION_MARKET_CLOSE_TIME"
            )
        if not self.live_trading_enabled:
            raise ValueError("Production mode requires LIVE_TRADING_ENABLED=true")
        if self.agent_enabled and not self.groq_api_key:
            raise ValueError("GROQ_API_KEY is required when AGENT_ENABLED=true")


@lru_cache
def settings() -> Settings:
    return Settings()
