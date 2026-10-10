"""Approval-gated remediation against real Postgres, real MCP tools and an oracle LLM."""

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from settleagent.domain.case import ApprovalStatus, CaseStatus, Decision
from settleagent.domain.investigation import FixAction
from settleagent.persistence.models import SettlementActionRow
from settleagent.persistence.repositories import (
    ApprovalAlreadyDecidedError,
    ApprovalNotFoundError,
    ApprovalRepository,
)
from settleagent.remediation.models import (
    CaseStateError,
    ExecutableFix,
    ExecutionRecord,
    SettlementSystemError,
)
from settleagent.remediation.service import RemediationService
from settleagent.remediation.system import SyntheticSettlementSystem
from tests.doubles import RoutedLlm, oracle_reply, pick
from tests.integration.helpers import (
    NOW,
    audit_actions,
    load_case,
    make_processor,
    process_scenario,
)

pytestmark = pytest.mark.integration


class SpySystem:
    """Wraps the real synthetic system: counts calls and can inject failures."""

    def __init__(self, inner: SyntheticSettlementSystem) -> None:
        self.inner = inner
        self.executed: list[str] = []
        self.reversed: list[str] = []
        self.fail_next: list[SettlementSystemError] = []

    async def execute(self, fix: ExecutableFix, key: str) -> ExecutionRecord:
        if self.fail_next:
            raise self.fail_next.pop(0)
        self.executed.append(key)
        return await self.inner.execute(fix, key)

    async def reverse(self, key: str) -> ExecutionRecord:
        self.reversed.append(key)
        return await self.inner.reverse(key)


class Env:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.system = SpySystem(SyntheticSettlementSystem(sessions, clock=lambda: NOW))
        self.service = RemediationService(sessions, self.system, clock=lambda: NOW)

    async def awaiting(self, code: str = "SSIM", llm_reply: Any = None) -> tuple[UUID, UUID]:
        """Process a clear scenario to AWAITING_APPROVAL. Returns (case_id, approval_id)."""
        sc = pick("clear", code)
        llm = RoutedLlm(synthesize=llm_reply or oracle_reply(sc))
        case_id = await process_scenario(make_processor(self.sessions, llm), sc)
        assert (await load_case(self.sessions, case_id)).status is CaseStatus.AWAITING_APPROVAL
        async with self.sessions() as s:
            approval = await ApprovalRepository(s).get_by_case(case_id)
        assert approval is not None and approval.status is ApprovalStatus.PENDING
        return case_id, approval.id

    async def ledger(self, case_id: UUID) -> list[SettlementActionRow]:
        async with self.sessions() as s:
            rows = await s.execute(
                select(SettlementActionRow).where(SettlementActionRow.case_id == case_id)
            )
            return list(rows.scalars())


@pytest.fixture
def env(sessions: async_sessionmaker[AsyncSession]) -> Env:
    return Env(sessions)


# ------------------------------------------------------------- the safety property
async def test_nothing_is_executed_until_a_human_approves(env: Env) -> None:
    case_id, _ = await env.awaiting()
    assert env.system.executed == []
    assert await env.ledger(case_id) == []


async def test_approve_executes_once_and_resolves(env: Env) -> None:
    case_id, approval_id = await env.awaiting("SSIM")
    out = await env.service.approve(approval_id, "alice")
    assert out.changed and out.case.status is CaseStatus.RESOLVED
    assert out.approval.status is ApprovalStatus.APPROVED and out.approval.decided_by == "alice"
    [row] = await env.ledger(case_id)
    assert row.action == "AMEND_SSI" and row.reversed_at is None
    assert row.parameters["new_ssi_id"].endswith("-2")  # derived from evidence (the active SSI)
    assert row.inverse["op"] == "restore_ssi"
    assert row.idempotency_key == str(approval_id)
    actions = await audit_actions(env.sessions, case_id)
    assert actions[-2:] == ["approval.approved", "remediation.executed"]


