from decimal import Decimal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class StateAndTuningSettings(BaseSettings):
    """Wash-sale window, persisted-state file paths, and the
    (currently disabled) auto-apply strategy-tuning gate/state, plus
    the remaining log/status/command file paths."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    wash_sale_block_days: int = Field(default=31, ge=31, le=365)
    wash_sale_state_file: str = "conf/wash_sale_blocks.json"
    daily_pnl_state_file: str = "conf/daily_pnl.json"
    trade_history_state_file: str = "conf/trade_history.json"
    invalid_symbol_state_file: str = "conf/invalid_symbols.json"
    option_contracts_state_file: str = "conf/option_contracts.json"
    # DISABLED by request, after an independent risk review found fully-
    # automatic live application of model-generated tuning to be the
    # most urgent operational risk in the system - a synthetic test
    # suite can prove config.py still behaves structurally after a
    # change, it cannot prove the change improves live expectancy. This
    # flag is now an actual functional gate (scripts/apply_strategy_
    # review.py reads and enforces it directly) - previously it was
    # declared but never read anywhere, giving false confidence that it
    # controlled anything. The real, primary gate is
    # .github/workflows/strategy-tuning-auto-apply.yml's own `if: false`
    # on the job itself (see that file's header comment) - this is
    # deliberate defense-in-depth on top of that, not the only gate.
    # See src/webull_bot/strategy_tuning.py for the bounded lever table
    # this would gate if ever re-enabled.
    strategy_tuning_auto_apply_enabled: bool = False
    strategy_tuning_cooldown_hours: int = Field(default=24, ge=1, le=168)
    strategy_tuning_step_fraction: Decimal = Field(
        default=Decimal("0.10"), gt=0, le=Decimal("0.5")
    )
    strategy_tuning_state_file: str = "conf/strategy_tuning.json"
    log_directory: str = "logs"
    # Days of daily log files to keep. 5 by explicit request ("there is
    # no need to save so much data") after the host's 8.7G root filled
    # to 99% on 2026-09-23 and wedged Docker with the bot DOWN
    # mid-session. Nothing reads these beyond the last few sessions -
    # the dashboard only ever opens TODAY's file - so keeping a month
    # was storage spent on data no one looks at. 0 disables pruning.
    log_retention_days: int = Field(default=5, ge=0, le=365)
    # When each currently-open position was entered. Tiny and bounded:
    # one timestamp per OPEN position, pruned to what is still held.
    # See PositionOpenTimeStore for the two live failures it fixes.
    position_open_times_state_file: str = "conf/position_open_times.json"
    status_file: str = "status.json"
    command_file: str = "commands.json"
