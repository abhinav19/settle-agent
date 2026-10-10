"""Load/smoke (spec 9.5): stream N events through real Kafka + Postgres and check the system
behaves under pressure — throughput, bounded backlog, no unhandled errors, cost ceiling held."""

import asyncio
import logging
import time
import traceback
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from confluent_kafka import Consumer, Producer, TopicPartition
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from settleagent.agent.graph import InvestigationAgent
from settleagent.agent.nodes import AgentConfig
from settleagent.agent.ratelimit import RateLimitedLlm
from settleagent.consumer.dlq import KafkaDlqPublisher
from settleagent.consumer.kafka import CaseConsumer, ConsumerLike
from settleagent.consumer.processor import CaseProcessor
from settleagent.consumer.service import EventIntake
from settleagent.gate.policy import default_policy
from settleagent.observability.cost import CostTracker
from settleagent.observability.pricing import cost_usd
from settleagent.persistence.models import AuditEventRow, CaseRow
from settleagent.replay.producer import publish
from tests.doubles import DATASET, DatasetOracleLlm, real_gateway

pytestmark = pytest.mark.integration

N_EVENTS = 150
MAX_INFLIGHT = 8
MODEL = "claude-opus-5-5"
# DatasetOracleLlm reports 50 in / 10 out tokens per call and the agent makes 3 calls per case
PER_CASE = cost_usd(MODEL, 3 * 50, 3 * 10)
MIN_THROUGHPUT = 5.0  # cases/second — an order of magnitude below what this rig actually does


class CountingOracle(DatasetOracleLlm):
    """The oracle, priced as a real model so cost accrues; counts calls and peak concurrency."""

    def __init__(self) -> None:
        super().__init__()
        self.model = MODEL
        self.calls = 0

    async def complete_json(self, **kw: Any) -> Any:
        self.calls += 1
        await asyncio.sleep(0.002)  # a little latency, so concurrency is actually exercised
        return await super().complete_json(**kw)


def events(n: int) -> list[dict[str, str]]:
    """n synthetic failures drawn from the dataset, each with a fresh event id."""
    scenarios = [
        s for s in DATASET.scenarios if s.difficulty != "underdetermined" or "absent" not in s.note
    ]
    return [{**scenarios[i % len(scenarios)].event, "event_id": str(uuid4())} for i in range(n)]


async def settled_counts(
    sessions: async_sessionmaker[AsyncSession], ids: list[str]
) -> tuple[int, int]:
    async with sessions() as s:
        rows = (
            await s.execute(
                select(CaseRow.status, func.count())
                .where(CaseRow.event_id.in_([UUID(i) for i in ids]))
                .group_by(CaseRow.status)
            )
        ).all()
    by = {status: int(n) for status, n in rows}
    pending = by.get("RECEIVED", 0) + by.get("INVESTIGATING", 0)
    return sum(by.values()), pending


