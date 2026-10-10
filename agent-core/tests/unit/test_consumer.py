"""Consumer behaviour with a fake Kafka client and fake processor (no I/O)."""

import asyncio
import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from confluent_kafka import KafkaError

from settleagent.consumer.dlq import DlqPublishError
from settleagent.consumer.kafka import CaseConsumer
from settleagent.consumer.messages import IncomingMessage, PoisonMessageError, parse_event
from settleagent.consumer.service import EventIntake, IntakeKind
from settleagent.domain.case import Case, CaseStatus
from tests.factories import NOW, event_payload, make_event


class FakeMsg:
    def __init__(
        self, value: bytes | None, offset: int, key: bytes | None = b"T-1", err: Any = None
    ):
        self._v, self._o, self._k, self._e = value, offset, key, err

    def value(self) -> bytes | None:
        return self._v  # noqa: E704

    def key(self) -> bytes | None:
        return self._k  # noqa: E704

    def topic(self) -> str:
        return "settlement.failures"  # noqa: E704

    def partition(self) -> int:
        return 0  # noqa: E704

    def offset(self) -> int:
        return self._o  # noqa: E704

    def error(self) -> Any:
        return self._e  # noqa: E704


def valid_msg(offset: int, **over: Any) -> FakeMsg:
    return FakeMsg(json.dumps(event_payload(event_id=str(uuid4()), **over)).encode(), offset)


class FakeKafka:
    """Yields scripted messages, then None; records the order of commits and the close."""

    def __init__(self, messages: list[Any], on_empty: Any = None) -> None:
        self.messages = list(messages)
        self.committed: list[int] = []
        self.closed = False
        self.on_empty = on_empty
        self.log: list[str] = []

    def subscribe(self, topics: list[str]) -> None:
        self.log.append(f"subscribe:{topics}")

    def poll(self, timeout: float) -> Any:
        if self.messages:
            return self.messages.pop(0)
        if self.on_empty:
            self.on_empty()
        return None

    def commit(self, *, message: Any, asynchronous: bool) -> None:
        assert asynchronous is False, "commits must be synchronous"
        self.committed.append(message.offset())
        self.log.append(f"commit:{message.offset()}")

    def close(self) -> None:
        self.closed = True


class FakeDlq:
    def __init__(self, fail: bool = False) -> None:
        self.items: list[tuple[IncomingMessage, str]] = []
        self.fail = fail

    async def publish(self, msg: IncomingMessage, error: str) -> None:
        if self.fail:
            raise DlqPublishError("dlq down")
        self.items.append((msg, error))


class FakeProcessor:
    def __init__(self) -> None:
        self.seen: set[UUID] = set()
        self.investigated: list[UUID] = []
        self.fail_intake = 0
        self.intake_calls = 0
        self.incomplete: list[UUID] = []
        self.gate: asyncio.Event | None = None
        self.running = 0
        self.max_running = 0

    async def intake(self, event: Any) -> tuple[Case, bool]:
        self.intake_calls += 1
        if self.fail_intake > 0:
            self.fail_intake -= 1
            raise ConnectionError("db down")
        created = event.event_id not in self.seen
        self.seen.add(event.event_id)
        case = Case(
            id=uuid4(),
            event_id=event.event_id,
            status=CaseStatus.RECEIVED,
            event=event,
            created_at=NOW,
            updated_at=NOW,
        )
        return case, created

    async def investigate(self, case_id: UUID) -> None:
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        if self.gate is not None:
            await self.gate.wait()
        self.investigated.append(case_id)
        self.running -= 1

    async def incomplete_case_ids(self) -> list[UUID]:
        return self.incomplete


async def no_sleep(_: float) -> None:
    return None


def build(
    kafka: FakeKafka, proc: FakeProcessor, dlq: FakeDlq | None = None, **kw: Any
) -> CaseConsumer:
    d = dlq or FakeDlq()
    return CaseConsumer(
        kafka, "settlement.failures", EventIntake(proc, d), d, proc, sleep=no_sleep, **kw
    )


