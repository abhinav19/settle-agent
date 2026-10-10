from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from settleagent.domain.case import (
    ActorType,
    ApprovalStatus,
    CaseStatus,
    Decision,
    InvalidTransitionError,
    ToolCallRecord,
)
from settleagent.domain.reason_codes import ToolName
from settleagent.persistence.repositories import (
    ApprovalAlreadyDecidedError,
    ApprovalNotFoundError,
    ApprovalRepository,
    AuditRepository,
    CaseNotFoundError,
    CaseRepository,
    ToolCallRepository,
)
from tests.factories import NOW, make_event, make_fix, make_result

pytestmark = pytest.mark.integration


async def new_case(session: AsyncSession):  # type: ignore[no-untyped-def]
    case, created = await CaseRepository(session).create_from_event(make_event(), uuid4(), NOW)
    assert created
    return case


async def test_create_case_and_roundtrip(session: AsyncSession) -> None:
    case = await new_case(session)
    assert case.status is CaseStatus.RECEIVED
    loaded = await CaseRepository(session).get(case.id)
    assert loaded == case
    assert loaded.event.reason_code == "SSIM"


async def test_duplicate_event_id_is_noop(session: AsyncSession) -> None:
    repo = CaseRepository(session)
    event = make_event()
    first, created1 = await repo.create_from_event(event, uuid4(), NOW)
    second, created2 = await repo.create_from_event(event, uuid4(), NOW)
    assert created1 and not created2
    assert first.id == second.id


async def test_get_missing_case(session: AsyncSession) -> None:
    with pytest.raises(CaseNotFoundError):
        await CaseRepository(session).get(uuid4())


async def test_legal_transition_persists_result(session: AsyncSession) -> None:
    repo = CaseRepository(session)
    case = await new_case(session)
    await repo.transition(case.id, CaseStatus.INVESTIGATING, NOW)
    result = make_result(case.id)
    done = await repo.transition(
        case.id,
        CaseStatus.AWAITING_APPROVAL,
        NOW,
        result=result,
        decision=Decision.AUTO_RESOLVE,
        trace_id="t1",
    )
    assert done.status is CaseStatus.AWAITING_APPROVAL
    assert done.result == result
    assert done.decision is Decision.AUTO_RESOLVE
    assert done.trace_id == "t1"


async def test_illegal_transition_rejected(session: AsyncSession) -> None:
    repo = CaseRepository(session)
    case = await new_case(session)
    with pytest.raises(InvalidTransitionError):
        await repo.transition(case.id, CaseStatus.RESOLVED, NOW)
    await repo.transition(case.id, CaseStatus.FAILED, NOW)
    with pytest.raises(InvalidTransitionError):
        await repo.transition(case.id, CaseStatus.INVESTIGATING, NOW)


async def test_transition_missing_case(session: AsyncSession) -> None:
    with pytest.raises(CaseNotFoundError):
        await CaseRepository(session).transition(uuid4(), CaseStatus.INVESTIGATING, NOW)


async def test_list_by_status(session: AsyncSession) -> None:
    repo = CaseRepository(session)
    case = await new_case(session)
    await repo.transition(case.id, CaseStatus.INVESTIGATING, NOW)
    ids = {c.id for c in await repo.list_by_status(CaseStatus.INVESTIGATING, limit=1000)}
    assert case.id in ids


async def test_audit_append_and_list_in_order(session: AsyncSession) -> None:
    case = await new_case(session)
    audit = AuditRepository(session)
    await audit.append(case.id, ActorType.SYSTEM, "case.received", {"a": 1}, NOW)
    await audit.append(case.id, ActorType.AGENT, "investigation.done", {"b": 2}, NOW, "claude")
    events = await audit.list_for_case(case.id)
    assert [e.action for e in events] == ["case.received", "investigation.done"]
    assert events[1].actor_id == "claude"


async def test_audit_is_immutable_at_db_level(session: AsyncSession) -> None:
    case = await new_case(session)
    await AuditRepository(session).append(case.id, ActorType.SYSTEM, "x", {}, NOW)
    await session.commit()
    for stmt in (
        "UPDATE audit_events SET action = 'tampered'",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            await session.execute(text(stmt))
        await session.rollback()


async def test_approval_create_is_idempotent_per_case(session: AsyncSession) -> None:
    case = await new_case(session)
    repo = ApprovalRepository(session)
    a1, c1 = await repo.create_pending(uuid4(), case.id, make_fix(), NOW)
    a2, c2 = await repo.create_pending(uuid4(), case.id, make_fix(), NOW)
    assert c1 and not c2
    assert a1.id == a2.id
    assert a1.status is ApprovalStatus.PENDING


async def test_approval_decide_flow(session: AsyncSession) -> None:
    case = await new_case(session)
    repo = ApprovalRepository(session)
    approval, _ = await repo.create_pending(uuid4(), case.id, make_fix(), NOW)
    decided, changed = await repo.decide(approval.id, ApprovalStatus.APPROVED, "alice", NOW)
    assert changed and decided.status is ApprovalStatus.APPROVED
    assert decided.decided_by == "alice"
    again, changed2 = await repo.decide(approval.id, ApprovalStatus.APPROVED, "bob", NOW)
    assert not changed2 and again.decided_by == "alice"
    with pytest.raises(ApprovalAlreadyDecidedError):
        await repo.decide(approval.id, ApprovalStatus.REJECTED, "bob", NOW)


async def test_approval_edge_cases(session: AsyncSession) -> None:
    repo = ApprovalRepository(session)
    with pytest.raises(ApprovalNotFoundError):
        await repo.get(uuid4())
    with pytest.raises(ApprovalNotFoundError):
        await repo.decide(uuid4(), ApprovalStatus.APPROVED, "a", NOW)
    with pytest.raises(ValueError):
        await repo.decide(uuid4(), ApprovalStatus.PENDING, "a", NOW)


async def test_tool_call_record_and_list(session: AsyncSession) -> None:
    case = await new_case(session)
    repo = ToolCallRepository(session)
    saved = await repo.record(
        ToolCallRecord(
            case_id=case.id,
            tool=ToolName.SSI_STORE,
            request={"bic": "DEUTDEFF"},
            response={"ssi": "x"},
            latency_ms=12,
            ts=NOW,
        )
    )
    assert saved.id is not None
    listed = await repo.list_for_case(case.id)
    assert len(listed) == 1 and listed[0].tool is ToolName.SSI_STORE
