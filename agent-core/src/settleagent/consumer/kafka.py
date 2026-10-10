"""Kafka consumer loop: at-least-once, manual commit AFTER the case is durably persisted.

- Valid event  -> case persisted -> offset committed -> investigation dispatched (bounded).
- Poison event -> DLQ (with the validation error) -> offset committed.
- Transient failure while persisting -> bounded retries with backoff; if still failing, the
  message goes to the DLQ so one bad record cannot block the partition.
- If the DLQ write itself fails the offset is NOT committed and the error propagates.
- Backpressure: dispatch blocks when ``max_inflight`` investigations are running.
- Graceful shutdown: stop polling, let in-flight investigations finish, close the consumer.
- Investigations that were in flight at a crash are recovered at startup from the database.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from uuid import UUID

from confluent_kafka import KafkaError

from settleagent.consumer.dlq import DlqPublisher
from settleagent.consumer.messages import IncomingMessage
from settleagent.consumer.service import EventIntake, IntakeKind

log = logging.getLogger(__name__)


class ConsumerLike(Protocol):
    def subscribe(self, topics: list[str]) -> None: ...
    def poll(self, timeout: float) -> Any: ...
    def commit(self, *, message: Any, asynchronous: bool) -> Any: ...
    def close(self) -> None: ...


class CaseRunner(Protocol):
    async def investigate(self, case_id: UUID) -> Any: ...
    async def incomplete_case_ids(self) -> list[UUID]: ...


class CaseConsumer:
    def __init__(
        self,
        consumer: ConsumerLike,
        topic: str,
        intake: EventIntake,
        dlq: DlqPublisher,
        runner: CaseRunner,
        *,
        max_inflight: int = 8,
        max_attempts: int = 5,
        backoff_s: float = 1.0,
        poll_timeout_s: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._consumer = consumer
        self._topic = topic
        self._intake = intake
        self._dlq = dlq
        self._runner = runner
        self._max_attempts = max_attempts
        self._backoff = backoff_s
        self._poll_timeout = poll_timeout_s
        self._sleep = sleep
        self._slots = asyncio.Semaphore(max_inflight)
        self._tasks: set[asyncio.Task[None]] = set()
        self._stop = asyncio.Event()

    def stop(self) -> None:
        """Request a graceful shutdown (safe to call from a signal handler)."""
        self._stop.set()

    @property
    def inflight(self) -> int:
        return len(self._tasks)

    async def run(self) -> None:
        self._consumer.subscribe([self._topic])
        try:
            await self._recover()
            while not self._stop.is_set():
                msg = await asyncio.to_thread(self._consumer.poll, self._poll_timeout)
                if msg is None:
                    continue
                err = msg.error()
                if err is not None:
                    if err.code() == KafkaError._PARTITION_EOF:  # noqa: SLF001
                        continue
                    if err.fatal():
                        raise RuntimeError(f"fatal Kafka error: {err}")
                    log.error("kafka error: %s", err)
                    continue
                await self._process(msg)
        finally:
            await self._drain()
            await asyncio.to_thread(self._consumer.close)

    # ------------------------------------------------------------------ internals
    async def _process(self, msg: Any) -> None:
        incoming = IncomingMessage(
            topic=msg.topic(),
            partition=msg.partition(),
            offset=msg.offset(),
            key=msg.key(),
            value=msg.value(),
        )
        result = None
        last_error = ""
        for attempt in range(1, self._max_attempts + 1):
            try:
                result = await self._intake.handle(incoming)
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - classified as transient and retried
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "intake attempt %d/%d failed: %s", attempt, self._max_attempts, last_error
                )
                if attempt < self._max_attempts:
                    await self._sleep(min(self._backoff * 2 ** (attempt - 1), 30.0))
        if result is None:
            # Permanent after retries: park it so the partition keeps moving. If this raises,
            # nothing is committed and the process fails loudly instead of losing the event.
            await self._dlq.publish(
                incoming, f"processing failed after {self._max_attempts} attempts: {last_error}"
            )
        await asyncio.to_thread(self._consumer.commit, message=msg, asynchronous=False)
        if result is not None and result.kind is IntakeKind.CREATED and result.case_id:
            await self._dispatch(result.case_id)

    async def _recover(self) -> None:
        ids = await self._runner.incomplete_case_ids()
        if ids:
            log.info("recovering %d incomplete case(s)", len(ids))
        for case_id in ids:
            await self._dispatch(case_id)

    async def _dispatch(self, case_id: UUID) -> None:
        await self._slots.acquire()  # blocks polling when saturated -> backpressure
        task = asyncio.create_task(self._run_case(case_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_case(self, case_id: UUID) -> None:
        try:
            await self._runner.investigate(case_id)
        except Exception:
            log.exception("investigation of case %s raised", case_id)
        finally:
            self._slots.release()

    async def _drain(self) -> None:
        if self._tasks:
            log.info("draining %d in-flight case(s)", len(self._tasks))
            await asyncio.gather(*self._tasks, return_exceptions=True)
