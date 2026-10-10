"""Python replay producer: publishes synthetic failure events to Kafka.

A stand-in for the Spring Boot producer (same topic, key and JSON contract) so the pipeline
can be exercised end to end before the Java service exists, and so tests and the eval harness
can replay deterministic fixtures. Reads the scenarios from ``data/synthetic/dataset.json``.

    python -m settleagent.replay.producer --limit 20
    python -m settleagent.replay.producer --limit 5 --poison 2
"""

import argparse
import json
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from confluent_kafka import Producer

DEFAULT_DATASET = Path(__file__).resolve().parents[4] / "data" / "synthetic" / "dataset.json"


def load_events(path: Path, limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
    scenarios = json.loads(path.read_text(encoding="utf-8"))["scenarios"]
    events = [s["event"] for s in scenarios][offset:]
    return events[:limit] if limit is not None else events


def publish(
    producer: Producer,
    topic: str,
    events: Iterable[dict[str, Any]],
    *,
    rate_per_s: float | None = None,
    flush_timeout_s: float = 30.0,
) -> int:
    """Publish events keyed by ``trade_id`` (ordering per trade). Returns the count sent."""
    errors: list[Any] = []

    def on_delivery(err: Any, _msg: Any) -> None:
        if err is not None:
            errors.append(err)

    sent = 0
    for event in events:
        producer.produce(
            topic,
            key=event["trade_id"].encode(),
            value=json.dumps(event).encode(),
            on_delivery=on_delivery,
        )
        producer.poll(0)
        sent += 1
        if rate_per_s:
            time.sleep(1.0 / rate_per_s)
    remaining = producer.flush(flush_timeout_s)
    if remaining or errors:
        raise RuntimeError(f"publish incomplete: {remaining} undelivered, errors={errors[:3]}")
    return sent


def publish_poison(producer: Producer, topic: str, count: int) -> None:
    """Send malformed messages (for demos and DLQ tests)."""
    bodies = [b"this is not json", json.dumps({"schema_version": "9.9"}).encode(), b""]
    for i in range(count):
        producer.produce(topic, key=f"poison-{i}".encode(), value=bodies[i % len(bodies)])
    producer.flush(10)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay synthetic settlement failures to Kafka")
    parser.add_argument("--bootstrap", default="localhost:9092")
    parser.add_argument("--topic", default="settlement.failures")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--rate", type=float, default=None, help="events per second (stream mode)")
    parser.add_argument("--poison", type=int, default=0, help="also send N malformed messages")
    args = parser.parse_args(argv)

    producer = Producer({"bootstrap.servers": args.bootstrap, "enable.idempotence": True})
    events = load_events(args.dataset, args.limit, args.offset)
    sent = publish(producer, args.topic, events, rate_per_s=args.rate)
    if args.poison:
        publish_poison(producer, args.topic, args.poison)
    print(f"published {sent} event(s) and {args.poison} poison message(s) to {args.topic}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
