"""Hardening controls: LLM rate/concurrency limits, budget enforcement surface, bounded shutdown."""

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from settleagent.agent.llm import LlmError, LlmReply
from settleagent.agent.ratelimit import RateLimitedLlm
from settleagent.observability.cost import CostTracker
from tests.test_consumer_support import FakeDlq, FakeKafka, FakeProcessor, build, valid_msg


class VClock:
    """Virtual time: sleeping advances the clock instantly, so timing tests are exact and fast."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class SlowLlm:
    def __init__(self, delay: float = 0.02, fail: bool = False) -> None:
        self.delay, self.fail = delay, fail
        self.calls = 0
        self.in_flight = 0
        self.peak = 0

    async def complete_json(self, *, system: str, user: str, schema: dict[str, Any]) -> LlmReply:
        self.calls += 1
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            if self.fail:
                raise LlmError("boom")
            return LlmReply(data={}, model="m", input_tokens=1, output_tokens=1)
        finally:
            self.in_flight -= 1


async def call(llm: RateLimitedLlm) -> LlmReply:
    return await llm.complete_json(system="s", user="u", schema={})


# ----------------------------------------------------------------- concurrency cap
async def test_concurrency_never_exceeds_the_cap() -> None:
    inner = SlowLlm()
    limited = RateLimitedLlm(inner, max_concurrent=3, requests_per_minute=0)
    await asyncio.gather(*(call(limited) for _ in range(20)))
    assert inner.calls == 20 and inner.peak == 3 and limited.peak_in_flight == 3


async def test_a_failing_call_releases_its_slot_so_the_limiter_cannot_deadlock() -> None:
    inner = SlowLlm(delay=0, fail=True)
    limited = RateLimitedLlm(inner, max_concurrent=1, requests_per_minute=0)
    for _ in range(5):  # would hang forever if a failure leaked the single slot
        with pytest.raises(LlmError):
            await asyncio.wait_for(call(limited), timeout=1)
    assert limited.peak_in_flight == 1


# ---------------------------------------------------------------------- token bucket
async def test_burst_goes_through_immediately_then_the_rate_applies() -> None:
    clock = VClock()
    limited = RateLimitedLlm(
        SlowLlm(delay=0),
        max_concurrent=10,
        requests_per_minute=60,
        burst=3,
        clock=clock,
        sleep=clock.sleep,
    )
    for _ in range(3):
        await call(limited)
    assert clock.sleeps == []  # the burst is free
    await call(limited)
    assert clock.sleeps == [pytest.approx(1.0)]  # 60/min = 1 per second
    for _ in range(5):
        await call(limited)
    assert clock.t == pytest.approx(6.0)  # 3 free, then one per second for the other 6
    assert limited.throttled_seconds == pytest.approx(6.0)


async def test_idle_time_refills_the_bucket_up_to_the_burst() -> None:
    clock = VClock()
    limited = RateLimitedLlm(
        SlowLlm(delay=0), requests_per_minute=60, burst=2, clock=clock, sleep=clock.sleep
    )
    await call(limited)
    await call(limited)
    clock.t += 100  # a long quiet period must not bank more than `burst`
    for _ in range(2):
        await call(limited)
    assert clock.sleeps == []
    await call(limited)
    assert clock.sleeps == [pytest.approx(1.0)]


async def test_rate_zero_disables_the_rate_limit_only() -> None:
    clock = VClock()
    limited = RateLimitedLlm(
        SlowLlm(delay=0), requests_per_minute=0, burst=1, clock=clock, sleep=clock.sleep
    )
    await asyncio.gather(*(call(limited) for _ in range(50)))
    assert clock.sleeps == []


def test_invalid_limits_are_rejected() -> None:
    with pytest.raises(ValueError):
        RateLimitedLlm(SlowLlm(), max_concurrent=0)
    with pytest.raises(ValueError):
        RateLimitedLlm(SlowLlm(), burst=0)


# ---------------------------------------------------------------- budget enforcement
def test_exceeded_is_true_exactly_at_the_ceiling() -> None:
    t = CostTracker(10.0, per_case_alert_usd=100)
    assert not t.exceeded and t.ceiling_usd == 10.0
    t.record(uuid4(), 9.999999)
    assert not t.exceeded
    t.record(uuid4(), 0.000001)
    assert t.exceeded  # reaching the ceiling blocks; the boundary is inclusive


def test_sync_only_ever_raises_the_total_and_never_refires_alerts() -> None:
    t = CostTracker(10.0, per_case_alert_usd=100)
    t.record(uuid4(), 9.0)  # fires the 80% alert
    t.sync(3.0)  # a lower DB figure (stale replica) is ignored
    assert t.total_usd == 9.0
    t.sync(9.5)
    assert t.total_usd == 9.5 and not t.exceeded
    assert [a.threshold for a in t.record(uuid4(), 1.0)] == [1.0]  # only 100% remains to fire
    t.sync(25.0)
    assert t.exceeded and t.total_usd == 25.0


# ------------------------------------------------------------------ bounded shutdown
async def test_drain_timeout_cancels_stuck_cases_so_a_deploy_cannot_hang() -> None:
    proc = FakeProcessor()
    proc.gate = asyncio.Event()  # never set: the investigation never finishes
    kafka = FakeKafka([valid_msg(0)])
    consumer = build(kafka, proc, FakeDlq(), drain_timeout_s=0.2)
    runner = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.1)
    consumer.stop()
    await asyncio.wait_for(runner, timeout=3)  # returns because the drain gave up
    assert kafka.closed and consumer.inflight == 0 and proc.investigated == []


async def test_a_generous_drain_timeout_still_lets_work_finish() -> None:
    proc = FakeProcessor()
    proc.gate = asyncio.Event()
    kafka = FakeKafka([valid_msg(0)])
    consumer = build(kafka, proc, FakeDlq(), drain_timeout_s=5)
    runner = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.1)
    consumer.stop()
    await asyncio.sleep(0.1)
    proc.gate.set()
    await asyncio.wait_for(runner, timeout=3)
    assert len(proc.investigated) == 1  # finished, not cancelled


async def test_peak_inflight_is_recorded_and_bounded() -> None:
    proc = FakeProcessor()
    proc.gate = asyncio.Event()
    kafka = FakeKafka([valid_msg(i) for i in range(8)])
    consumer = build(kafka, proc, FakeDlq(), max_inflight=3)
    kafka.on_empty = consumer.stop
    runner = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.15)
    proc.gate.set()
    await asyncio.wait_for(runner, timeout=3)
    assert consumer.peak_inflight == 3 == proc.max_running  # the cap, observed two ways
    assert len(proc.investigated) == 8 and consumer.inflight == 0
