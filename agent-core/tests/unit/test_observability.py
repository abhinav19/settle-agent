"""Pricing, JSON logging, budget alerts, the telemetry facade and the instrumentation wrappers."""

import io
import json
import logging
from typing import Any
from uuid import uuid4

import pytest

from settleagent.agent.llm import LlmError
from settleagent.agent.state import initial_state
from settleagent.domain.reason_codes import ToolName
from settleagent.observability.cost import CostTracker
from settleagent.observability.instrumentation import (
    TracedGateway,
    TracedLlm,
    schema_kind,
    traced_node,
)
from settleagent.observability.logging import (
    bind_case,
    configure_logging,
    current_case_id,
)
from settleagent.observability.pricing import (
    PRICES_PER_MTOK,
    cost_usd,
    is_known_model,
)
from settleagent.observability.telemetry import NOOP, Telemetry, clean, setup_telemetry
from settleagent.tools.gateway import ErrorKind
from tests.doubles import DownLlm, FakeGateway, RoutedLlm
from tests.factories import make_event
from tests.telemetry_support import Probe, make_probe


def attrs(span: Any) -> dict[str, Any]:
    return dict(span.attributes or {})


# ------------------------------------------------------------------- pricing
def test_cost_math_for_known_models() -> None:
    # opus-5-5: $4 in / $20 out per MTok
    assert cost_usd("claude-opus-5-5", 1_000_000, 0) == 4.0
    assert cost_usd("claude-opus-5-5", 0, 1_000_000) == 20.0
    assert cost_usd("claude-opus-5-5", 10_000, 2_000) == pytest.approx(0.08)
    assert cost_usd("claude-sonnet-5-5", 10_000, 2_000) == pytest.approx(0.04)
    assert cost_usd("claude-haiku-4-5", 1_000_000, 1_000_000) == 6.0


def test_zero_tokens_cost_nothing_and_negatives_are_rejected() -> None:
    assert cost_usd("claude-opus-5-5", 0, 0) == 0.0
    with pytest.raises(ValueError):
        cost_usd("claude-opus-5-5", -1, 0)


def test_dated_snapshots_use_the_base_model_price() -> None:
    assert cost_usd("claude-haiku-4-5-20251001", 1_000_000, 0) == cost_usd(
        "claude-haiku-4-5", 1_000_000, 0
    )
    assert is_known_model("claude-haiku-4-5-20251001")


def test_unknown_models_are_priced_conservatively_never_free() -> None:
    assert not is_known_model("mystery-model")
    assert cost_usd("mystery-model", 1_000_000, 0) == max(p[0] for p in PRICES_PER_MTOK.values())
    assert cost_usd("mystery-model", 0, 1_000_000) == max(p[1] for p in PRICES_PER_MTOK.values())


def test_micro_dollar_rounding() -> None:
    assert cost_usd("claude-opus-5-5", 1, 1) == pytest.approx(0.000024)


# ------------------------------------------------------------------- logging
def log_lines(buf: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line]


@pytest.fixture
def logbuf() -> Any:
    buf = io.StringIO()
    configure_logging("DEBUG", stream=buf)
    yield buf
    logging.getLogger().handlers = []


def test_json_logs_carry_the_bound_case_id(logbuf: io.StringIO) -> None:
    cid = uuid4()
    log = logging.getLogger("t")
    log.info("outside")
    with bind_case(cid):
        assert current_case_id() == str(cid)
        log.warning("inside", extra={"decision": "HUMAN_REVIEW", "confidence": 0.4})
    log.info("after")
    outside, inside, after = log_lines(logbuf)
    assert outside["case_id"] is None and after["case_id"] is None
    assert inside["case_id"] == str(cid) and inside["level"] == "WARNING"
    assert inside["decision"] == "HUMAN_REVIEW" and inside["confidence"] == 0.4
    assert (
        inside["message"] == "inside"
        and inside["logger"] == "t"
        and inside["ts"].endswith("+00:00")
    )
    assert current_case_id() is None


def test_nested_binding_restores_the_outer_case(logbuf: io.StringIO) -> None:
    a, b = uuid4(), uuid4()
    log = logging.getLogger("t")
    with bind_case(a):
        with bind_case(b):
            log.info("inner")
        log.info("outer")
    inner, outer = log_lines(logbuf)
    assert (inner["case_id"], outer["case_id"]) == (str(b), str(a))


async def test_concurrent_tasks_do_not_leak_case_ids(logbuf: io.StringIO) -> None:
    import asyncio

    log = logging.getLogger("t")

    async def work(cid: Any) -> None:
        with bind_case(cid):
            await asyncio.sleep(0.01)
            log.info("working", extra={"who": str(cid)})

    ids = [uuid4() for _ in range(5)]
    await asyncio.gather(*(work(i) for i in ids))
    lines = [ln for ln in log_lines(logbuf) if ln["logger"] == "t"]  # ignore asyncio's own debug
    assert len(lines) == 5
    for line in lines:
        assert line["case_id"] == line["who"]


