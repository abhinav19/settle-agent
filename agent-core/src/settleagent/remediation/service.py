"""Approval-gated remediation: the only code path that can change state.

Invariants (each has a test):
- A write fix is executed ONLY after an explicit APPROVED decision; reject/pending/duplicate
  paths never reach the settlement system.
- Execution is idempotent (key = approval id) and every step is audited.
- Crash-safe: the decision is committed first, execution is keyed, then the case is resolved.
  Calling ``approve`` again after a crash or a transient failure resumes where it stopped.
- Lock order is always case row first, then approval row (no deadlocks with the processor).
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from settleagent.domain.case import (
    ActorType,
    Approval,
    ApprovalStatus,
    Case,
    CaseStatus,
    InvalidTransitionError,
)
from settleagent.domain.investigation import ProposedFix
from settleagent.persistence.repositories import (
    ApprovalRepository,
    AuditRepository,
    CaseRepository,
)
from settleagent.remediation.models import (
    CaseStateError,
    ExecutableFix,
    ExecutionRecord,
    FixNotExecutableError,
    RemediationOutcome,
    SettlementSystemError,
)
from settleagent.remediation.resolve import resolve_write_fix
from settleagent.remediation.system import SettlementSystem

log = logging.getLogger(__name__)


async def record_read_only_resolution(
    audit: AuditRepository, case_id: UUID, fix: ProposedFix, now: datetime
) -> None:
    """Auto-resolve path for ``requires_write=False`` fixes (e.g. NO_ACTION: a holiday).

    Nothing is changed in the settlement system, so no approval is needed — but it is audited.
    """
    await audit.append(
        case_id,
        ActorType.SYSTEM,
        "remediation.read_only_completed",
        {"fix_action": fix.action.value, "requires_write": False, "parameters": fix.parameters},
        now,
    )


class RemediationService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        system: SettlementSystem,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._system = system
        self._clock = clock

    # ----------------------------------------------------------------- approve
    async def approve(self, approval_id: UUID, decided_by: str) -> RemediationOutcome:
        """Approve and execute. Idempotent; resumes an interrupted approval."""
        now = self._clock()
        # Stage A — commit the decision (and nothing else) before touching the system.
        async with self._sessions() as session, session.begin():
            approvals, cases, audit = self._repos(session)
            approval = await approvals.get(approval_id)
            case = await cases.get_for_update(approval.case_id)  # lock order: case, then approval
            approval, changed = await approvals.decide(
                approval_id, ApprovalStatus.APPROVED, decided_by, now
            )  # raises ApprovalAlreadyDecidedError if it was REJECTED
            if changed:
                await audit.append(
                    case.id,
                    ActorType.USER,
                    "approval.approved",
                    {"approval_id": str(approval_id)},
                    now,
                    actor_id=decided_by,
                )
            if case.status is CaseStatus.RESOLVED:
                return RemediationOutcome(case=case, approval=approval, changed=False)
            if case.status is not CaseStatus.AWAITING_APPROVAL:
                raise CaseStateError(f"case is {case.status.value}, not awaiting approval")

        # Stage B — derive the fix from evidence and execute it (idempotent on the approval id).
        try:
            fix = self._executable(case)
            record = await self._system.execute(fix, str(approval_id))
        except FixNotExecutableError as exc:
            return await self._fail(approval, case.id, "fix not executable", str(exc), decided_by)
        except SettlementSystemError as exc:
            if exc.transient:
                log.warning("approval %s: transient settlement error: %s", approval_id, exc)
                raise  # decision stays committed; calling approve() again resumes
            return await self._fail(
                approval, case.id, "settlement system rejected", str(exc), decided_by
            )

        # Stage C — resolve the case and audit the execution.
        return await self._resolve(approval, case.id, fix, record, decided_by)

    async def _resolve(
        self,
        approval: Approval,
        case_id: UUID,
        fix: ExecutableFix,
        record: ExecutionRecord,
        decided_by: str,
    ) -> RemediationOutcome:
        now = self._clock()
        async with self._sessions() as session, session.begin():
            approvals, cases, audit = self._repos(session)
            await cases.get_for_update(case_id)
            try:
                case = await cases.transition(case_id, CaseStatus.RESOLVED, now)
            except InvalidTransitionError:  # a concurrent approve already finished it
                return RemediationOutcome(
                    case=await cases.get(case_id), approval=approval, changed=False
                )
            await audit.append(
                case_id,
                ActorType.SYSTEM,
                "remediation.executed",
                {
                    "approval_id": str(approval.id),
                    "action": fix.action.value,
                    "parameters": fix.parameters,
                    "inverse": fix.inverse,
                    "replayed": not record.created,
                },
                now,
                actor_id=decided_by,
            )
            return RemediationOutcome(
                case=case, approval=await approvals.get(approval.id), changed=True
            )

    async def _fail(
        self, approval: Approval, case_id: UUID, headline: str, detail: str, decided_by: str
    ) -> RemediationOutcome:
        """The approved fix cannot be applied: hand the case to a human, never half-apply."""
        now = self._clock()
        async with self._sessions() as session, session.begin():
            approvals, cases, audit = self._repos(session)
            await cases.get_for_update(case_id)
            try:
                case = await cases.transition(case_id, CaseStatus.ESCALATED, now)
            except InvalidTransitionError:
                return RemediationOutcome(
                    case=await cases.get(case_id), approval=approval, changed=False
                )
            await audit.append(
                case_id,
                ActorType.SYSTEM,
                "remediation.failed",
                {"approval_id": str(approval.id), "reason": headline, "detail": detail[:500]},
                now,
                actor_id=decided_by,
            )
            return RemediationOutcome(
                case=case, approval=await approvals.get(approval.id), changed=True
            )

    # ------------------------------------------------------------------ reject
    async def reject(
        self, approval_id: UUID, decided_by: str, reason: str | None = None
    ) -> RemediationOutcome:
        """Reject: nothing is executed; the case goes to the human queue. Idempotent."""
        now = self._clock()
        async with self._sessions() as session, session.begin():
            approvals, cases, audit = self._repos(session)
            approval = await approvals.get(approval_id)
            case = await cases.get_for_update(approval.case_id)
            approval, changed = await approvals.decide(
                approval_id, ApprovalStatus.REJECTED, decided_by, now
            )  # raises ApprovalAlreadyDecidedError if it was APPROVED
            if not changed:
                return RemediationOutcome(case=case, approval=approval, changed=False)
            if case.status is not CaseStatus.AWAITING_APPROVAL:
                raise CaseStateError(f"case is {case.status.value}, not awaiting approval")
            case = await cases.transition(case.id, CaseStatus.ESCALATED, now)
            await audit.append(
                case.id,
                ActorType.USER,
                "approval.rejected",
                {"approval_id": str(approval_id), "reason": reason},
                now,
                actor_id=decided_by,
            )
            return RemediationOutcome(case=case, approval=approval, changed=True)

    # ----------------------------------------------------------------- reverse
    async def reverse(
        self, approval_id: UUID, decided_by: str, reason: str | None = None
    ) -> RemediationOutcome:
        """Undo an executed remediation and reopen the case for a human. Idempotent."""
        async with self._sessions() as session:
            approval = await ApprovalRepository(session).get(approval_id)
            case = await CaseRepository(session).get(approval.case_id)
        if approval.status is not ApprovalStatus.APPROVED:
            raise CaseStateError("only an approved and executed remediation can be reversed")
        if case.status is CaseStatus.ESCALATED:
            return RemediationOutcome(case=case, approval=approval, changed=False)
        if case.status is not CaseStatus.RESOLVED:
            raise CaseStateError(f"case is {case.status.value}; nothing to reverse")

        record = await self._system.reverse(str(approval_id))  # idempotent in the system
        now = self._clock()
        async with self._sessions() as session, session.begin():
            approvals, cases, audit = self._repos(session)
            await cases.get_for_update(case.id)
            try:
                case = await cases.transition(case.id, CaseStatus.ESCALATED, now)
            except InvalidTransitionError:
                return RemediationOutcome(
                    case=await cases.get(case.id), approval=approval, changed=False
                )
            await audit.append(
                case.id,
                ActorType.USER,
                "remediation.reversed",
                {
                    "approval_id": str(approval_id),
                    "reason": reason,
                    "applied_inverse": record.inverse,
                },
                now,
                actor_id=decided_by,
            )
            return RemediationOutcome(case=case, approval=approval, changed=True)

    # --------------------------------------------------------------- internals
    @staticmethod
    def _executable(case: Case) -> ExecutableFix:
        if case.result is None:
            raise FixNotExecutableError("case has no investigation result")
        return resolve_write_fix(case.event, case.result)

    @staticmethod
    def _repos(session: AsyncSession) -> tuple[ApprovalRepository, CaseRepository, AuditRepository]:
        return ApprovalRepository(session), CaseRepository(session), AuditRepository(session)


__all__ = ["RemediationService", "record_read_only_resolution"]
