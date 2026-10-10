"""12-factor configuration. Fails fast on missing or invalid values at startup."""

from functools import lru_cache
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_database_url(url: str) -> str:
    """Hosts hand out ``postgres://`` / ``postgresql://``; we need the psycopg driver."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def normalize_mcp_url(value: str) -> str:
    """Accept a full URL or a bare ``host:port`` (what private-service discovery provides)."""
    if "://" not in value:
        value = f"http://{value}"
    return value if value.rstrip("/").endswith("/mcp") else value.rstrip("/") + "/mcp"


class Settings(BaseSettings):
    """Process configuration, sourced from environment variables / .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="forbid", frozen=True)

    kafka_bootstrap_servers: str
    kafka_topic: str = "settlement.failures"
    kafka_dlq_topic: str = "settlement.failures.dlq"
    kafka_group_id: str = "settleagent"

    database_url: str

    # Secured Kafka (Confluent Cloud, Upstash, MSK...). Empty = plaintext, e.g. local compose.
    kafka_security_protocol: str = ""
    kafka_sasl_mechanism: str = ""
    kafka_sasl_username: str = ""
    kafka_sasl_password: str = Field(default="", repr=False)

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
    llm_max_concurrency: int = Field(default=4, ge=1)
    llm_requests_per_minute: float = Field(default=300.0, ge=0, description="0 disables")
    llm_burst: int = Field(default=20, ge=1)
    shutdown_grace_s: float = Field(default=30.0, gt=0)
    kafka_max_poll_interval_ms: int = Field(default=600_000, ge=60_000)

    otel_exporter_otlp_endpoint: str = "http://localhost:4317"
    otel_enabled: bool = True
    otel_metric_interval_ms: int = Field(default=10_000, ge=1000)
    ready_requires_otel: bool = True
    log_level: str = "INFO"
    log_json: bool = True
    per_case_cost_alert_usd: float = Field(default=1.0, gt=0)
    budget_warn_fraction: float = Field(default=0.8, gt=0, le=1.0)

    @field_validator("database_url")
    @classmethod
    def _driver(cls, v: str) -> str:
        return normalize_database_url(v)

    @field_validator(
        "mcp_security_master_url",
        "mcp_ssi_store_url",
        "mcp_holiday_calendar_url",
        "mcp_trade_store_url",
    )
    @classmethod
    def _mcp(cls, v: str) -> str:
        return normalize_mcp_url(v)

    def kafka_client_config(self) -> dict[str, Any]:
        """librdkafka settings shared by the consumer, the DLQ producer and tools."""
        conf: dict[str, Any] = {"bootstrap.servers": self.kafka_bootstrap_servers}
        if self.kafka_security_protocol:
            conf["security.protocol"] = self.kafka_security_protocol
        if self.kafka_sasl_mechanism:
            conf["sasl.mechanism"] = self.kafka_sasl_mechanism
            conf["sasl.username"] = self.kafka_sasl_username
            conf["sasl.password"] = self.kafka_sasl_password
        return conf


@lru_cache
def get_settings() -> Settings:
    """Load settings once; raises ValidationError if config is missing/invalid."""
    return Settings()
