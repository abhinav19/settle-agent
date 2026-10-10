"""Dead-letter publishing. The original bytes are preserved; the error travels in headers."""

import asyncio
from typing import Protocol

from confluent_kafka import KafkaError, Producer

from settleagent.consumer.messages import IncomingMessage


class DlqPublishError(RuntimeError):
    """The DLQ write failed. The source offset must NOT be committed."""


class DlqPublisher(Protocol):
    async def publish(self, msg: IncomingMessage, error: str) -> None: ...


class KafkaDlqPublisher:
    def __init__(self, producer: Producer, topic: str, flush_timeout_s: float = 10.0) -> None:
        self._producer = producer
        self._topic = topic
        self._timeout = flush_timeout_s

    async def publish(self, msg: IncomingMessage, error: str) -> None:
        failure: list[KafkaError] = []

        def on_delivery(err: KafkaError | None, _record: object) -> None:
            if err is not None:
                failure.append(err)

        headers: list[tuple[str, str | bytes | None]] = [
            ("dlq.error", error[:2000].encode()),
            ("dlq.source_topic", msg.topic.encode()),
            ("dlq.source_partition", str(msg.partition).encode()),
            ("dlq.source_offset", str(msg.offset).encode()),
        ]

        def produce_and_flush() -> int:
            self._producer.produce(
                self._topic, key=msg.key, value=msg.value, headers=headers, on_delivery=on_delivery
            )
            return self._producer.flush(self._timeout)

        remaining = await asyncio.to_thread(produce_and_flush)
        if remaining or failure:
            raise DlqPublishError(f"DLQ publish failed: {failure[0] if failure else 'timeout'}")