def test_exceptions_are_serialised_and_trace_ids_included(logbuf: io.StringIO) -> None:
    probe = make_probe()
    log = logging.getLogger("t")
    with probe.telemetry.span("work") as span:
        try:
            raise ValueError("boom")
        except ValueError:
            log.exception("failed")
        trace_id = format(span.get_span_context().trace_id, "032x")
    [line] = log_lines(logbuf)
    assert "ValueError: boom" in line["exception"] and line["trace_id"] == trace_id
    assert len(line["span_id"]) == 16


def test_configure_logging_is_idempotent_and_supports_plain_text() -> None:
    buf = io.StringIO()
    configure_logging("INFO", stream=buf)
    configure_logging("INFO", stream=buf)
    assert len(logging.getLogger().handlers) == 1
    configure_logging("INFO", json_logs=False, stream=buf)
    logging.getLogger("t").info("plain")
    assert "plain" in buf.getvalue() and not buf.getvalue().lstrip().startswith("{")
    logging.getLogger().handlers = []


# ---------------------------------------------------------------- budget alerts
def tracker(ceiling: float = 10.0, **kw: Any) -> CostTracker:
    return CostTracker(ceiling, **kw)


def test_threshold_alerts_fire_once_each(caplog: pytest.LogCaptureFixture) -> None:
    probe = make_probe()
    t = tracker(10.0, per_case_alert_usd=100, telemetry=probe.telemetry)
    with caplog.at_level("WARNING"):
        assert t.record(uuid4(), 5.0) == []
        [a80] = t.record(uuid4(), 3.5)  # 8.5 -> crosses 80%
        assert a80.threshold == 0.8 and t.fraction_used == pytest.approx(0.85)
        assert t.record(uuid4(), 0.2) == []  # still below 100%; 80% does not repeat
        [a100] = t.record(uuid4(), 2.0)
        assert a100.threshold == 1.0
        assert t.record(uuid4(), 1.0) == []  # already alerted
    assert caplog.text.count("BUDGET ALERT") == 2
    assert probe.total("settleagent.budget.alerts", kind="budget_threshold") == 2


def test_one_big_jump_alerts_for_every_threshold_crossed() -> None:
    alerts = tracker(10.0, per_case_alert_usd=100).record(uuid4(), 12.0)
    assert [a.threshold for a in alerts] == [0.8, 1.0]


def test_expensive_single_case_alerts() -> None:
    probe = make_probe()
    t = tracker(1000.0, per_case_alert_usd=1.0, telemetry=probe.telemetry)
    assert t.record(uuid4(), 0.5) == []
    [alert] = t.record(uuid4(), 1.5)
    assert (
        alert.kind == "case_cost"
        and probe.total("settleagent.budget.alerts", kind="case_cost") == 1
    )


def test_seeding_from_the_database_suppresses_repeat_alerts_after_a_restart() -> None:
    t = tracker(10.0)
    t.seed(9.0)  # already past 80%
    assert t.total_usd == 9.0
    assert t.record(uuid4(), 0.5) == []  # no re-alert for 80%
    assert [a.threshold for a in t.record(uuid4(), 1.0)] == [1.0]


def test_tracker_rejects_a_nonpositive_ceiling() -> None:
    with pytest.raises(ValueError):
        CostTracker(0)


# ----------------------------------------------------------------- telemetry
def test_noop_telemetry_is_safe_everywhere() -> None:
    with NOOP.span("x", a=1) as span:
        span.set_attribute("k", "v")
    NOOP.cases_processed.add(1, {"a": "b"})
    NOOP.set_gauges(queue_depth=3, pending_approvals=1)
    assert NOOP.current_trace_id() is None
    NOOP.shutdown()


def test_attribute_cleaning() -> None:
    out = clean({"a": 1, "b": None, "d": [1, 2], "e": True, "f": uuid4()})
    assert out["a"] == 1 and out["e"] is True and "b" not in out
    assert out["d"] == "[1, 2]" and isinstance(out["f"], str)  # non-primitives are stringified


def test_spans_nest_and_record_exceptions() -> None:
    p = make_probe()
    with pytest.raises(RuntimeError):
        with p.telemetry.span("outer"):
            with p.telemetry.span("inner"):
                raise RuntimeError("bad")
    outer, inner = p.span("outer"), p.span("inner")
    assert inner.parent is not None and inner.parent.span_id == outer.context.span_id
    assert p.errored(inner) and p.errored(outer)
    assert any(e.name == "exception" for e in inner.events)


def test_current_trace_id_matches_the_active_span() -> None:
    p = make_probe()
    with p.telemetry.span("root") as span:
        tid = Telemetry.current_trace_id()
        assert tid == format(span.get_span_context().trace_id, "032x") and len(tid) == 32
    assert Telemetry.current_trace_id() is None


