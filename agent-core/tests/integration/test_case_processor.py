"""CaseProcessor against real Postgres + real MCP servers; the LLM is an oracle double."""

import asyncio
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from settleagent.consumer.processor import CaseProcessor
from settleagent.domain.case import ApprovalStatus, CaseStatus, Decision
from settleagent.domain.reason_codes import ToolName
from settleagent.gate.policy import default_policy
from settleagent.persistence.repositories import (
    ApprovalRepository,
    CaseRepository,
    ToolCallRepository,
)
from settleagent.tools.gateway import McpToolGateway
from tests.doubles import DownLlm, RoutedLlm, fresh_event, oracle_reply, pick
from tests.integration.helpers import (
    NOW,
    audit_actions,
    load_case,
    make_processor,
    process_scenario,
)

pytestmark = pytest.mark.integration


processor = make_processor
process = process_scenario
load = load_case


async def test_high_confidence_write_fix_parks_in_awaiting_approval(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    sc = pick("clear", "SSIM")
    case_id = await process(processor(sessions, RoutedLlm(synthesize=oracle_reply(sc))), sc)
    case = await load(sessions, case_id)
    assert case.status is CaseStatus.AWAITING_APPROVAL
    assert case.decision is Decision.AUTO_RESOLVE
    assert case.result is not None and case.result.proposed_fix is not None
    assert case.result.proposed_fix.requires_write and case.trace_id
    async with sessions() as s:
        approvals = await ApprovalRepository(s).list_by_status(ApprovalStatus.PENDING, 1000)
        calls = await ToolCallRepository(s).list_for_case(case_id)
    assert [a.case_id for a in approvals].count(case_id) == 1
    assert {c.tool for c in calls} >= {ToolName.SSI_STORE, ToolName.TRADE_STORE}
    assert await audit_actions(sessions, case_id) == [
        "case.received",
        "case.investigation_started",
        "investigation.completed",
        "gate.decision",
        "approval.requested",
    ]


async def test_high_confidence_read_only_fix_auto_resolves_without_approval(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    sc = pick("clear", "CALD")
    case_id = await process(processor(sessions, RoutedLlm(synthesize=oracle_reply(sc))), sc)
    case = await load(sessions, case_id)
    assert case.status is CaseStatus.AUTO_RESOLVED
    async with sessions() as s:
        pending = await ApprovalRepository(s).list_by_status(ApprovalStatus.PENDING, 1000)
    assert case_id not in {a.case_id for a in pending}
    assert (await audit_actions(sessions, case_id))[-1] == "case.auto_resolved"


@pytest.mark.parametrize("difficulty", ["ambiguous", "underdetermined"])
async def test_uncertain_cases_go_to_the_human_queue_with_no_write(
    sessions: async_sessionmaker[AsyncSession], difficulty: str
) -> None:
    sc = pick(difficulty, "SSIM")
    case_id = await process(processor(sessions, RoutedLlm(synthesize=oracle_reply(sc))), sc)
    case = await load(sessions, case_id)
    assert case.status is CaseStatus.ESCALATED and case.decision is Decision.HUMAN_REVIEW
    assert (await audit_actions(sessions, case_id))[-1] == "case.escalated"
    async with sessions() as s:
        pending = await ApprovalRepository(s).list_by_status(ApprovalStatus.PENDING, 1000)
    assert case_id not in {a.case_id for a in pending}


async def test_trade_missing_from_store_is_escalated(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    sc = pick("underdetermined", "LACK", "absent")
    case_id = await process(processor(sessions, RoutedLlm(synthesize=oracle_reply(sc))), sc)
    assert (await load(sessions, case_id)).status is CaseStatus.ESCALATED


async def test_duplicate_event_is_a_noop(sessions: async_sessionmaker[AsyncSession]) -> None:
    proc = processor(sessions, RoutedLlm(synthesize=oracle_reply(pick("clear", "SSIM"))))
    event = fresh_event(pick("clear", "SSIM"))
    first, created1 = await proc.intake(event)
    second, created2 = await proc.intake(event)
    assert created1 and not created2 and first.id == second.id
    assert (await audit_actions(sessions, first.id)).count("case.received") == 1


async def test_tool_outage_degrades_to_human_review(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async def no_sleep(_: float) -> None:
        return None

    def broken() -> Any:
        raise ConnectionError("down")

    sc = pick("clear", "SSIM")
    gw = McpToolGateway(
        {t: broken for t in ToolName}, max_attempts=2, sleep=no_sleep, breaker_threshold=99
    )
    llm = RoutedLlm(synthesize=oracle_reply(sc))
    case_id = await process(processor(sessions, llm, gw), sc)
    case = await load(sessions, case_id)
    assert case.status in {CaseStatus.ESCALATED}  # confidence capped below the threshold
    assert case.result is not None and case.result.confidence <= 0.6
    async with sessions() as s:
        calls = await ToolCallRepository(s).list_for_case(case_id)
    assert calls and all(c.error and "UNAVAILABLE" in c.error for c in calls)


async def test_agent_crash_marks_case_failed_and_audits_it(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    class Exploding:
        async def investigate(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("bug in agent")

    proc = CaseProcessor(sessions, Exploding(), default_policy(0.9), clock=lambda: NOW)  # type: ignore[arg-type]
    case, _ = await proc.intake(fresh_event(pick("clear", "SSIM")))
    failed = await proc.investigate(case.id)
    assert failed.status is CaseStatus.FAILED
    assert (await audit_actions(sessions, case.id))[-1] == "investigation.failed"


async def test_llm_down_escalates_rather_than_failing(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    case_id = await process(processor(sessions, DownLlm()), pick("clear", "SSIM"))
    assert (await load(sessions, case_id)).status is CaseStatus.ESCALATED


async def test_reinvestigating_a_finished_case_is_a_noop(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    sc = pick("clear", "CALD")
    proc = processor(sessions, RoutedLlm(synthesize=oracle_reply(sc)))
    case_id = await process(proc, sc)
    before = await audit_actions(sessions, case_id)
    again = await proc.investigate(case_id)
    assert again.status is CaseStatus.AUTO_RESOLVED
    assert await audit_actions(sessions, case_id) == before


async def test_concurrent_duplicate_runs_finish_the_case_once(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    sc = pick("clear", "SSIM")
    proc = processor(sessions, RoutedLlm(synthesize=oracle_reply(sc)))
    case, _ = await proc.intake(fresh_event(sc))
    await asyncio.gather(proc.investigate(case.id), proc.investigate(case.id))
    actions = await audit_actions(sessions, case.id)
    assert actions.count("gate.decision") == 1
    assert (await load(sessions, case.id)).status is CaseStatus.AWAITING_APPROVAL


async def test_incomplete_cases_are_discoverable_and_resumable(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    sc = pick("clear", "CALD")
    proc = processor(sessions, RoutedLlm(synthesize=oracle_reply(sc)))
    received, _ = await proc.intake(fresh_event(sc))
    stuck, _ = await proc.intake(fresh_event(sc))
    async with sessions() as s, s.begin():  # simulate a crash after the INVESTIGATING commit
        await CaseRepository(s).transition(stuck.id, CaseStatus.INVESTIGATING, NOW)
    ids = await proc.incomplete_case_ids()
    assert {received.id, stuck.id} <= set(ids)
    for cid in (received.id, stuck.id):
        assert (await proc.investigate(cid)).status is CaseStatus.AUTO_RESOLVED


async def test_every_clear_scenario_with_real_tools_reaches_the_expected_outcome(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """One clear case per reason code, end to end through Postgres."""
    for code in ("LACK", "MONY", "NMAT", "CMIS", "SSIM", "DENO", "LATE", "CALD"):
        sc = pick("clear", code)
        case_id = await process(processor(sessions, RoutedLlm(synthesize=oracle_reply(sc))), sc)
        case = await load(sessions, case_id)
        expected = (
            CaseStatus.AWAITING_APPROVAL if sc.expected_requires_write else CaseStatus.AUTO_RESOLVED
        )
        assert case.status is expected, code
