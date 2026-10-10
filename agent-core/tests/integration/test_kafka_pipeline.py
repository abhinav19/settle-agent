"""End to end: replay producer -> real Kafka -> consumer -> agent (oracle LLM, real MCP tools)
-> gate -> Postgres. Covers idempotency, poison/DLQ, graceful shutdown and crash recovery."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

import pytest
from confluent_kafka import Consumer, Producer, TopicPartition
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from settleagent.agent.graph import InvestigationAgent
from settleagent.agent.nodes import AgentConfig
from settleagent.consumer.dlq import KafkaDlqPublisher
from settleagent.consumer.kafka import CaseConsumer
from settleagent.consumer.processor import CaseProcessor
from settleagent.consumer.service import EventIntake
from settleagent.domain.case import CaseStatus
from settleagent.gate.policy import default_policy
from settleagent.persistence.repositories import AuditRepository, CaseRepository
from settleagent.replay.producer import publish, publish_poison
from tests.doubles import DatasetOracleLlm, fresh_event, pick, real_gateway

pytestmark = pytest.mark.integration


class Pipeline:
    def __init__(self, bootstrap: str, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.bootstrap = bootstrap
        self.sessions = sessions
        self.topic = f"settlement.failures.{uuid4().hex[:8]}"
        self.dlq_topic = f"{self.topic}.dlq"
        self.group = f"g-{uuid4().hex[:8]}"
        self.producer = Producer({"bootstrap.servers": bootstrap})
        self.processor = CaseProcessor(
            sessions,
            InvestigationAgent(DatasetOracleLlm(), real_gateway(), AgentConfig()),
            default_policy(0.9),
        )

    def consumer(self) -> CaseConsumer:
        kafka = Consumer(
            {
                "bootstrap.servers": self.bootstrap,
                "group.id": self.group,
                "enable.auto.commit": False,
                "auto.offset.reset": "earliest",
            }
        )
        dlq = KafkaDlqPublisher(Producer({"bootstrap.servers": self.bootstrap}), self.dlq_topic)
        return CaseConsumer(
            kafka,  # type: ignore[arg-type]
            self.topic,
            EventIntake(self.processor, dlq),
            dlq,
            self.processor,
            poll_timeout_s=0.2,
            backoff_s=0.05,
        )

    def send(self, scenarios: list[Any]) -> list[Any]:
        events = [fresh_event(s) for s in scenarios]
        publish(self.producer, self.topic, [e.model_dump(mode="json") for e in events])
        return events

    async def status(self, event_id: Any) -> CaseStatus | None:
        async with self.sessions() as s:
            from sqlalchemy import select

            from settleagent.persistence.models import CaseRow

            row = (
                await s.execute(select(CaseRow).where(CaseRow.event_id == event_id))
            ).scalar_one_or_none()
            return CaseStatus(row.status) if row else None

    def committed_offset(self) -> int:
        c = Consumer({"bootstrap.servers": self.bootstrap, "group.id": self.group})
        try:
            [tp] = c.committed([TopicPartition(self.topic, 0)], timeout=10)
            return int(tp.offset)
        finally:
            c.close()

    def read_dlq(self, expected: int, timeout_s: float = 15.0) -> list[Any]:
        c = Consumer(
            {
                "bootstrap.servers": self.bootstrap,
                "group.id": f"dlq-{uuid4().hex[:6]}",
                "auto.offset.reset": "earliest",
            }
        )
        c.subscribe([self.dlq_topic])
        out: list[Any] = []
        import time

        deadline = time.monotonic() + timeout_s
        while len(out) < expected and time.monotonic() < deadline:
            m = c.poll(0.5)
            if m is not None and m.error() is None:
                out.append(m)
        c.close()
        return out


@pytest.fixture
def pipeline(kafka_bootstrap: str, sessions: async_sessionmaker[AsyncSession]) -> Pipeline:
    return Pipeline(kafka_bootstrap, sessions)


async def wait_for(cond: Callable[[], Awaitable[bool]], timeout_s: float = 60.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if await cond():
            return
        await asyncio.sleep(0.25)
    raise AssertionError("condition not met in time")


async def run_consumer(consumer: CaseConsumer) -> asyncio.Task[None]:
    return asyncio.create_task(consumer.run())


async def test_end_to_end_routing_dedupe_poison_and_graceful_shutdown(pipeline: Pipeline) -> None:
    scenarios = [
        pick("clear", "SSIM"),  # write fix -> AWAITING_APPROVAL
        pick("clear", "CALD"),  # read-only fix -> AUTO_RESOLVED
        pick("ambiguous", "LACK"),  # -> ESCALATED
        pick("underdetermined", "MONY"),  # -> ESCALATED
    ]
    expected = [
        CaseStatus.AWAITING_APPROVAL,
        CaseStatus.AUTO_RESOLVED,
        CaseStatus.ESCALATED,
        CaseStatus.ESCALATED,
    ]
    events = pipeline.send(scenarios)
    publish(pipeline.producer, pipeline.topic, [events[0].model_dump(mode="json")])  # duplicate
    publish_poison(pipeline.producer, pipeline.topic, 2)
    pipeline.send([pick("clear", "NMAT")])  # arrives AFTER the poison: the pipeline keeps running

    consumer = pipeline.consumer()
    task = await run_consumer(consumer)

    async def all_terminal() -> bool:
        statuses = [await pipeline.status(e.event_id) for e in events]
        return statuses == expected

    await wait_for(all_terminal)

    # idempotency: the duplicate created no second case and no second audit trail
    async with pipeline.sessions() as s:
        first = await CaseRepository(s).list_by_statuses([CaseStatus.AWAITING_APPROVAL], 5000)
        case = next(c for c in first if c.event_id == events[0].event_id)
        audit = await AuditRepository(s).list_for_case(case.id)
    assert [a.action for a in audit].count("case.received") == 1

    # poison messages are in the DLQ with the original bytes and the validation error
    dlq = pipeline.read_dlq(expected=2)
    assert len(dlq) == 2
    headers = dict(dlq[0].headers())
    assert headers["dlq.error"], "DLQ record must carry the validation error"
    assert headers["dlq.source_topic"] == pipeline.topic.encode()
    assert dlq[0].value() in {b"this is not json", json.dumps({"schema_version": "9.9"}).encode()}

    # graceful shutdown drains and commits every offset that was processed
    consumer.stop()
    await asyncio.wait_for(task, timeout=30)
    assert pipeline.committed_offset() == 4 + 1 + 2 + 1  # events + duplicate + poison + late event


async def test_restart_does_not_reprocess_and_recovers_unfinished_cases(pipeline: Pipeline) -> None:
    # A case that was persisted but never investigated (simulates a crash after the commit).
    stranded_event = fresh_event(pick("clear", "CALD"))
    stranded, created = await pipeline.processor.intake(stranded_event)
    assert created

    events = pipeline.send([pick("clear", "CALD")])
    consumer = pipeline.consumer()
    task = await run_consumer(consumer)

    async def both_done() -> bool:
        return (
            await pipeline.status(events[0].event_id) is CaseStatus.AUTO_RESOLVED
            and await pipeline.status(stranded_event.event_id) is CaseStatus.AUTO_RESOLVED
        )

    await wait_for(both_done)
    consumer.stop()
    await asyncio.wait_for(task, timeout=30)

    # A restarted consumer in the same group finds nothing new to do.
    second = pipeline.consumer()
    task2 = await run_consumer(second)
    await asyncio.sleep(3)
    second.stop()
    await asyncio.wait_for(task2, timeout=30)
    async with pipeline.sessions() as s:
        audit = await AuditRepository(s).list_for_case(stranded.id)
    assert [a.action for a in audit].count("gate.decision") == 1
