"""Uvicorn entrypoint: ``uvicorn --factory settleagent.api.main:create_app_from_settings``."""

from fastapi import FastAPI

from settleagent.api.app import ApiDeps, create_app
from settleagent.api.readiness import database_check, kafka_check, tcp_check
from settleagent.config import get_settings
from settleagent.observability.logging import configure_logging
from settleagent.observability.telemetry import setup_telemetry
from settleagent.persistence.db import make_engine, make_session_factory
from settleagent.remediation.service import RemediationService
from settleagent.remediation.system import SyntheticSettlementSystem


def create_app_from_settings() -> FastAPI:
    settings = get_settings()  # fails fast on missing/invalid config
    configure_logging(settings.log_level, json_logs=settings.log_json)
    telemetry = setup_telemetry(
        "settleagent-api",
        settings.otel_exporter_otlp_endpoint,
        enabled=settings.otel_enabled,
        metric_interval_ms=settings.otel_metric_interval_ms,
    )
    engine = make_engine(settings.database_url)
    sessions = make_session_factory(engine)
    checks = {
        "database": database_check(sessions),
        "kafka": kafka_check(settings.kafka_bootstrap_servers),
    }
    if settings.ready_requires_otel:
        checks["otel"] = tcp_check(settings.otel_exporter_otlp_endpoint)

    async def _shutdown() -> None:
        await engine.dispose()
        telemetry.shutdown()

    deps = ApiDeps(
        sessions=sessions,
        remediation=RemediationService(
            sessions, SyntheticSettlementSystem(sessions), telemetry=telemetry
        ),
        api_key=settings.api_key,
        readiness=checks,
        cost_ceiling_usd=settings.cost_ceiling_usd,
        budget_warn_fraction=settings.budget_warn_fraction,
        on_shutdown=_shutdown,
    )
    return create_app(deps)
