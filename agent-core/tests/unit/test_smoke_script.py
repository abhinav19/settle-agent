"""scripts/smoke.py against a fake API (no Kafka, no stack)."""

import argparse
import importlib.util
from pathlib import Path
from typing import Any

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "smoke.py"
spec = importlib.util.spec_from_file_location("smoke_script", SCRIPT)
assert spec and spec.loader
smoke: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class FakeStack:
    """Pretends to be the control-plane API; cases settle after `polls_to_settle` list calls."""

    def __init__(
        self, polls_to_settle: int = 2, ready: bool = True, final: str = "ESCALATED"
    ) -> None:
        self.published: list[dict[str, Any]] = []
        self.polls = 0
        self.polls_to_settle, self.ready, self.final = polls_to_settle, ready, final
        self.cost = 0.0

    def publish(self, events: list[dict[str, Any]]) -> None:
        self.published = events

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "k"
        if request.url.path == "/ready":
            ok = {"ok": self.ready, "detail": "down" if not self.ready else "fine", "latency_ms": 1}
            return httpx.Response(
                200 if self.ready else 503, json={"status": "x", "checks": {"kafka": ok}}
            )
        if request.url.path == "/metrics/summary":
            return httpx.Response(
                200, json={"total_cost_usd": self.cost, "budget": {"level": "ok"}}
            )
        self.polls += 1
        status = self.final if self.polls >= self.polls_to_settle else "INVESTIGATING"
        items = [{"event_id": e["event_id"], "status": status} for e in self.published]
        return httpx.Response(200, json={"items": items})


def args_for(stack: FakeStack, **over: Any) -> argparse.Namespace:
    ns = argparse.Namespace(
        api_url="http://api",
        api_key="k",
        count=5,
        timeout=5.0,
        dataset=Path(__file__).resolve().parents[3] / "data" / "synthetic" / "dataset.json",
        transport=httpx.MockTransport(stack.handler),
    )
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def test_fresh_events_have_unique_new_ids_but_keep_the_scenario_content() -> None:
    a = smoke.fresh_events(args_for(FakeStack()).dataset, 5)
    b = smoke.fresh_events(args_for(FakeStack()).dataset, 5)
    assert len({e["event_id"] for e in a + b}) == 10
    assert [e["trade_id"] for e in a] == [e["trade_id"] for e in b]


def test_a_healthy_run_passes_and_reports_outcomes(capsys: pytest.CaptureFixture[str]) -> None:
    stack = FakeStack()
    assert smoke.run(args_for(stack), stack.publish) == 0
    out = capsys.readouterr().out
    assert "published 5; settled 5" in out and "'ESCALATED': 5" in out and "ready: kafka=ok" in out


def test_an_agent_crash_fails_the_smoke_test(capsys: pytest.CaptureFixture[str]) -> None:
    stack = FakeStack(final="FAILED")
    assert smoke.run(args_for(stack), stack.publish) == 1
    assert "FAILED (an agent crash)" in capsys.readouterr().out


def test_cases_that_never_settle_fail_after_the_timeout(capsys: pytest.CaptureFixture[str]) -> None:
    stack = FakeStack(polls_to_settle=10_000)
    ticks = iter(range(1000))  # a fake clock that advances one second per reading
    code = smoke.run(
        args_for(stack, timeout=3.0),
        stack.publish,
        clock=lambda: float(next(ticks)),
        sleep=lambda _: None,
    )
    assert code == 1
    assert "did not settle within 3.0s" in capsys.readouterr().out


def test_a_stack_that_is_not_ready_aborts_before_publishing() -> None:
    stack = FakeStack(ready=False)
    with pytest.raises(SystemExit, match="not ready"):
        smoke.run(args_for(stack), stack.publish)
    assert stack.published == []  # nothing was sent to a broken stack


def test_events_for_other_cases_are_ignored() -> None:
    client = httpx.Client(
        base_url="http://api",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={
                    "items": [
                        {"event_id": "mine", "status": "RESOLVED"},
                        {"event_id": "other", "status": "FAILED"},
                    ]
                },
            )
        ),
    )
    assert smoke.wait_for_cases(client, {"mine"}, timeout_s=1) == {"mine": "RESOLVED"}