async def test_approve_is_idempotent(env: Env) -> None:
    case_id, approval_id = await env.awaiting()
    first = await env.service.approve(approval_id, "alice")
    second = await env.service.approve(approval_id, "bob")
    assert first.changed and not second.changed
    assert second.case.status is CaseStatus.RESOLVED
    assert second.approval.decided_by == "alice"  # the original decision stands
    assert len(await env.ledger(case_id)) == 1 and env.system.executed == [str(approval_id)]
    actions = await audit_actions(env.sessions, case_id)
    assert actions.count("approval.approved") == 1 and actions.count("remediation.executed") == 1


async def test_concurrent_approvals_execute_exactly_once(env: Env) -> None:
    case_id, approval_id = await env.awaiting()
    results = await asyncio.gather(
        env.service.approve(approval_id, "a"), env.service.approve(approval_id, "b")
    )
    assert sorted(r.changed for r in results) == [False, True]  # exactly one did the work
    assert all(r.case.status is CaseStatus.RESOLVED for r in results)
    assert len(await env.ledger(case_id)) == 1
    actions = await audit_actions(env.sessions, case_id)
    assert actions.count("remediation.executed") == 1
    assert (await load_case(env.sessions, case_id)).status is CaseStatus.RESOLVED


async def test_reject_never_executes_and_escalates(env: Env) -> None:
    case_id, approval_id = await env.awaiting()
    out = await env.service.reject(approval_id, "alice", "SSI looks right to me")
    assert out.changed and out.case.status is CaseStatus.ESCALATED
    assert out.approval.status is ApprovalStatus.REJECTED
    assert env.system.executed == [] and await env.ledger(case_id) == []
    assert (await audit_actions(env.sessions, case_id))[-1] == "approval.rejected"
    again = await env.service.reject(approval_id, "alice")
    assert not again.changed


async def test_conflicting_decisions_are_refused(env: Env) -> None:
    _, rejected = await env.awaiting()
    await env.service.reject(rejected, "alice")
    with pytest.raises(ApprovalAlreadyDecidedError):
        await env.service.approve(rejected, "bob")
    assert env.system.executed == []  # a rejected approval can never be executed

    _, approved = await env.awaiting()
    await env.service.approve(approved, "alice")
    with pytest.raises(ApprovalAlreadyDecidedError):
        await env.service.reject(approved, "bob")


async def test_unknown_approval(env: Env) -> None:
    with pytest.raises(ApprovalNotFoundError):
        await env.service.approve(uuid4(), "alice")
    with pytest.raises(ApprovalNotFoundError):
        await env.service.reject(uuid4(), "alice")


# ------------------------------------------------------------------- resilience
async def test_transient_failure_keeps_the_decision_and_a_retry_completes(env: Env) -> None:
    case_id, approval_id = await env.awaiting()
    env.system.fail_next = [SettlementSystemError("db blip", transient=True)]
    with pytest.raises(SettlementSystemError):
        await env.service.approve(approval_id, "alice")
    async with env.sessions() as s:
        approval = await ApprovalRepository(s).get(approval_id)
    assert approval.status is ApprovalStatus.APPROVED  # decision is durable
    assert (await load_case(env.sessions, case_id)).status is CaseStatus.AWAITING_APPROVAL
    assert await env.ledger(case_id) == []

    out = await env.service.approve(approval_id, "alice")  # resume
    assert out.case.status is CaseStatus.RESOLVED and len(await env.ledger(case_id)) == 1
    actions = await audit_actions(env.sessions, case_id)
    assert actions.count("approval.approved") == 1 and actions.count("remediation.executed") == 1


async def test_crash_after_execution_is_recovered_without_a_second_write(env: Env) -> None:
    """Execution happened but the case was never resolved (process died between the steps)."""
    case_id, approval_id = await env.awaiting()
    env.system.fail_next = [SettlementSystemError("crash", transient=True)]
    with pytest.raises(SettlementSystemError):
        await env.service.approve(approval_id, "alice")
    # Simulate: the write landed in the ledger before the crash.
    case = await load_case(env.sessions, case_id)
    from settleagent.remediation.resolve import resolve_write_fix

    assert case.result is not None
    await env.system.inner.execute(resolve_write_fix(case.event, case.result), str(approval_id))

    out = await env.service.approve(approval_id, "alice")
    assert out.case.status is CaseStatus.RESOLVED
    assert len(await env.ledger(case_id)) == 1  # replayed, not duplicated


