"""Process entrypoint: wires config, Kafka, the agent graph and the case processor.

uv run python -m settleagent.main
"""

import asyncio
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
from settleagent.config import Settings, get_settings
from settleagent.consumer.dlq import KafkaDlqPublisher
from settleagent.consumer.kafka import CaseConsumer, ConsumerLike
from settleagent.consumer.processor import CaseProcessor
from settleagent.consumer.service import EventIntake
from settleagent.domain.reason_codes import ToolName
from settleagent.gate.policy import default_policy
from settleagent.persistence.db import make_engine, make_session_factory
from settleagent.tools.gateway import ClientFactory, McpToolGateway

log = logging.getLogger("settleagent")


@dataclass
class App:
    consumer: CaseConsumer
    engine: AsyncEngine


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


def build_agent(settings: Settings) -> InvestigationAgent:
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
    return InvestigationAgent(
        llm,
        build_gateway(settings),
        AgentConfig(
            max_steps=settings.max_steps,
            max_analyze_rounds=settings.max_analyze_rounds,
            degraded_confidence_cap=settings.degraded_confidence_cap,
        ),
    )


def build_app(settings: Settings) -> App:
    engine = make_engine(settings.database_url)
    processor = CaseProcessor(
        make_session_factory(engine),
        build_agent(settings),
        default_policy(settings.auto_threshold),
    )
    dlq = KafkaDlqPublisher(
        Producer(
            {"bootstrap.servers": settings.kafka_bootstrap_servers, "enable.idempotence": True}
        ),
        settings.kafka_dlq_topic,
    )
    kafka = Consumer(
        {
            "bootstrap.servers": settings.kafka_bootstrap_servers,
            "group.id": settings.kafka_group_id,
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
        }
    )
    consumer = CaseConsumer(
        cast(ConsumerLike, kafka),
        settings.kafka_topic,
        EventIntake(processor, dlq),
        dlq,
        processor,
        max_inflight=settings.max_inflight_cases,
    )
    return App(consumer=consumer, engine=engine)


async def amain() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()  # fails fast on missing/invalid config
    app = build_app(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, app.consumer.stop)
    log.info(
        "settleagent started (topic=%s, model=%s)", settings.kafka_topic, settings.anthropic_model
    )
    try:
        await app.consumer.run()
    finally:
        await app.engine.dispose()
        log.info("settleagent stopped")


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
