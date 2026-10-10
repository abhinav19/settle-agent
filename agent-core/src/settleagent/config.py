"""12-factor configuration. Fails fast on missing or invalid values at startup."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration, sourced from environment variables / .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="forbid", frozen=True)

    kafka_bootstrap_servers: str
    kafka_topic: str = "settlement.failures"
    kafka_dlq_topic: str = "settlement.failures.dlq"
    kafka_group_id: str = "settleagent"

    database_url: str

    anthropic_api_key: str = Field(default="", repr=False)
    anthropic_model: str = "claude-opus-5-5"
    llm_effort: str | None = "medium"
    llm_max_tokens: int = Field(default=4096, ge=256)

    api_key: str = Field(repr=False, min_length=16)

    auto_threshold: float = Field(default=0.9, ge=0.0, le=1.0)
    max_steps: int = Field(default=12, ge=1)
    max_analyze_rounds: int = Field(default=2, ge=0)
    degraded_confidence_cap: float = Field(default=0.6, ge=0.0, le=1.0)
    max_inflight_cases: int = Field(default=8, ge=1)

    tool_timeout_s: float = Field(default=10.0, gt=0)
    tool_max_attempts: int = Field(default=3, ge=1)
    tool_backoff_s: float = Field(default=0.2, ge=0)
    breaker_threshold: int = Field(default=5, ge=1)
    breaker_cooldown_s: float = Field(default=30.0, gt=0)

    mcp_security_master_url: str = "http://localhost:8101/mcp"
    mcp_ssi_store_url: str = "http://localhost:8102/mcp"
    mcp_holiday_calendar_url: str = "http://localhost:8103/mcp"
    mcp_trade_store_url: str = "http://localhost:8104/mcp"
    cost_ceiling_usd: float = Field(default=50.0, gt=0)

    otel_exporter_otlp_endpoint: str = "http://localhost:4317"


@lru_cache
def get_settings() -> Settings:
    """Load settings once; raises ValidationError if config is missing/invalid."""
    return Settings()
