"""Smoke-test a RUNNING stack (docker compose --profile app, or `make run`).

Publishes N synthetic failures to Kafka, waits for each to become a settled case (via the API),
and reports throughput and outcomes. Exits non-zero if anything fails to settle, a case FAILED,
or the stack is not ready. Needs no Anthropic key: without one, cases are escalated to a human
(the agent degrades gracefully), which still proves Kafka -> agent -> tools -> DB -> API wiring.

    uv run python scripts/smoke.py --count 30 --api-key $API_KEY
"""

import argparse
import json
import sys
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "data" / "synthetic" / "dataset.json"
SETTLED_NOT = {"RECEIVED", "INVESTIGATING"}


def fresh_events(path: Path, count: int) -> list[dict[str, Any]]:
    """The first ``count`` seeded failures, each re-issued with a new event id."""
    scenarios = json.loads(path.read_text(encoding="utf-8"))["scenarios"]
    return [{**s["event"], "event_id": str(uuid4())} for s in scenarios[:count]]


def check_ready(client: httpx.Client) -> dict[str, Any]:
    response = client.get("/ready")
    body: dict[str, Any] = response.json()
    if response.status_code != 200:
        failing = {k: v["detail"] for k, v in body["checks"].items() if not v["ok"]}
        raise SystemExit(f"stack is not ready: {failing}")
    return body


def wait_for_cases(
    client: httpx.Client,
    event_ids: set[str],
    *,
    timeout_s: float,
    poll_s: float = 0.5,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, str]:
    """Poll the API until every event has a settled case or the timeout hits.

    Returns {event_id: status} for the cases seen (settled or not).
    """
    deadline = clock() + timeout_s
    seen: dict[str, str] = {}
    while True:
        page = client.get(
            "/cases", params={"limit": 200, "sort": "created_at", "order": "desc"}
        ).json()
        for case in page["items"]:
            if case["event_id"] in event_ids:
                seen[case["event_id"]] = case["status"]
        settled = {e for e, s in seen.items() if s not in SETTLED_NOT}
        if settled == event_ids or clock() >= deadline:
            return seen
        sleep(poll_s)


def run(
    args: argparse.Namespace,
    publish: Callable[[list[dict[str, Any]]], None],
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    events = fresh_events(args.dataset, args.count)
    ids = {e["event_id"] for e in events}
    headers = {"X-API-Key": args.api_key}
    with httpx.Client(
        base_url=args.api_url, headers=headers, timeout=15, transport=args.transport
    ) as client:
        ready = check_ready(client)
        print(
            "ready:",
            ", ".join(f"{k}={'ok' if v['ok'] else 'DOWN'}" for k, v in ready["checks"].items()),
        )
        before = client.get("/metrics/summary").json()

        started = clock()
        publish(events)
        seen = wait_for_cases(client, ids, timeout_s=args.timeout, clock=clock, sleep=sleep)
        elapsed = max(clock() - started, 1e-9)
        after = client.get("/metrics/summary").json()

    settled = {e: s for e, s in seen.items() if s not in SETTLED_NOT}
    outcomes = Counter(settled.values())
    spent = after["total_cost_usd"] - before["total_cost_usd"]
    print(
        f"published {len(events)}; settled {len(settled)} in {elapsed:.1f}s "
        f"({len(settled) / elapsed:.1f} cases/s)"
    )
    print("outcomes:", dict(sorted(outcomes.items())))
    print(f"spend during smoke: ${spent:.4f}; budget level: {after['budget']['level']}")

    problems = []
    if len(settled) < len(ids):
        problems.append(f"{len(ids) - len(settled)} event(s) did not settle within {args.timeout}s")
    if outcomes.get("FAILED"):
        problems.append(f"{outcomes['FAILED']} case(s) FAILED (an agent crash)")
    for problem in problems:
        print("SMOKE FAILED:", problem)
    return 1 if problems else 0


def kafka_publisher(bootstrap: str, topic: str) -> Callable[[list[dict[str, Any]]], None]:
    from confluent_kafka import Producer

    from settleagent.replay.producer import publish

    def go(events: list[dict[str, Any]]) -> None:
        publish(Producer({"bootstrap.servers": bootstrap}), topic, events)

    return go


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--api-url", default="http://localhost:8000")
    p.add_argument("--api-key", required=True)
    p.add_argument("--bootstrap", default="localhost:9092")
    p.add_argument("--topic", default="settlement.failures")
    p.add_argument("--count", type=int, default=20)
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--dataset", type=Path, default=DATASET)
    args = p.parse_args(argv)
    args.transport = None
    return run(args, kafka_publisher(args.bootstrap, args.topic))


if __name__ == "__main__":
    sys.exit(main())