async def test_permanent_rejection_by_the_system_escalates(env: Env) -> None:
    case_id, approval_id = await env.awaiting()
    env.system.fail_next = [SettlementSystemError("trade locked", transient=False)]
    out = await env.service.approve(approval_id, "alice")
    assert out.case.status is CaseStatus.ESCALATED
    assert await env.ledger(case_id) == []
    assert (await audit_actions(env.sessions, case_id))[-1] == "remediation.failed"


# --------------------------------------------------------------------- reversal
async def test_reverse_undoes_the_action_and_reopens_the_case(env: Env) -> None:
    case_id, approval_id = await env.awaiting()
    with pytest.raises(CaseStateError):
        await env.service.reverse(approval_id, "alice")  # nothing executed yet
    await env.service.approve(approval_id, "alice")
    out = await env.service.reverse(approval_id, "carol", "wrong account")
    assert out.changed and out.case.status is CaseStatus.ESCALATED
    [row] = await env.ledger(case_id)
    assert row.reversed_at is not None
    assert (await audit_actions(env.sessions, case_id))[-1] == "remediation.reversed"
    again = await env.service.reverse(approval_id, "carol")
    assert not again.changed


async def test_a_rejected_approval_cannot_be_reversed(env: Env) -> None:
    _, approval_id = await env.awaiting()
    await env.service.reject(approval_id, "alice")
    with pytest.raises(CaseStateError):
        await env.service.reverse(approval_id, "alice")


# ------------------------------------------- verification against the case's evidence
async def test_fix_for_a_different_trade_is_never_offered_for_approval(
    env: Env, sessions: async_sessionmaker[AsyncSession]
) -> None:
    sc = pick("clear", "SSIM")
    base = oracle_reply(sc)

    def hijacked(user: str) -> dict[str, Any]:
        reply = base(user)
        reply["fix"]["parameters_json"] = '{"trade_id": "T-SOMEONE-ELSE"}'
        return reply

    case_id = await process_scenario(make_processor(sessions, RoutedLlm(synthesize=hijacked)), sc)
    case = await load_case(sessions, case_id)
    assert case.status is CaseStatus.ESCALATED and case.decision is Decision.HUMAN_REVIEW
    async with sessions() as s:
        assert await ApprovalRepository(s).get_by_case(case_id) is None
    assert env.system.executed == []


