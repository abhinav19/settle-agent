"""The cost ceiling is a hard stop: over budget, cases go to a human and the LLM is not called."""

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from settleagent.api.app import summarize, usage_from_audit
from settleagent.domain.case import CaseStatus, Decision
from settleagent.observability.cost import CostTracker
from settleagent.observability.instrumentation import TracedLlm
from settleagent.persistence.metrics import total_cost
from settleagent.persistence.repositories import (
    ApprovalRepository,
    AuditRepository,
    CaseRepository,
    ToolCallRepository,
)
from tests.doubles import RoutedLlm, fresh_event, oracle_reply, pick
from tests.integration.helpers import make_processor
from tests.telemetry_support import Probe, make_probe

pytestmark = pytest.mark.integration
MODEL = "claude-opus-5-5"  # a real price, so cost accrues; one oracle case costs $0.0024


class Setup:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], ceiling: float) -> None:
        self.sessions = sessions
        self.probe: Probe = make_probe()
        self.tracker = CostTracker(ceiling, per_case_alert_usd=100, telemetry=self.probe.telemetry)
        sc = pick("clear", "SSIM")
        self.llm = RoutedLlm(synthesize=oracle_reply(sc), model=MODEL)
        self.processor = make_processor(sessions, TracedLlm(self.llm, self.probe.telemetry))
        self.processor._cost_tracker = self.tracker  # noqa: SLF001 - wire the tracker under test
        self.processor._telemetry = self.probe.telemetry  # noqa: SLF001

    async def run_case(self) -> UUID:
        case, _ = await self.processor.intake(fresh_event(pick("clear", "SSIM")))
        await self.processor.investigate(case.id)
        return case.id

    async def actions(self, case_id: UUID) -> list[str]:
        async with self.sessions() as s:
            return [e.action for e in await AuditRepository(s).list_for_case(case_id)]


async def test_cases_over_the_ceiling_are_escalated_without_calling_the_llm(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    rig = Setup(sessions, ceiling=0.002)  # a single oracle case ($0.0024) blows it
    first = await rig.run_case()
    assert rig.tracker.exceeded
    calls_after_first = len(rig.llm.calls)
    assert calls_after_first > 0

    blocked = await rig.run_case()
    assert len(rig.llm.calls) == calls_after_first  # NOT ONE more LLM call
    async with sessions() as s:
        case = await CaseRepository(s).get(blocked)
        assert await ToolCallRepository(s).list_for_case(blocked) == []  # no tool calls either
        assert (
            await ApprovalRepository(s).get_by_case(blocked) is None
        )  # nothing offered for approval
    assert case.status is CaseStatus.ESCALATED and case.decision is Decision.HUMAN_REVIEW
    assert case.result is None  # honest: it was never investigated
    actions = await rig.actions(blocked)
    assert actions[-1] == "budget.ceiling_reached" and "investigation.completed" not in actions

    async with sessions() as s:  # the audit row says why, with the figures
        events = await AuditRepository(s).list_for_case(blocked)
    payload = next(e.payload for e in events if e.action == "budget.ceiling_reached")
    assert payload["ceiling_usd"] == 0.002 and payload["total_usd"] >= 0.002

    assert (await rig.actions(first))[-1] == "approval.requested"  # the first case was untouched
    assert rig.probe.total("settleagent.budget.blocked", reason_code="SSIM") == 1
    assert rig.probe.total("settleagent.cases.processed", decision="budget_blocked") == 1
    ran, skipped = rig.probe.spans("case.investigate")  # one root span per run, in order
    assert "budget.blocked" not in (ran.attributes or {})
    assert (skipped.attributes or {}).get("budget.blocked") is True


async def test_under_the_ceiling_everything_runs_normally(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    rig = Setup(sessions, ceiling=100.0)
    ids = [await rig.run_case() for _ in range(3)]
    assert not rig.tracker.exceeded and rig.probe.total("settleagent.budget.blocked") == 0
    async with sessions() as s:
        statuses = [(await CaseRepository(s).get(i)).status for i in ids]
    assert statuses == [CaseStatus.AWAITING_APPROVAL] * 3
    assert rig.tracker.total_usd == pytest.approx(3 * 0.0024)


async def test_a_replica_learns_about_spend_from_the_database(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Two agent processes share one budget through the audit trail, not through memory."""
    replica_a = Setup(sessions, ceiling=0.002)
    await replica_a.run_case()  # replica A spends past the ceiling

    replica_b = Setup(sessions, ceiling=0.002)  # fresh process: believes it has spent nothing
    assert replica_b.tracker.total_usd == 0 and not replica_b.tracker.exceeded
    async with sessions() as s:
        replica_b.tracker.sync(await total_cost(s))  # what the periodic refresh does
    assert replica_b.tracker.exceeded

    blocked = await replica_b.run_case()
    assert replica_b.llm.calls == []  # replica B never called the LLM
    assert (await replica_b.actions(blocked))[-1] == "budget.ceiling_reached"


async def test_a_blocked_case_is_presentable_through_the_api_models(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    rig = Setup(sessions, ceiling=0.002)
    await rig.run_case()
    blocked = await rig.run_case()
    async with sessions() as s:
        case = await CaseRepository(s).get(blocked)
        audit = await AuditRepository(s).list_for_case(blocked)
    summary = summarize(case)
    assert summary.status is CaseStatus.ESCALATED and summary.confidence is None
    assert summary.root_cause is None and summary.fix_action is None
    assert usage_from_audit(audit) is None  # nothing was spent on it


async def test_restart_does_not_forget_the_spend(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    rig = Setup(sessions, ceiling=1000.0)
    await rig.run_case()
    restarted: Any = CostTracker(1000.0, per_case_alert_usd=100)
    async with sessions() as s:
        restarted.seed(await total_cost(s))
    assert restarted.total_usd >= rig.tracker.total_usd > 0
