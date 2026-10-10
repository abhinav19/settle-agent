"""Repositories. Each takes an ``AsyncSession``; the caller commits (unit of work).

Time is always passed in (injectable clock) — repositories never read the wall clock.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from settleagent.domain.case import (
    ALLOWED_TRANSITIONS,
    ActorType,
    Approval,
    ApprovalStatus,
    AuditEvent,
    Case,
    CaseStatus,
    Decision,
    InvalidTransitionError,
    ToolCallRecord,
)
from settleagent.domain.events import SettlementFailureEvent
from settleagent.domain.investigation import InvestigationResult, ProposedFix
from settleagent.persistence.models import (
    ApprovalRow,
    AuditEventRow,
    CaseRow,
    ToolCallRow,
)


class CaseNotFoundError(LookupError):
    """No case with the given id."""


class ApprovalNotFoundError(LookupError):
    """No approval with the given id."""


class ApprovalAlreadyDecidedError(RuntimeError):
    """The approval was already decided with a different outcome."""


def _load[M: BaseModel](model: type[M], data: dict[str, Any]) -> M:
    # Rows hold JSON-native values (str uuids/datetimes); rehydrate leniently.
    # extra="forbid" still applies, so schema drift is caught.
    return model.model_validate(data, strict=False)


def _case(row: CaseRow) -> Case:
    return Case(
        id=row.id,
        event_id=row.event_id,
        status=CaseStatus(row.status),
        event=_load(SettlementFailureEvent, row.event),
        result=_load(InvestigationResult, row.result) if row.result else None,
        decision=Decision(row.decision) if row.decision else None,
        trace_id=row.trace_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class CaseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create_from_event(
        self, event: SettlementFailureEvent, case_id: UUID, now: datetime
    ) -> tuple[Case, bool]:
        """Insert a RECEIVED case. Duplicate ``event_id`` is a no-op -> (existing, False)."""
        stmt = (
            pg_insert(CaseRow)
            .values(
                id=case_id,
                event_id=event.event_id,
                status=CaseStatus.RECEIVED.value,
                event=event.model_dump(mode="json"),
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=[CaseRow.event_id])
            .returning(CaseRow.id)
        )
        created = (await self._s.execute(stmt)).scalar_one_or_none() is not None
        row = (
            await self._s.execute(select(CaseRow).where(CaseRow.event_id == event.event_id))
        ).scalar_one()
        return _case(row), created

    async def get(self, case_id: UUID) -> Case:
        row = await self._s.get(CaseRow, case_id)
        if row is None:
            raise CaseNotFoundError(str(case_id))
        return _case(row)

    async def get_for_update(self, case_id: UUID) -> Case:
        """Read a case under a row lock. Always take this BEFORE locking child rows."""
        row = (
            await self._s.execute(select(CaseRow).where(CaseRow.id == case_id).with_for_update())
        ).scalar_one_or_none()
        if row is None:
            raise CaseNotFoundError(str(case_id))
        return _case(row)

    async def list_by_status(self, status: CaseStatus, limit: int = 100) -> list[Case]:
        stmt = (
            select(CaseRow)
            .where(CaseRow.status == status.value)
            .order_by(CaseRow.created_at)
            .limit(limit)
        )
        return [_case(r) for r in (await self._s.execute(stmt)).scalars()]

    async def list_by_statuses(
        self, statuses: Sequence[CaseStatus], limit: int = 1000
    ) -> list[Case]:
        stmt = (
            select(CaseRow)
            .where(CaseRow.status.in_([s.value for s in statuses]))
            .order_by(CaseRow.created_at)
            .limit(limit)
        )
        return [_case(r) for r in (await self._s.execute(stmt)).scalars()]

    async def transition(
        self,
        case_id: UUID,
        target: CaseStatus,
        now: datetime,
        *,
        result: InvestigationResult | None = None,
        decision: Decision | None = None,
        trace_id: str | None = None,
    ) -> Case:
        """Move a case to ``target`` under a row lock; illegal moves raise."""
        row = (
            await self._s.execute(select(CaseRow).where(CaseRow.id == case_id).with_for_update())
        ).scalar_one_or_none()
        if row is None:
            raise CaseNotFoundError(str(case_id))
        current = CaseStatus(row.status)
        if target not in ALLOWED_TRANSITIONS[current]:
            raise InvalidTransitionError(current, target)
        values: dict[str, Any] = {"status": target.value, "updated_at": now}
        if result is not None:
            values["result"] = result.model_dump(mode="json")
        if decision is not None:
            values["decision"] = decision.value
        if trace_id is not None:
            values["trace_id"] = trace_id
        await self._s.execute(update(CaseRow).where(CaseRow.id == case_id).values(**values))
        await self._s.refresh(row)
        return _case(row)


class AuditRepository:
    """Append-only. There is intentionally no update or delete method."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def append(
        self,
        case_id: UUID,
        actor: ActorType,
        action: str,
        payload: dict[str, Any],
        ts: datetime,
        actor_id: str | None = None,
    ) -> AuditEvent:
        row = AuditEventRow(
            case_id=case_id,
            actor=actor.value,
            actor_id=actor_id,
            action=action,
            payload=payload,
            ts=ts,
        )
        self._s.add(row)
        await self._s.flush()
        return self._to_domain(row)

    async def list_for_case(self, case_id: UUID) -> list[AuditEvent]:
        stmt = (
            select(AuditEventRow).where(AuditEventRow.case_id == case_id).order_by(AuditEventRow.id)
        )
        return [self._to_domain(r) for r in (await self._s.execute(stmt)).scalars()]

    @staticmethod
    def _to_domain(row: AuditEventRow) -> AuditEvent:
        return AuditEvent(
            id=row.id,
            case_id=row.case_id,
            actor=ActorType(row.actor),
            actor_id=row.actor_id,
            action=row.action,
            payload=row.payload,
            ts=row.ts,
        )