def test_gauges_are_observable() -> None:
    p = make_probe()
    p.telemetry.set_gauges(queue_depth=7, pending_approvals=2)
    assert [pt.value for pt in p.points("settleagent.queue.depth")] == [7.0]
    assert [pt.value for pt in p.points("settleagent.approvals.pending")] == [2.0]


def test_disabled_setup_returns_a_working_noop() -> None:
    t = setup_telemetry("svc", "http://127.0.0.1:1", enabled=False)
    with t.span("x"):
        pass
    t.shutdown()


def test_enabled_setup_builds_real_providers_and_shuts_down_cleanly() -> None:
    t = setup_telemetry("svc", "http://127.0.0.1:1", enabled=True)
    assert type(t.tracer).__module__.startswith("opentelemetry.sdk")
    t.shutdown()  # nothing was emitted, so this does not wait on the dead endpoint


# ------------------------------------------------------- instrumentation wrappers
def probe_and_traced_llm(inner: Any, model: str = "claude-opus-5-5") -> tuple[Probe, TracedLlm]:
    p = make_probe()
    return p, TracedLlm(inner, p.telemetry)


async def test_traced_llm_records_tokens_cost_and_a_span() -> None:
    reply: dict[str, Any] = {
        "root_cause": "x",
        "reason_category": "STATIC_DATA",
        "evidence_refs": [],
        "fix": None,
        "confidence": 0.5,
    }
    p, llm = probe_and_traced_llm(RoutedLlm(synthesize=reply, model="claude-opus-5-5"))
    schema: dict[str, Any] = {"properties": {"root_cause": {}}}
    await llm.complete_json(system="s", user="u", schema=schema)
    span = p.span("llm.complete")
    assert attrs(span)["llm.kind"] == "synthesize" and attrs(span)["llm.model"] == "claude-opus-5-5"
    assert (attrs(span)["llm.input_tokens"], attrs(span)["llm.output_tokens"]) == (100, 20)
    expected = cost_usd("claude-opus-5-5", 100, 20)
    assert attrs(span)["llm.cost_usd"] == expected
    assert p.total("settleagent.llm.tokens", type="input") == 100
    assert p.total("settleagent.llm.tokens", type="output") == 20
    assert p.total("settleagent.llm.cost") == pytest.approx(expected)
    assert p.count("settleagent.llm.duration", kind="synthesize") == 1


async def test_traced_llm_marks_failures_on_the_span() -> None:
    p, llm = probe_and_traced_llm(DownLlm())
    with pytest.raises(LlmError):
        await llm.complete_json(system="s", user="u", schema={"properties": {"sufficient": {}}})
    span = p.span("llm.complete")
    assert p.errored(span) and attrs(span)["llm.kind"] == "analyze"
    assert p.total("settleagent.llm.cost") == 0


@pytest.mark.parametrize(
    ("props", "kind"),
    [
        ({"extra_calls": 1}, "plan"),
        ({"sufficient": 1}, "analyze"),
        ({"root_cause": 1}, "synthesize"),
    ],
)
def test_schema_kind(props: dict[str, int], kind: str) -> None:
    assert schema_kind({"properties": props}) == kind


async def test_traced_gateway_distinguishes_findings_from_faults() -> None:
    p = make_probe()
    gw = TracedGateway(
        FakeGateway(
            {
                "trade-store.get_trade": {"ok": 1},
                "ssi-store.get_ssi": ErrorKind.NOT_FOUND,
                "security-master.get_instrument": ErrorKind.TIMEOUT,
            }
        ),
        p.telemetry,
    )
    await gw.call(ToolName.TRADE_STORE, "get_trade", {})
    await gw.call(ToolName.SSI_STORE, "get_ssi", {})
    await gw.call(ToolName.SECURITY_MASTER, "get_instrument", {})
    ok, notfound, timeout = p.spans("tool.call")
    assert attrs(ok)["tool.ok"] is True and not p.errored(ok)
    assert attrs(notfound)["tool.error"] == "NOT_FOUND" and not p.errored(notfound)
    assert attrs(timeout)["tool.error"] == "TIMEOUT" and p.errored(timeout)
    assert p.total("settleagent.tool.calls", outcome="ok") == 1
    assert p.total("settleagent.tool.calls", outcome="NOT_FOUND", infra_failure=False) == 1
    assert p.total("settleagent.tool.calls", outcome="TIMEOUT", infra_failure=True) == 1
    assert p.count("settleagent.tool.duration") == 3


async def test_traced_node_creates_a_named_span_with_the_step_count() -> None:
    p = make_probe()

    async def plan(state: Any) -> dict[str, Any]:
        return {"steps_used": 3}

    wrapped = traced_node(p.telemetry, "plan", plan)
    state = initial_state(make_event(), uuid4(), "t")
    assert await wrapped(state) == {"steps_used": 3}
    span = p.span("agent.plan")
    assert attrs(span)["agent.steps_used"] == 3 and attrs(span)["case_id"] == str(state["case_id"])
    assert wrapped.__name__ == "plan"  # type: ignore[attr-defined]