async def run_until_idle(consumer: CaseConsumer, kafka: FakeKafka) -> None:
    kafka.on_empty = consumer.stop
    await asyncio.wait_for(consumer.run(), timeout=5)


# ---------------------------------------------------------------- parsing
def test_parse_valid_event() -> None:
    ev = parse_event(json.dumps(event_payload()).encode())
    assert ev.trade_id == "T-0001"


@pytest.mark.parametrize(
    "body", [None, b"", b"not json", b"[]", json.dumps(event_payload(isin="BAD")).encode()]
)
def test_parse_rejects_poison(body: bytes | None) -> None:
    with pytest.raises(PoisonMessageError):
        parse_event(body)


def test_poison_error_names_the_offending_field() -> None:
    with pytest.raises(PoisonMessageError, match="isin"):
        parse_event(json.dumps(event_payload(isin="BAD")).encode())


# ------------------------------------------------------------------ intake
async def test_intake_classifies_created_duplicate_and_poison() -> None:
    proc, dlq = FakeProcessor(), FakeDlq()
    intake = EventIntake(proc, dlq)
    body = json.dumps(event_payload()).encode()
    msg = IncomingMessage("t", 0, 1, b"k", body)
    assert (await intake.handle(msg)).kind is IntakeKind.CREATED
    assert (await intake.handle(msg)).kind is IntakeKind.DUPLICATE
    bad = IncomingMessage("t", 0, 2, b"k", b"{")
    assert (await intake.handle(bad)).kind is IntakeKind.POISON
    assert len(dlq.items) == 1 and dlq.items[0][0] is bad


# ---------------------------------------------------------------- consumer
async def test_commit_happens_after_persist_and_before_investigation_completes() -> None:
    proc = FakeProcessor()
    proc.gate = asyncio.Event()
    kafka = FakeKafka([valid_msg(0)])
    consumer = build(kafka, proc)
    runner = asyncio.create_task(run_until_idle(consumer, kafka))
    await asyncio.sleep(0.05)
    assert kafka.committed == [0]  # committed although the investigation is still running
    assert proc.investigated == [] and consumer.inflight == 1
    proc.gate.set()
    await runner
    assert len(proc.investigated) == 1 and kafka.closed


async def test_every_valid_message_is_committed_and_investigated() -> None:
    proc = FakeProcessor()
    kafka = FakeKafka([valid_msg(i) for i in range(5)])
    await run_until_idle(build(kafka, proc), kafka)
    assert kafka.committed == [0, 1, 2, 3, 4] and len(proc.investigated) == 5


async def test_duplicate_message_is_committed_but_not_investigated_again() -> None:
    proc = FakeProcessor()
    m = valid_msg(0)
    dup = FakeMsg(m.value(), 1)
    kafka = FakeKafka([m, dup])
    await run_until_idle(build(kafka, proc), kafka)
    assert kafka.committed == [0, 1] and len(proc.investigated) == 1


async def test_poison_goes_to_dlq_then_commits_and_pipeline_keeps_running() -> None:
    proc, dlq = FakeProcessor(), FakeDlq()
    kafka = FakeKafka([FakeMsg(b"garbage", 0), valid_msg(1)])
    await run_until_idle(build(kafka, proc, dlq), kafka)
    assert len(dlq.items) == 1 and dlq.items[0][0].value == b"garbage"
    assert "json" in dlq.items[0][1].lower() or "validation" in dlq.items[0][1].lower()
    assert kafka.committed == [0, 1] and len(proc.investigated) == 1


async def test_transient_failure_is_retried_then_succeeds() -> None:
    proc = FakeProcessor()
    proc.fail_intake = 2
    kafka = FakeKafka([valid_msg(0)])
    await run_until_idle(build(kafka, proc, max_attempts=5), kafka)
    assert proc.intake_calls == 3 and len(proc.investigated) == 1