def _approval(row: ApprovalRow) -> Approval:
    return Approval(
        id=row.id,
        case_id=row.case_id,
        status=ApprovalStatus(row.status),
        proposed_fix=_load(ProposedFix, row.proposed_fix),
        decided_by=row.decided_by,
        decided_at=row.decided_at,
        created_at=row.created_at,
    )


class ApprovalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create_pending(
        self, approval_id: UUID, case_id: UUID, fix: ProposedFix, now: datetime
    ) -> tuple[Approval, bool]:
        """Idempotent per case: a second call returns the existing approval -> (it, False)."""
        stmt = (
            pg_insert(ApprovalRow)
            .values(
                id=approval_id,
                case_id=case_id,
                status=ApprovalStatus.PENDING.value,
                proposed_fix=fix.model_dump(mode="json"),
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=[ApprovalRow.case_id])
            .returning(ApprovalRow.id)
        )
        created = (await self._s.execute(stmt)).scalar_one_or_none() is not None
        row = (
            await self._s.execute(select(ApprovalRow).where(ApprovalRow.case_id == case_id))
        ).scalar_one()
        return _approval(row), created

    async def get(self, approval_id: UUID) -> Approval:
        row = await self._s.get(ApprovalRow, approval_id)
        if row is None:
            raise ApprovalNotFoundError(str(approval_id))
        return _approval(row)

    async def get_by_case(self, case_id: UUID) -> Approval | None:
        row = (
            await self._s.execute(select(ApprovalRow).where(ApprovalRow.case_id == case_id))
        ).scalar_one_or_none()
        return _approval(row) if row else None

    async def list_by_status(self, status: ApprovalStatus, limit: int = 100) -> list[Approval]:
        stmt = (
            select(ApprovalRow)
            .where(ApprovalRow.status == status.value)
            .order_by(ApprovalRow.created_at)
            .limit(limit)
        )
        return [_approval(r) for r in (await self._s.execute(stmt)).scalars()]

    async def decide(
        self, approval_id: UUID, status: ApprovalStatus, decided_by: str, now: datetime
    ) -> tuple[Approval, bool]:
        """Record a decision. Repeating the same decision is a no-op -> (approval, False).

        A conflicting second decision raises ``ApprovalAlreadyDecidedError``.
        """
        if status is ApprovalStatus.PENDING:
            raise ValueError("decision must be APPROVED or REJECTED")
        row = (
            await self._s.execute(
                select(ApprovalRow).where(ApprovalRow.id == approval_id).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise ApprovalNotFoundError(str(approval_id))
        if row.status != ApprovalStatus.PENDING.value:
            if row.status == status.value:
                return _approval(row), False
            raise ApprovalAlreadyDecidedError(f"{approval_id} already {row.status}")
        row.status = status.value
        row.decided_by = decided_by
        row.decided_at = now
        await self._s.flush()
        return _approval(row), True


class ToolCallRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def record(self, call: ToolCallRecord) -> ToolCallRecord:
        row = ToolCallRow(
            case_id=call.case_id,
            tool=call.tool.value,
            request=call.request,
            response=call.response,
            latency_ms=call.latency_ms,
            error=call.error,
            ts=call.ts,
        )
        self._s.add(row)
        await self._s.flush()
        return call.model_copy(update={"id": row.id})

    async def list_for_case(self, case_id: UUID) -> Sequence[ToolCallRecord]:
        stmt = select(ToolCallRow).where(ToolCallRow.case_id == case_id).order_by(ToolCallRow.id)
        return [
            ToolCallRecord.model_validate(
                {
                    "id": r.id,
                    "case_id": r.case_id,
                    "tool": r.tool,
                    "request": r.request,
                    "response": r.response,
                    "latency_ms": r.latency_ms,
                    "error": r.error,
                    "ts": r.ts,
                },
                strict=False,
            )
            for r in (await self._s.execute(stmt)).scalars()
        ]
