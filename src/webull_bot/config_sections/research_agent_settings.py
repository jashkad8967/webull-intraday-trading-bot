from decimal import Decimal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ResearchAgentSettings(BaseSettings):
    """The Groq-backed research/strategy-review agent: model selection,
    review cadence and payload sizing, daily request/token budgets, and
    the exit-influence/runner/de-risk confidence thresholds."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    agent_enabled: bool = True
    groq_api_key: str = ""
    # Not one of Groq's Compound systems - research is scored entirely from
    # provided STATE data with no web search (see market_agent.py), so
    # Compound's tool-orchestration layer was pure overhead: the actual
    # source of the truncated/malformed/empty responses _parse_response
    # kept having to work around. Groq has since removed every plain
    # (non-reasoning) chat model from its catalog - gpt-oss-120b is a
    # reasoning model too, but its hidden "thinking" tokens are small and
    # bounded (unlike Compound's orchestration overhead) and controllable
    # via groq_reasoning_effort below, so it's the closer match.
    groq_model: str = "openai/gpt-oss-120b"
    # Only meaningful for a gpt-oss model (see market_agent.py's request
    # builder) - "low" keeps hidden reasoning-token spend small so it
    # doesn't crowd out the actual JSON answer within max_completion_tokens.
    groq_reasoning_effort: str = "low"
    # Fixed cadence, no core/extended split (the agent reviews account
    # performance, not per-symbol setups, so there's no reason to research
    # more often just because the market's more active).
    strategy_review_enabled: bool = True
    # By request: "space out the research agent sentiment to every 30
    # minutes." Raised 900s (15min) -> 1800s (30min) - also frees up
    # daily request/token budget headroom for the new once-daily
    # predict_likely_gainers call (see AutoTrader.refresh_agent_
    # predicted_gainers), which shares this same Groq account budget.
    strategy_review_interval_seconds: int = Field(default=1800, ge=60, le=3600)
    # How many of the most recent StatusWriter.trades entries go into each
    # review's payload - small and fixed on purpose: this runs 4x/hour,
    # so the prompt has to stay bounded regardless of how many trades a
    # high-frequency account racks up between reviews.
    strategy_review_trade_history_limit: int = Field(default=15, ge=1, le=50)
    # Groq's own usage dashboard attributes each compound-mini call to 3
    # underlying model rows (the compound orchestration plus its 2 backing
    # models - see console.groq.com's per-key usage table), so the real
    # cost of one "successful" cycle can be ~3x its nominal request
    # weight. Sized for STRATEGY_REVIEW_INTERVAL_SECONDS=900 across the
    # MARKET_OPEN_TIME-to-EOD_CLOSE_TIME trading day (~16h / 900s ≈ 64
    # reviews/day), with margin.
    agent_daily_request_limit: int = Field(default=75, ge=1, le=250)
    # Groq's real cap is tokens per day (TPD), not request count - a quiet
    # account can exhaust TPD in well under agent_daily_request_limit
    # requests. This must match your actual Groq model/tier TPD limit (see
    # console.groq.com/settings/billing, which is the only place Groq
    # reports it - it isn't in any response header) with some margin.
    agent_daily_token_budget: int = Field(default=90000, ge=1000)
    # Per-list cap (gainers/losers/most-active each) for the deterministic
    # market-pulse context fed to the research agent - see
    # AutoTrader.refresh_market_pulse(). Small and fixed on purpose: this
    # replaced asking the agent to discover movers via open-ended web
    # search, which was the actual source of unpredictable request size.
    agent_market_pulse_symbols: int = Field(default=3, ge=1, le=10)
    agent_timeout_seconds: int = Field(default=60, ge=5, le=180)
    agent_exit_influence_enabled: bool = True
    agent_exit_min_confidence: Decimal = Field(
        default=Decimal("0.60"),
        ge=0,
        le=1,
    )
    agent_runner_bias_threshold: Decimal = Field(
        default=Decimal("0.50"),
        ge=0,
        le=1,
    )
    agent_runner_profit_percent: Decimal = Field(
        default=Decimal("0.01"),
        ge=0,
        le=Decimal("0.50"),
    )
    agent_derisk_bias_threshold: Decimal = Field(
        default=Decimal("-0.50"),
        ge=-1,
        le=0,
    )
