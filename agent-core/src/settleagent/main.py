"""Process entrypoint: wires config, Kafka, the agent graph and the case processor.

uv run python -m settleagent.main
"""

import asyncio
import contextlib
import logging
import signal
from dataclasses import dataclass
from typing import cast

import anthropic
from confluent_kafka import Consumer, Producer
from mcp import Client
from sqlalchemy.ext.asyncio import AsyncEngine

from settleagent.agent.graph import InvestigationAgent
from settleagent.agent.llm import AnthropicLlm
from settleagent.agent.nodes import AgentConfig
from settleagent.agent.ratelimit import RateLimitedLlm
from settleagent.config import Settings, get_settings
from settleagent.consumer.dlq import KafkaDlqPublisher
from settleagent.consumer.kafka import CaseConsumer, ConsumerLike
from settleagent.consumer.processor import CaseProcessor
from settleagent.consumer.service import EventIntake
from settleagent.domain.reason_codes import ToolName
from settleagent.gate.policy import default_policy
from settleagent.observability.cost import CostTracker
from settleagent.observability.instrumentation import TracedGateway, TracedLlm
from settleagent.observability.logging import configure_logging
from settleagent.observability.telemetry import Telemetry, setup_telemetry
from settleagent.persistence.db import make_engine, make_session_factory
from settleagent.persistence.metrics import compute_metrics, total_cost
from settleagent.tools.gateway import ClientFactory, McpToolGateway

log = logging.getLogger("settleagent.main")


GAUGE_REFRESH_S = 15.0


@dataclass
class App:
    consumer: CaseConsumer
    engine: AsyncEngine
    telemetry: Telemetry
    cost_tracker: CostTracker


def build_gateway(settings: Settings) -> McpToolGateway:
    urls = {
        ToolName.SECURITY_MASTER: settings.mcp_security_master_url,
        ToolName.SSI_STORE: settings.mcp_ssi_store_url,
        ToolName.HOLIDAY_CALENDAR: settings.mcp_holiday_calendar_url,
        ToolName.TRADE_STORE: settings.mcp_trade_store_url,
    }

    def factory(url: str) -> ClientFactory:
        return lambda: Client(url)

    return McpToolGateway(
        {tool: factory(url) for tool, url in urls.items()},
        timeout_s=settings.tool_timeout_s,
        max_attempts=settings.tool_max_attempts,
        backoff_s=settings.tool_backoff_s,
        breaker_threshold=settings.breaker_threshold,
        breaker_cooldown_s=settings.breaker_cooldown_s,
    )


def build_agent(settings: Settings, telemetry: Telemetry) -> InvestigationAgent:
    client = (
        anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        if settings.anthropic_api_key
        else anthropic.AsyncAnthropic()  # falls back to env / `ant auth login` profile
    )
    llm = AnthropicLlm(
        client,
        settings.anthropic_model,
        effort=settings.llm_effort,
        max_tokens=settings.llm_max_tokens,
    )
    limited = RateLimitedLlm(
        TracedLlm(llm, telemetry),  # the span times the call itself, not the queueing
        max_concurrent=settings.llm_max_concurrency,
        requests_per_minute=settings.llm_requests_per_minute,
        burst=settings.llm_burst,
    )
    return InvestigationAgent(
        limited,
        TracedGateway(build_gateway(settings), telemetry),
        AgentConfig(
            max_steps=settings.max_steps,
            max_analyze_rounds=settings.max_analyze_rounds,
            degraded_confidence_cap=settings.degraded_confidence_cap,
        ),
        telemetry,
    )


def build_app(settings: Settings, telemetry: Telemetry) -> App:
    engine = make_engine(settings.database_url)
    cost_tracker = CostTracker(
        settings.cost_ceiling_usd,
        per_case_alert_usd=settings.per_case_cost_alert_usd,
        telemetry=telemetry,
    )
    processor = CaseProcessor(
        make_session_factory(engine),
        build_agent(settings, telemetry),
        default_policy(settings.auto_threshold),
        telemetry=telemetry,
        cost_tracker=cost_tracker,
    )
    dlq = KafkaDlqPublisher(
        Producer({**settings.kafka_client_config(), "enable.idempotence": True}),
        settings.kafka_dlq_topic,
    )
    kafka = Consumer(
        {
            **settings.kafka_client_config(),
            "group.id": settings.kafka_group_id,
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
            # investigations can legitimately block polling (backpressure); don't get kicked out
            "max.poll.interval.ms": settings.kafka_max_poll_interval_ms,
        }
    )
    consumer = CaseConsumer(
        cast(ConsumerLike, kafka),
        settings.kafka_topic,
        EventIntake(processor, dlq),
        dlq,
        processor,
        max_inflight=settings.max_inflight_cases,
        drain_timeout_s=settings.shutdown_grace_s,
    )
    return App(consumer=consumer, engine=engine, telemetry=telemetry, cost_tracker=cost_tracker)


async def _report_gauges(app: App) -> None:
    """Refresh the queue-depth / pending-approvals gauges from the database."""
    sessions = make_session_factory(app.engine)
    while True:
        try:
            async with sessions() as session:
                m = await compute_metrics(session)
            app.telemetry.set_gauges(
                queue_depth=m.queue_depth, pending_approvals=m.pending_approvals
            )
            app.cost_tracker.sync(m.total_cost_usd)  # other replicas / restarts may have spent
        except Exception:  # noqa: BLE001 - metrics must never take the pipeline down
            log.warning("could not refresh queue gauges", exc_info=True)
        await asyncio.sleep(GAUGE_REFRESH_S)


async def amain() -> None:
    settings = get_settings()  # fails fast on missing/invalid config
    configure_logging(settings.log_level, json_logs=settings.log_json)
    telemetry = setup_telemetry(
        "settleagent-agent",
        settings.otel_exporter_otlp_endpoint,
        enabled=settings.otel_enabled,
        metric_interval_ms=settings.otel_metric_interval_ms,
    )
    app = build_app(settings, telemetry)
    async with make_session_factory(app.engine)() as session:
        app.cost_tracker.seed(
            await total_cost(session)
        )  # spend to date, so restarts keep the budget
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, app.consumer.stop)
    reporter = asyncio.create_task(_report_gauges(app))
    log.info(
        "settleagent started",
        extra={
            "topic": settings.kafka_topic,
            "model": settings.anthropic_model,
            "otel": settings.otel_enabled,
            "spend_to_date_usd": round(app.cost_tracker.total_usd, 6),
        },
    )
    try:
        await app.consumer.run()
    finally:
        reporter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reporter
        await app.engine.dispose()
        telemetry.shutdown()  # flush spans/metrics
        log.info("settleagent stopped")


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
