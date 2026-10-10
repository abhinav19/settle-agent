"""Fakes for the Kafka consumer tests (shared by test_consumer and test_hardening)."""

import asyncio
import json
from typing import Any
from uuid import UUID, uuid4

from settleagent.consumer.dlq import DlqPublishError
from settleagent.consumer.kafka import CaseConsumer
from settleagent.consumer.messages import IncomingMessage
from settleagent.consumer.service import EventIntake
from settleagent.domain.case import Case, CaseStatus
from tests.factories import NOW, event_payload


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