async def test_model_invented_ssi_is_never_offered_for_approval(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    sc = pick("clear", "SSIM")
    base = oracle_reply(sc)

    def invented(user: str) -> dict[str, Any]:
        reply = base(user)
        reply["fix"]["parameters_json"] = '{"new_ssi_id": "SSI-ATTACKER-9"}'
        return reply

    case_id = await process_scenario(make_processor(sessions, RoutedLlm(synthesize=invented)), sc)
    assert (await load_case(sessions, case_id)).status is CaseStatus.ESCALATED


@pytest.mark.parametrize("code", ["LACK", "MONY", "SSIM", "DENO", "LATE"])
async def test_every_clear_write_scenario_is_approvable_and_applies_the_expected_values(
    env: Env, code: str
) -> None:
    sc = pick("clear", code)
    case_id, approval_id = await env.awaiting(code)
    out = await env.service.approve(approval_id, "alice")
    assert out.case.status is CaseStatus.RESOLVED, code
    [row] = await env.ledger(case_id)
    assert (
        row.action == sc.expected_fix_action and row.parameters["trade_id"] == sc.event["trade_id"]
    )
    if code == "LACK":  # partial settlement is strictly less than the full quantity
        assert 0 < float(row.parameters["quantity"]) < float(sc.event["quantity"])
    if code == "DENO":  # whole lots only
        assert float(row.parameters["quantity"]) <= float(sc.event["quantity"])
    if code == "LATE":
        assert row.parameters["new_settlement_date"] > sc.event["settlement_date"]


async def test_read_only_auto_resolve_needs_no_approval_but_is_audited(
    env: Env, sessions: async_sessionmaker[AsyncSession]
) -> None:
    sc = pick("clear", "CALD")
    case_id = await process_scenario(
        make_processor(sessions, RoutedLlm(synthesize=oracle_reply(sc))), sc
    )
    assert (await load_case(sessions, case_id)).status is CaseStatus.AUTO_RESOLVED
    actions = await audit_actions(sessions, case_id)
    assert "remediation.read_only_completed" in actions
    assert await env.ledger(case_id) == [] and env.system.executed == []


# ----------------------------------------------------------- ledger guarantees
async def test_system_rejects_key_reuse_for_a_different_action(env: Env) -> None:
    case_id, approval_id = await env.awaiting()
    await env.service.approve(approval_id, "alice")
    case = await load_case(env.sessions, case_id)
    assert case.result is not None
    from settleagent.remediation.resolve import resolve_write_fix

    other = resolve_write_fix(case.event, case.result).model_copy(
        update={"parameters": {"trade_id": "x", "new_ssi_id": "y"}}
    )
    with pytest.raises(SettlementSystemError, match="different action") as exc:
        await env.system.inner.execute(other, str(approval_id))
    assert exc.value.transient is False


async def test_reversing_an_unknown_key_is_a_permanent_error(env: Env) -> None:
    with pytest.raises(SettlementSystemError) as exc:
        await env.system.inner.reverse("nope")
    assert exc.value.transient is False


async def test_ledger_is_tamper_proof_at_the_database_level(env: Env) -> None:
    _, approval_id = await env.awaiting()
    await env.service.approve(approval_id, "alice")
    await env.service.reverse(approval_id, "alice")
    async with env.sessions() as s:
        for stmt in (
            "UPDATE settlement_actions SET parameters = '{}'::jsonb",
            "UPDATE settlement_actions SET action = 'NO_ACTION'",
            "UPDATE settlement_actions SET reversed_at = NULL",  # un-reverse
            "DELETE FROM settlement_actions",
        ):
            with pytest.raises(DBAPIError, match="append-only"):
                await s.execute(text(stmt))
            await s.rollback()


# ------------------------------------------------- state conflicts and races
async def _force_status(env: Env, case_id: UUID, target: CaseStatus) -> None:
    from settleagent.persistence.repositories import CaseRepository

    async with env.sessions() as s, s.begin():
        await CaseRepository(s).transition(case_id, target, NOW)


async def test_approve_on_a_case_no_longer_awaiting_is_refused_and_rolls_back(env: Env) -> None:
    case_id, approval_id = await env.awaiting()
    await _force_status(env, case_id, CaseStatus.ESCALATED)  # e.g. an operator took it over
    with pytest.raises(CaseStateError, match="not awaiting approval"):
        await env.service.approve(approval_id, "alice")
    async with env.sessions() as s:
        approval = await ApprovalRepository(s).get(approval_id)
    assert approval.status is ApprovalStatus.PENDING  # the decision was rolled back with it
    assert env.system.executed == []


async def test_reject_on_a_case_no_longer_awaiting_is_refused_and_rolls_back(env: Env) -> None:
    case_id, approval_id = await env.awaiting()
    await _force_status(env, case_id, CaseStatus.ESCALATED)
    with pytest.raises(CaseStateError, match="not awaiting approval"):
        await env.service.reject(approval_id, "alice")
    async with env.sessions() as s:
        assert (await ApprovalRepository(s).get(approval_id)).status is ApprovalStatus.PENDING


async def test_fix_that_stopped_being_executable_escalates_instead_of_writing(env: Env) -> None:
    """Defence in depth: execution re-verifies the fix, even though intake already did."""
    case_id, approval_id = await env.awaiting()
    async with env.sessions() as s, s.begin():  # tamper with the stored fix (wrong trade)
        await s.execute(
            text(
                "UPDATE cases SET result = jsonb_set(result, '{proposed_fix,parameters}', "
                '\'{"trade_id": "T-SOMEONE-ELSE"}\') WHERE id = :id'
            ),
            {"id": case_id},
        )
    out = await env.service.approve(approval_id, "alice")
    assert out.case.status is CaseStatus.ESCALATED
    assert await env.ledger(case_id) == [] and env.system.executed == []
    assert (await audit_actions(env.sessions, case_id))[-1] == "remediation.failed"


async def test_concurrent_finisher_during_execution_is_handled(env: Env) -> None:
    """Another worker resolves the case while this one is executing: no double audit, no error."""
    case_id, approval_id = await env.awaiting()
    inner_execute = env.system.inner.execute

    async def execute_then_other_worker_resolves(fix: ExecutableFix, key: str) -> ExecutionRecord:
        record = await inner_execute(fix, key)
        await _force_status(env, case_id, CaseStatus.RESOLVED)
        return record

    env.system.inner.execute = execute_then_other_worker_resolves  # type: ignore[method-assign]
    out = await env.service.approve(approval_id, "alice")
    assert not out.changed and out.case.status is CaseStatus.RESOLVED
    assert "remediation.executed" not in await audit_actions(env.sessions, case_id)


async def test_concurrent_escalation_during_a_failed_execution_is_handled(env: Env) -> None:
    case_id, approval_id = await env.awaiting()

    async def fail_after_other_worker_escalates(fix: ExecutableFix, key: str) -> ExecutionRecord:
        await _force_status(env, case_id, CaseStatus.ESCALATED)
        raise SettlementSystemError("locked", transient=False)

    env.system.inner.execute = fail_after_other_worker_escalates  # type: ignore[method-assign]
    out = await env.service.approve(approval_id, "alice")
    assert not out.changed and out.case.status is CaseStatus.ESCALATED
    assert "remediation.failed" not in await audit_actions(env.sessions, case_id)


async def test_concurrent_reopen_during_reverse_is_handled(env: Env) -> None:
    case_id, approval_id = await env.awaiting()
    await env.service.approve(approval_id, "alice")
    inner_reverse = env.system.inner.reverse

    async def reverse_then_other_worker_reopens(key: str) -> ExecutionRecord:
        record = await inner_reverse(key)
        await _force_status(env, case_id, CaseStatus.ESCALATED)
        return record

    env.system.inner.reverse = reverse_then_other_worker_reopens  # type: ignore[method-assign]
    out = await env.service.reverse(approval_id, "alice")
    assert not out.changed and out.case.status is CaseStatus.ESCALATED
    assert "remediation.reversed" not in await audit_actions(env.sessions, case_id)


async def test_reverse_refuses_when_the_case_never_resolved(env: Env) -> None:
    _, approval_id = await env.awaiting()
    env.system.fail_next = [SettlementSystemError("blip", transient=True)]
    with pytest.raises(SettlementSystemError):
        await env.service.approve(approval_id, "alice")  # approved, but not executed/resolved
    with pytest.raises(CaseStateError, match="nothing to reverse"):
        await env.service.reverse(approval_id, "alice")


async def test_a_case_without_a_result_has_nothing_to_execute(
    env: Env, sessions: async_sessionmaker[AsyncSession]
) -> None:
    from settleagent.remediation.models import FixNotExecutableError
    from tests.doubles import fresh_event

    proc = make_processor(sessions, RoutedLlm())
    case, _ = await proc.intake(fresh_event(pick("clear", "SSIM")))
    with pytest.raises(FixNotExecutableError, match="no investigation result"):
        RemediationService._executable(case)  # noqa: SLF001


# ------------------------------------------------------- settlement-system outage
async def test_database_outage_is_a_transient_settlement_error() -> None:
    from settleagent.persistence.db import make_engine, make_session_factory

    engine = make_engine("postgresql+psycopg://x:y@127.0.0.1:1/none?connect_timeout=1")
    system = SyntheticSettlementSystem(make_session_factory(engine))
    fix = ExecutableFix(
        case_id=uuid4(),
        trade_id="T",
        action=FixAction.AMEND_SSI,
        parameters={"trade_id": "T", "new_ssi_id": "S"},
        inverse={"op": "restore_ssi"},
    )
    with pytest.raises(SettlementSystemError) as exec_err:
        await system.execute(fix, "k")
    assert exec_err.value.transient is True
    with pytest.raises(SettlementSystemError) as rev_err:
        await system.reverse("k")
    assert rev_err.value.transient is True
    await engine.dispose()