async def test_streaming_load_is_bounded_correct_and_respects_the_cost_ceiling(
    kafka_bootstrap: str,
    isolated_sessions: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A private database: startup recovery re-dispatches every incomplete case it finds, so a
    # shared one would drag in strays left by other tests.
    sessions = isolated_sessions
    topic, dlq_topic, group = (
        f"smoke.{uuid4().hex[:8]}",
        f"smoke.dlq.{uuid4().hex[:8]}",
        f"g-{uuid4().hex[:6]}",
    )
    ceiling = PER_CASE * 60  # the budget runs out after roughly 60 of the 150 cases
    llm = CountingOracle()
    limited = RateLimitedLlm(llm, max_concurrent=4, requests_per_minute=0)  # the production wrapper
    tracker = CostTracker(ceiling, per_case_alert_usd=100)
    processor = CaseProcessor(
        sessions,
        InvestigationAgent(limited, real_gateway(), AgentConfig()),
        default_policy(0.9),
        cost_tracker=tracker,
    )
    dlq = KafkaDlqPublisher(Producer({"bootstrap.servers": kafka_bootstrap}), dlq_topic)
    kafka = Consumer(
        {
            "bootstrap.servers": kafka_bootstrap,
            "group.id": group,
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
        }
    )
    consumer = CaseConsumer(
        cast(ConsumerLike, kafka),
        topic,
        EventIntake(processor, dlq),
        dlq,
        processor,
        max_inflight=MAX_INFLIGHT,
        poll_timeout_s=0.1,
        drain_timeout_s=30,
    )

    batch = events(N_EVENTS)
    ids = [e["event_id"] for e in batch]
    started = time.monotonic()
    publish(Producer({"bootstrap.servers": kafka_bootstrap}), topic, batch)
    # a duplicate burst and a few poison messages in the middle of the stream
    publish(Producer({"bootstrap.servers": kafka_bootstrap}), topic, batch[:10])

    with caplog.at_level(logging.INFO):
        runner = asyncio.create_task(consumer.run())
        peak_backlog = 0
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            seen, pending = await settled_counts(sessions, ids)
            peak_backlog = max(peak_backlog, pending)
            duplicates = sum(r.getMessage() == "duplicate event ignored" for r in caplog.records)
            if seen == N_EVENTS and pending == 0 and duplicates >= 10:
                break  # all originals settled AND the trailing duplicates were consumed
            await asyncio.sleep(0.05)
        elapsed = time.monotonic() - started
        consumer.stop()
        await asyncio.wait_for(runner, timeout=60)

    # ---- correctness: exactly one case per event, all settled
    seen, pending = await settled_counts(sessions, ids)
    assert (seen, pending) == (N_EVENTS, 0), "every event became exactly one settled case"

    # ---- throughput
    throughput = N_EVENTS / elapsed
    print(
        f"\nsmoke: {N_EVENTS} events in {elapsed:.1f}s = {throughput:.1f} cases/s, "
        f"peak in-flight {consumer.peak_inflight}, peak backlog {peak_backlog}"
    )
    assert throughput >= MIN_THROUGHPUT

    # ---- bounded growth: in-flight work and the unprocessed backlog never ran away
    assert consumer.peak_inflight <= MAX_INFLIGHT
    assert limited.peak_in_flight <= 4  # the LLM concurrency cap held under load
    assert peak_backlog <= MAX_INFLIGHT + 2  # intake blocks when workers are saturated

    # ---- no unhandled errors from our own code
    ours = [
        r for r in caplog.records if r.name.startswith("settleagent") and r.levelno >= logging.ERROR
    ]
    assert ours == [], [
        (r.getMessage(), "".join(traceback.format_exception(*r.exc_info)) if r.exc_info else "")
        for r in ours
    ]

    # ---- the cost ceiling held
    async with sessions() as s:
        blocked = (
            await s.execute(
                select(func.count(func.distinct(AuditEventRow.case_id))).where(
                    AuditEventRow.action == "budget.ceiling_reached",
                    AuditEventRow.case_id.in_(
                        select(CaseRow.id).where(CaseRow.event_id.in_([UUID(i) for i in ids]))
                    ),
                )
            )
        ).scalar_one()
        spent = float(
            (
                await s.execute(
                    text(
                        "SELECT COALESCE(SUM((a.payload->>'cost_usd')::float), 0) "
                        "FROM audit_events a JOIN cases c ON c.id = a.case_id "
                        "WHERE a.action = 'investigation.completed' AND c.event_id = ANY(:ids)"
                    ),
                    {"ids": [UUID(i) for i in ids]},
                )
            ).scalar_one()
        )
    investigated = N_EVENTS - blocked
    assert blocked > 0, "the ceiling was never reached; the test is not exercising enforcement"
    assert llm.calls == investigated * 3, "blocked cases made no LLM calls at all"
    # overshoot is bounded by what was already in flight when the ceiling was crossed
    assert spent <= ceiling + MAX_INFLIGHT * PER_CASE + 1e-9
    assert spent == pytest.approx(investigated * PER_CASE)

    # ---- delivery: every offset (events + the 10 duplicates) was committed
    c = Consumer({"bootstrap.servers": kafka_bootstrap, "group.id": group})
    try:
        [tp] = c.committed([TopicPartition(topic, 0)], timeout=10)
    finally:
        c.close()
    assert tp.offset == N_EVENTS + 10
