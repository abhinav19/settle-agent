import json
from pathlib import Path
from typing import Any

import pytest

from settleagent.replay.producer import DEFAULT_DATASET, load_events, publish, publish_poison


class FakeProducer:
    def __init__(self, fail: bool = False, undelivered: int = 0) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail, self.undelivered = fail, undelivered

    def produce(
        self, topic: str, key: bytes, value: bytes | None = None, on_delivery: Any = None
    ) -> None:
        self.sent.append({"topic": topic, "key": key, "value": value})
        if on_delivery and self.fail:
            on_delivery("broker said no", None)

    def poll(self, timeout: float) -> None:
        return None

    def flush(self, timeout: float) -> int:
        return self.undelivered


@pytest.mark.skipif(not DEFAULT_DATASET.exists(), reason="run `make seed`")
def test_load_events_limit_and_offset() -> None:
    first = load_events(DEFAULT_DATASET, limit=3)
    shifted = load_events(DEFAULT_DATASET, limit=3, offset=1)
    assert len(first) == 3 and first[1] == shifted[0]


def test_publish_keys_by_trade_id_and_sends_json(tmp_path: Path) -> None:
    producer = FakeProducer()
    events = [{"trade_id": "T-1", "x": 1}, {"trade_id": "T-2", "x": 2}]
    assert publish(producer, "topic", events) == 2  # type: ignore[arg-type]
    assert [m["key"] for m in producer.sent] == [b"T-1", b"T-2"]
    assert json.loads(producer.sent[0]["value"]) == events[0]


def test_publish_raises_on_delivery_failure() -> None:
    with pytest.raises(RuntimeError, match="incomplete"):
        publish(FakeProducer(fail=True), "t", [{"trade_id": "T"}])  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="undelivered"):
        publish(FakeProducer(undelivered=1), "t", [{"trade_id": "T"}])  # type: ignore[arg-type]


def test_poison_messages_are_malformed() -> None:
    producer = FakeProducer()
    publish_poison(producer, "t", 3)  # type: ignore[arg-type]
    assert len(producer.sent) == 3
    assert producer.sent[0]["value"] == b"this is not json"