async def test_persistent_failure_is_parked_in_dlq_not_dropped_silently() -> None:
    proc, dlq = FakeProcessor(), FakeDlq()
    proc.fail_intake = 99
    kafka = FakeKafka([valid_msg(0), valid_msg(1)])
    proc_ok_after = proc

    def heal() -> None:
        proc_ok_after.fail_intake = 0

    consumer = build(kafka, proc, dlq, max_attempts=3)
    kafka.on_empty = consumer.stop
    # first message exhausts retries; heal before the second is polled
    orig_publish = dlq.publish

    async def publish_and_heal(msg: IncomingMessage, error: str) -> None:
        await orig_publish(msg, error)
        heal()

    dlq.publish = publish_and_heal  # type: ignore[method-assign]
    await asyncio.wait_for(consumer.run(), timeout=5)
    assert len(dlq.items) == 1 and "after 3 attempts" in dlq.items[0][1]
    assert kafka.committed == [0, 1] and len(proc.investigated) == 1


async def test_dlq_failure_does_not_commit_and_propagates() -> None:
    proc = FakeProcessor()
    kafka = FakeKafka([FakeMsg(b"garbage", 0)])
    consumer = build(kafka, proc, FakeDlq(fail=True))
    with pytest.raises(DlqPublishError):
        await asyncio.wait_for(consumer.run(), timeout=5)
    assert kafka.committed == [] and kafka.closed  # never acknowledged, consumer still closed


async def test_backpressure_bounds_concurrent_investigations() -> None:
    proc = FakeProcessor()
    proc.gate = asyncio.Event()
    kafka = FakeKafka([valid_msg(i) for i in range(6)])
    consumer = build(kafka, proc, max_inflight=2)
    runner = asyncio.create_task(run_until_idle(consumer, kafka))
    await asyncio.sleep(0.1)
    assert consumer.inflight == 2 and proc.max_running == 2
    assert len(kafka.committed) == 3  # the 3rd message is persisted, then dispatch blocks
    proc.gate.set()
    await runner
    assert len(proc.investigated) == 6 and proc.max_running == 2


async def test_graceful_shutdown_drains_inflight_work_before_closing() -> None:
    proc = FakeProcessor()
    proc.gate = asyncio.Event()
    kafka = FakeKafka([valid_msg(0)])
    consumer = build(kafka, proc)
    runner = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.05)
    consumer.stop()
    await asyncio.sleep(0.05)
    assert not kafka.closed and not runner.done()  # waiting for the in-flight case
    proc.gate.set()
    await asyncio.wait_for(runner, timeout=5)
    assert kafka.closed and len(proc.investigated) == 1


async def test_incomplete_cases_are_recovered_on_startup() -> None:
    proc = FakeProcessor()
    proc.incomplete = [uuid4(), uuid4()]
    kafka = FakeKafka([])
    await run_until_idle(build(kafka, proc), kafka)
    assert set(proc.investigated) == set(proc.incomplete)


async def test_kafka_error_events_are_skipped_but_fatal_ones_raise() -> None:
    class Err:
        def __init__(self, code: int, fatal: bool) -> None:
            self._c, self._f = code, fatal

        def code(self) -> int:
            return self._c  # noqa: E704

        def fatal(self) -> bool:
            return self._f  # noqa: E704

    proc = FakeProcessor()
    eof = FakeMsg(None, 0, err=Err(KafkaError._PARTITION_EOF, False))  # noqa: SLF001
    transient = FakeMsg(None, 1, err=Err(-195, False))
    kafka = FakeKafka([eof, transient, valid_msg(2)])
    await run_until_idle(build(kafka, proc), kafka)
    assert kafka.committed == [2]

    fatal = FakeKafka([FakeMsg(None, 0, err=Err(-1, True))])
    with pytest.raises(RuntimeError, match="fatal"):
        await asyncio.wait_for(build(fatal, proc).run(), timeout=5)
    assert fatal.closed


async def test_event_fixture_roundtrip_sanity() -> None:
    assert make_event().trade_id == "T-0001"
