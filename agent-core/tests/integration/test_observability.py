"""Observability end to end: a real case through Postgres with in-memory OpenTelemetry."""

import io
import json
import logging
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from settleagent.agent.graph import InvestigationAgent
from settleagent.agent.nodes import AgentConfig
from settleagent.consumer.processor import CaseProcessor
from settleagent.domain.case import CaseStatus
from settleagent.domain.reason_codes import ToolName
from settleagent.gate.policy import default_policy
from settleagent.observability.cost import CostTracker
from settleagent.observability.instrumentation import TracedGateway, TracedLlm
from settleagent.observability.logging import configure_logging
from settleagent.observability.pricing import cost_usd
from settleagent.persistence.repositories import ApprovalAlreadyDecidedError, AuditRepository
from settleagent.remediation.service import RemediationService
from settleagent.remediation.system import SyntheticSettlementSystem
from settleagent.tools.gateway import McpToolGateway
from tests.doubles import RoutedLlm, fresh_event, oracle_reply, pick, real_gateway
from tests.integration.helpers import NOW
from tests.telemetry_support import Probe, make_probe

pytestmark = pytest.mark.integration
MODEL = "claude-opus-5-5"


class Rig:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        probe: Probe,
        *,
        llm: Any,
        gateway: Any = None,
        tracker: CostTracker | None = None,
        perf: Any = None,
    ) -> None:
        self.sessions, self.probe = sessions, probe
        agent = InvestigationAgent(
            TracedLlm(llm, probe.telemetry),
            TracedGateway(gateway or real_gateway(), probe.telemetry),
            AgentConfig(),
            probe.telemetry,
        )
        kwargs: dict[str, Any] = {"perf_counter": perf} if perf else {}
        self.processor = CaseProcessor(
            sessions,
            agent,
            default_policy(0.9),
            clock=lambda: NOW,
            telemetry=probe.telemetry,
            cost_tracker=tracker,
            **kwargs,
        )

    async def run(self, difficulty: str, code: str, **pick_kw: Any) -> UUID:
        sc = pick(difficulty, code, **pick_kw)
        case, _ = await self.processor.intake(fresh_event(sc))
        await self.processor.investigate(case.id)
        return case.id

    async def audit(self, case_id: UUID, action: str) -> dict[str, Any]:
        async with self.sessions() as s:
            events = await AuditRepository(s).list_for_case(case_id)
        return next(e.payload for e in events if e.action == action)


def make_rig(sessions: async_sessionmaker[AsyncSession], code: str = "SSIM", **kw: Any) -> Rig:
    sc = pick("clear", code)
    llm = RoutedLlm(synthesize=oracle_reply(sc), model=MODEL)
    return Rig(sessions, make_probe(), llm=kw.pop("llm", llm), **kw)


@pytest.fixture
def logbuf() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    configure_logging("INFO", stream=buf)
    yield buf
    logging.getLogger().handlers = []


