"""Case lifecycle orchestration: intake an event, investigate, gate, persist the outcome.

Transaction discipline: every state change commits together with its audit rows, and no
database transaction is held open while the agent (LLM + tools) runs.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from settleagent.agent.graph import AgentRun, InvestigationAgent
from settleagent.domain.case import (
    ActorType,
    Case,
    CaseStatus,
    Decision,
    InvalidTransitionError,
    ToolCallRecord,
)
from settleagent.domain.events import SettlementFailureEvent
from settleagent.gate.policy import Policy
from settleagent.gate.router import route_with_reason
from settleagent.persistence.repositories import (
    ApprovalRepository,
    AuditRepository,
    CaseRepository,
    ToolCallRepository,
)

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
TERMINAL = frozenset(
    {CaseStatus.AUTO_RESOLVED, CaseStatus.RESOLVED, CaseStatus.ESCALATED, CaseStatus.FAILED}
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class CaseProcessor:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        agent: InvestigationAgent,
        policy: Policy,
        *,
        clock: Clock = utc_now,
        new_id: Callable[[], UUID] = uuid4,
        new_trace_id: Callable[[], str] = lambda: uuid4().hex,
    ) -> None:
        self._sessions = sessions
        self._agent = agent
        self._policy = policy
        self._clock = clock
        self._new_id = new_id
        self._new_trace_id = new_trace_id

    # ------------------------------------------------------------------ intake
    async def intake(self, event: SettlementFailureEvent) -> tuple[Case, bool]:
        """Durably create the case (idempotent on ``event_id``). Returns (case, created)."""
        now = self._clock()
        async with self._sessions() as session, session.begin():
            case, created = await CaseRepository(session).create_from_event(
                event, self._new_id(), now
            )
            if created:
                await AuditRepository(session).append(
                    case.id,
                    ActorType.SYSTEM,
                    "case.received",
                    {"event_id": str(event.event_id), "reason_code": event.reason_code.value},
                    now,
                )
        if created:
            log.info("case %s received (reason=%s)", case.id, event.reason_code.value)
        else:
            log.info("duplicate event %s ignored (case %s)", event.event_id, case.id)
        return case, created

    # ------------------------------------------------------------- investigate
    async def investigate(self, case_id: UUID) -> Case:
        """Run the agent for a case and persist the routed outcome. Safe to call twice."""
        async with self._sessions() as session, session.begin():
            repo = CaseRepository(session)
            case = await repo.get(case_id)
            if case.status in TERMINAL or case.status is CaseStatus.AWAITING_APPROVAL:
                return case
            if case.status is CaseStatus.RECEIVED:
                now = self._clock()
                try:
                    case = await repo.transition(case_id, CaseStatus.INVESTIGATING, now)
                except InvalidTransitionError:
                    log.info("case %s already being investigated elsewhere", case_id)
                    return await repo.get(case_id)
                await AuditRepository(session).append(
                    case_id, ActorType.SYSTEM, "case.investigation_started", {}, now
                )

        trace_id = self._new_trace_id()
        try:
            run = await self._agent.investigate(case.event, case_id, trace_id)
        except Exception as exc:  # noqa: BLE001 - an unexpected agent bug must fail the case, not the worker
            log.exception("case %s investigation crashed", case_id)
            return await self._fail(case_id, exc)
        try:
            return await self._complete(case_id, run, trace_id)
        except InvalidTransitionError:
            log.warning("case %s was finished concurrently; ignoring duplicate run", case_id)
            async with self._sessions() as session:
                return await CaseRepository(session).get(case_id)

    async def _fail(self, case_id: UUID, exc: Exception) -> Case:
        now = self._clock()
        async with self._sessions() as session, session.begin():
            case = await CaseRepository(session).transition(case_id, CaseStatus.FAILED, now)
            await AuditRepository(session).append(
                case_id,
                ActorType.SYSTEM,
                "investigation.failed",
                {"error_type": type(exc).__name__, "error": str(exc)[:500]},
                now,
            )
        return case

    async def _complete(self, case_id: UUID, run: AgentRun, trace_id: str) -> Case:
        result = run.result
        decision, reason = route_with_reason(result, self._policy)
        fix = result.proposed_fix
        now = self._clock()

        # Defence in depth: a write fix NEVER skips the approval gate, whatever the router said.
        needs_approval = (
            decision is Decision.AUTO_RESOLVE and fix is not None and fix.requires_write
        )
        if decision is Decision.HUMAN_REVIEW:
            target, action = CaseStatus.ESCALATED, "case.escalated"
        elif needs_approval:
            target, action = CaseStatus.AWAITING_APPROVAL, "approval.requested"
        else:
            target, action = CaseStatus.AUTO_RESOLVED, "case.auto_resolved"

        async with self._sessions() as session, session.begin():
            audit = AuditRepository(session)
            # Lock order: the case row FIRST (SELECT ... FOR UPDATE inside transition), then
            # child rows. Inserting children first takes FK key-share locks that two
            # concurrent runs would then deadlock on when upgrading to FOR UPDATE.
            case = await CaseRepository(session).transition(
                case_id, target, now, result=result, decision=decision, trace_id=trace_id
            )
            tool_calls = ToolCallRepository(session)
            for o in run.outcomes:
                await tool_calls.record(
                    ToolCallRecord(
                        case_id=case_id,
                        tool=o.tool,
                        request=o.args,
                        response=o.data,
                        latency_ms=o.latency_ms,
                        error=(f"{o.error.value}: {o.error_message}" if o.error else None),
                        ts=now,
                    )
                )
            await audit.append(
                case_id,
                ActorType.AGENT,
                "investigation.completed",
                _completed_payload(run),
                now,
                actor_id=run.model,
            )
            await audit.append(
                case_id,
                ActorType.SYSTEM,
                "gate.decision",
                {
                    "decision": decision.value,
                    "reason": reason,
                    "policy_version": self._policy.version,
                    "auto_threshold": self._policy.auto_threshold,
                },
                now,
            )
            if needs_approval and fix is not None:
                approval, _ = await ApprovalRepository(session).create_pending(
                    self._new_id(), case_id, fix, now
                )
                await audit.append(
                    case_id,
                    ActorType.SYSTEM,
                    action,
                    {"approval_id": str(approval.id), "fix_action": fix.action.value},
                    now,
                )
            else:
                await audit.append(case_id, ActorType.SYSTEM, action, {"status": target.value}, now)
        log.info(
            "case %s -> %s (decision=%s, confidence=%.2f)",
            case_id,
            target.value,
            decision.value,
            result.confidence,
        )
        return case

    # ---------------------------------------------------------------- recovery
    async def incomplete_case_ids(self) -> list[UUID]:
        """Cases whose investigation did not finish (e.g. process crashed mid-run)."""
        async with self._sessions() as session:
            cases = await CaseRepository(session).list_by_statuses(
                [CaseStatus.RECEIVED, CaseStatus.INVESTIGATING]
            )
        return [c.id for c in cases]


def _completed_payload(run: AgentRun) -> dict[str, Any]:
    r = run.result
    return {
        "category": r.reason_category.value,
        "confidence": r.confidence,
        "fix_action": r.proposed_fix.action.value if r.proposed_fix else None,
        "steps_used": run.steps_used,
        "tokens": run.tokens,
        "model": run.model,
        "notes": run.notes,
    }