# ------------------------------------------------------------------- traces
async def test_one_root_span_per_case_with_node_llm_and_tool_children(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    sc = pick("clear", "SSIM")
    rig = Rig(sessions, make_probe(), llm=RoutedLlm(synthesize=oracle_reply(sc), model=MODEL))
    case, _ = await rig.processor.intake(fresh_event(sc))
    await rig.processor.investigate(case.id)
    p = rig.probe

    root = p.span("case.investigate")
    assert root.parent is None
    assert root.attributes is not None and root.attributes["case_id"] == str(case.id)
    nodes = {s.name: s for s in p.children_of(root)}
    assert set(nodes) == {"agent.plan", "agent.gather", "agent.analyze", "agent.synthesize"}

    tool_spans = p.spans("tool.call")
    assert tool_spans and all(
        s.parent is not None and s.parent.span_id == nodes["agent.gather"].context.span_id
        for s in tool_spans
    )
    llm_spans = p.spans("llm.complete")
    assert {s.attributes["llm.kind"] for s in llm_spans if s.attributes} == {
        "plan",
        "analyze",
        "synthesize",
    }
    assert all(s.parent is not None for s in llm_spans)  # always under a node

    # every span of the investigation shares the root's trace
    in_trace = [s for s in p.spans() if s.context.trace_id == root.context.trace_id]
    assert {s.name for s in in_trace} >= {
        "case.investigate",
        "agent.plan",
        "tool.call",
        "llm.complete",
    }
    roots = [s for s in in_trace if s.parent is None]
    assert roots == [root]  # exactly one root span per case


async def test_root_span_carries_the_required_attributes_and_the_trace_is_stored_on_the_case(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    rig = make_rig(sessions)
    case_id = await rig.run("clear", "SSIM")
    root = rig.probe.span("case.investigate")
    a: dict[str, Any] = dict(root.attributes or {})
    assert a["case_id"] == str(case_id) and a["reason_code"] == "SSIM"
    assert a["confidence"] == pytest.approx(0.97) and a["decision"] == "AUTO_RESOLVE"
    assert a["tokens"] > 0 and a["cost_usd"] > 0 and a["model"] == MODEL
    assert a["case.status"] == "AWAITING_APPROVAL" and a["category"] == "STATIC_DATA"

    async with rig.sessions() as s:
        from settleagent.persistence.repositories import CaseRepository

        case = await CaseRepository(s).get(case_id)
    assert case.trace_id == format(root.context.trace_id, "032x")  # real OTel trace id
    assert case.result is not None and case.result.reasoning_trace_id == case.trace_id


async def test_failed_tool_calls_are_error_spans_but_not_found_is_not(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async def no_sleep(_: float) -> None:
        return None

    def broken() -> Any:
        raise ConnectionError("down")

    gw = McpToolGateway(
        {t: broken for t in ToolName}, max_attempts=1, sleep=no_sleep, breaker_threshold=99
    )
    rig = make_rig(sessions, gateway=gw)
    await rig.run("clear", "SSIM")
    tool_spans = rig.probe.spans("tool.call")
    assert tool_spans and all(rig.probe.errored(s) for s in tool_spans)
    assert rig.probe.total("settleagent.tool.calls", infra_failure=True) == len(tool_spans)

    missing = make_rig(sessions)
    await missing.run("underdetermined", "LACK", note_contains="absent")  # trade not in the store
    not_found = [
        s
        for s in missing.probe.spans("tool.call")
        if (s.attributes or {}).get("tool.error") == "NOT_FOUND"
    ]
    assert not_found and not any(missing.probe.errored(s) for s in not_found)


async def test_an_agent_crash_marks_the_root_span_failed(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    class Exploding:
        async def investigate(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("bug")

    probe = make_probe()
    agent: Any = Exploding()
    proc = CaseProcessor(
        sessions, agent, default_policy(0.9), clock=lambda: NOW, telemetry=probe.telemetry
    )
    case, _ = await proc.intake(fresh_event(pick("clear", "SSIM")))
    failed = await proc.investigate(case.id)
    assert failed.status is CaseStatus.FAILED
    assert probe.errored(probe.span("case.investigate"))
    assert probe.total("settleagent.cases.processed", status="FAILED") == 1


# ------------------------------------------------------------------ metrics
async def test_case_metrics(sessions: async_sessionmaker[AsyncSession]) -> None:
    ticks = iter([0.0, 2.5])  # injected clock: the run "took" 2.5 s
    rig = make_rig(sessions, perf=lambda: next(ticks))
    case_id = await rig.run("clear", "SSIM")
    p = rig.probe
    assert p.total("settleagent.cases.received", outcome="created", reason_code="SSIM") == 1
    assert (
        p.total(
            "settleagent.cases.processed",
            status="AWAITING_APPROVAL",
            decision="AUTO_RESOLVE",
            reason_code="SSIM",
        )
        == 1
    )
    assert p.count("settleagent.case.duration") == 1
    assert p.total("settleagent.case.duration") == pytest.approx(2.5)
    payload = await rig.audit(case_id, "investigation.completed")
    assert p.total("settleagent.llm.cost") == pytest.approx(payload["cost_usd"])
    assert p.total("settleagent.case.cost") == pytest.approx(payload["cost_usd"])
    tokens = p.total("settleagent.llm.tokens", type="input") + p.total(
        "settleagent.llm.tokens", type="output"
    )
    assert tokens == payload["tokens"]


async def test_duplicate_events_are_counted(sessions: async_sessionmaker[AsyncSession]) -> None:
    rig = make_rig(sessions)
    event = fresh_event(pick("clear", "SSIM"))
    await rig.processor.intake(event)
    await rig.processor.intake(event)
    assert rig.probe.total("settleagent.cases.received", outcome="created") == 1
    assert rig.probe.total("settleagent.cases.received", outcome="duplicate") == 1


async def test_tool_metrics_match_the_recorded_tool_calls(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    rig = make_rig(sessions)
    await rig.run("clear", "SSIM")
    assert rig.probe.total("settleagent.tool.calls", outcome="ok") == len(
        rig.probe.spans("tool.call")
    )


# ------------------------------------------------------- usage in the audit trail
async def test_audit_records_tokens_cost_and_duration(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    ticks = iter([10.0, 10.75])
    rig = make_rig(sessions, perf=lambda: next(ticks))
    case_id = await rig.run("clear", "SSIM")
    pl = await rig.audit(case_id, "investigation.completed")
    assert pl["model"] == MODEL and pl["duration_ms"] == 750
    assert pl["input_tokens"] > 0 and pl["output_tokens"] > 0
    assert pl["tokens"] == pl["input_tokens"] + pl["output_tokens"]
    assert pl["cost_usd"] == cost_usd(MODEL, pl["input_tokens"], pl["output_tokens"])


# ----------------------------------------------------------------- logging
async def test_every_log_line_during_a_case_carries_its_case_id(
    sessions: async_sessionmaker[AsyncSession], logbuf: io.StringIO
) -> None:
    rig = make_rig(sessions)
    case_id = await rig.run("clear", "SSIM")
    lines = [json.loads(ln) for ln in logbuf.getvalue().splitlines() if ln]
    ours = [ln for ln in lines if ln["logger"].startswith("settleagent.")]
    assert ours, "expected settleagent log lines"
    assert all(ln["case_id"] == str(case_id) for ln in ours), [
        ln for ln in ours if ln["case_id"] != str(case_id)
    ]
    routed = next(ln for ln in ours if ln["message"] == "case routed")
    assert routed["decision"] == "AUTO_RESOLVE" and routed["cost_usd"] > 0
    root = rig.probe.span("case.investigate")
    assert routed["trace_id"] == format(root.context.trace_id, "032x")


# -------------------------------------------------------------------- budget
async def test_cost_tracker_accumulates_and_alerts_during_processing(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    probe = make_probe()
    tracker = CostTracker(0.0001, per_case_alert_usd=100, telemetry=probe.telemetry)  # tiny budget
    sc = pick("clear", "SSIM")
    rig = Rig(
        sessions, probe, llm=RoutedLlm(synthesize=oracle_reply(sc), model=MODEL), tracker=tracker
    )
    with caplog.at_level("WARNING"):
        case_id = await rig.run("clear", "SSIM")
    paid = (await rig.audit(case_id, "investigation.completed"))["cost_usd"]
    assert tracker.total_usd == pytest.approx(paid)
    assert caplog.text.count("BUDGET ALERT") == 2  # 80% and 100% crossed by one case
    assert probe.total("settleagent.budget.alerts", kind="budget_threshold") == 2


# ------------------------------------------------------------- remediation
async def test_operator_actions_are_traced_counted_and_logged(
    sessions: async_sessionmaker[AsyncSession], logbuf: io.StringIO
) -> None:
    rig = make_rig(sessions)
    case_id = await rig.run("clear", "SSIM")
    service = RemediationService(
        sessions,
        SyntheticSettlementSystem(sessions, clock=lambda: NOW),
        clock=lambda: NOW,
        telemetry=rig.probe.telemetry,
    )
    from settleagent.persistence.repositories import ApprovalRepository

    async with sessions() as s:
        approval = await ApprovalRepository(s).get_by_case(case_id)
    assert approval is not None
    await service.approve(approval.id, "alice")
    await service.approve(approval.id, "alice")  # idempotent replay
    with pytest.raises(ApprovalAlreadyDecidedError):
        await service.reject(approval.id, "bob")  # conflicting decision

    span = rig.probe.spans("remediation.approve")[0]
    a: dict[str, Any] = dict(span.attributes or {})
    assert a["case_id"] == str(case_id) and a["case.status"] == "RESOLVED" and a["changed"] is True
    assert (
        rig.probe.total("settleagent.remediation.actions", action="approve", outcome="applied") == 1
    )
    assert (
        rig.probe.total("settleagent.remediation.actions", action="approve", outcome="replay") == 1
    )
    assert rig.probe.total("settleagent.remediation.actions", action="reject") == 1  # error outcome
    assert any(
        "error:" in str(x["outcome"])
        for x in rig.probe.attribute_sets("settleagent.remediation.actions")
    )
    logged = [json.loads(ln) for ln in logbuf.getvalue().splitlines() if ln]
    approve_logs = [ln for ln in logged if ln["message"] == "operator approve"]
    assert approve_logs and all(ln["case_id"] == str(case_id) for ln in approve_logs)
    assert approve_logs[0]["decided_by"] == "alice"


async def test_average_cost_ignores_runs_recorded_before_cost_tracking(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Legacy audit rows (no cost_usd) must not drag the average down."""
    from sqlalchemy import text

    from settleagent.persistence.metrics import compute_metrics

    rig = make_rig(sessions)
    case_id = await rig.run("clear", "SSIM")
    async with sessions() as s:
        before = await compute_metrics(s)
    async with sessions() as s, s.begin():  # a legacy run: same action, no cost/duration keys
        await s.execute(
            text(
                "INSERT INTO audit_events (case_id, actor, action, payload, ts) "
                "VALUES (:c, 'agent', 'investigation.completed', '{\"tokens\": 5}'::jsonb, now())"
            ),
            {"c": case_id},
        )
    async with sessions() as s:
        after = await compute_metrics(s)
    assert after.investigations == before.investigations + 1  # counted as a run...
    assert after.total_cost_usd == pytest.approx(before.total_cost_usd)
    assert after.average_cost_usd == pytest.approx(before.average_cost_usd)  # ...but not averaged
