"""Case lifecycle orchestration: intake an event, investigate, gate, persist the outcome.

Transaction discipline: every state change commits together with its audit rows, and no
database transaction is held open while the agent (LLM + tools) runs.
"""

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
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
from settleagent.observability.cost import CostTracker
from settleagent.observability.logging import bind_case
from settleagent.observability.pricing import cost_usd
from settleagent.observability.telemetry import NOOP, Telemetry
from settleagent.persistence.repositories import (
    ApprovalRepository,
    AuditRepository,
    CaseRepository,
    ToolCallRepository,
)
from settleagent.remediation.models import FixNotExecutableError
from settleagent.remediation.resolve import resolve_write_fix
from settleagent.remediation.service import record_read_only_resolution

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
        telemetry: Telemetry = NOOP,
        cost_tracker: CostTracker | None = None,
        perf_counter: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._sessions = sessions
        self._agent = agent
        self._policy = policy
        self._clock = clock
        self._new_id = new_id
        self._new_trace_id = new_trace_id
        self._telemetry = telemetry
        self._cost_tracker = cost_tracker
        self._perf = perf_counter

    # ------------------------------------------------------------------ intake
    async def intake(self, event: SettlementFailureEvent) -> tuple[Case, bool]:
        """Durably create the case (idempotent on ``event_id``). Returns (case, created)."""
        reason = event.reason_code.value
        with self._telemetry.span("case.intake", event_id=str(event.event_id), reason_code=reason):
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
                        {"event_id": str(event.event_id), "reason_code": reason},
                        now,
                    )
            trace.get_current_span().set_attribute("case_id", str(case.id))
            with bind_case(case.id):
                if created:
                    log.info("case received", extra={"reason_code": reason})
                else:
                    log.info("duplicate event ignored", extra={"event_id": str(event.event_id)})
        self._telemetry.cases_received.add(
            1, {"outcome": "created" if created else "duplicate", "reason_code": reason}
        )
        return case, created

    # ------------------------------------------------------------- investigate
    async def investigate(self, case_id: UUID) -> Case:
        """Run the agent for a case and persist the routed outcome. Safe to call twice.

        One root span (``case.investigate``) per run; node and tool spans hang beneath it, and
        its trace id is stored on the case so the dashboard can link to it.
        """
        with (
            bind_case(case_id),
            self._telemetry.span("case.investigate", case_id=str(case_id)) as span,
        ):
            case = await self._investigate(case_id, span, self._perf())
            span.set_attribute("case.status", case.status.value)
            return case

    async def _investigate(self, case_id: UUID, span: trace.Span, started: float) -> Case:
        async with self._sessions() as session, session.begin():
            repo = CaseRepository(session)
            case = await repo.get(case_id)
            span.set_attribute("reason_code", case.event.reason_code.value)
            span.set_attribute("trade_id", case.event.trade_id)
            if case.status in TERMINAL or case.status is CaseStatus.AWAITING_APPROVAL:
                span.set_attribute("case.noop", True)
                return case
            if case.status is CaseStatus.RECEIVED:
                now = self._clock()
                try:
                    case = await repo.transition(case_id, CaseStatus.INVESTIGATING, now)
                except InvalidTransitionError:
                    log.info("case already being investigated elsewhere")
                    span.set_attribute("case.noop", True)
                    return await repo.get(case_id)
                await AuditRepository(session).append(
                    case_id, ActorType.SYSTEM, "case.investigation_started", {}, now
                )

        if self._cost_tracker is not None and self._cost_tracker.exceeded:
            span.set_attribute("budget.blocked", True)
            return await self._defer_over_budget(case_id, case.event)

        trace_id = self._telemetry.current_trace_id() or self._new_trace_id()
        try:
            run = await self._agent.investigate(case.event, case_id, trace_id)
        except Exception as exc:  # noqa: BLE001 - an unexpected agent bug must fail the case, not the worker
            log.exception("investigation crashed")
            span.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"[:200]))
            return await self._fail(case_id, case.event, exc, started)
        try:
            return await self._complete(case_id, case.event, run, trace_id, started)
        except InvalidTransitionError:
            log.warning("case was finished concurrently; ignoring duplicate run")
            span.set_attribute("case.noop", True)
            async with self._sessions() as session:
                return await CaseRepository(session).get(case_id)

    async def _defer_over_budget(self, case_id: UUID, event: SettlementFailureEvent) -> Case:
        """The cost ceiling is reached: hand the case to a human WITHOUT calling the LLM."""
        assert self._cost_tracker is not None  # noqa: S101 - only called when a tracker exists
        now = self._clock()
        payload = {
            "total_usd": round(self._cost_tracker.total_usd, 6),
            "ceiling_usd": self._cost_tracker.ceiling_usd,
        }
        async with self._sessions() as session, session.begin():
            case = await CaseRepository(session).transition(
                case_id, CaseStatus.ESCALATED, now, decision=Decision.HUMAN_REVIEW
            )
            await AuditRepository(session).append(
                case_id, ActorType.SYSTEM, "budget.ceiling_reached", payload, now
            )
        log.warning("cost ceiling reached; case escalated without investigation", extra=payload)
        self._telemetry.budget_blocked.add(1, {"reason_code": event.reason_code.value})
        self._telemetry.cases_processed.add(
            1,
            {
                "status": "ESCALATED",
                "decision": "budget_blocked",
                "reason_code": event.reason_code.value,
            },
        )
        return case

    async def _fail(
        self, case_id: UUID, event: SettlementFailureEvent, exc: Exception, started: float
    ) -> Case:
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
        self._telemetry.cases_processed.add(
            1, {"status": "FAILED", "decision": "none", "reason_code": event.reason_code.value}
        )
        self._telemetry.case_duration.record(self._perf() - started, {"status": "FAILED"})
        return case

    async def _complete(
        self,
        case_id: UUID,
        event: SettlementFailureEvent,
        run: AgentRun,
        trace_id: str,
        started: float,
    ) -> Case:
        result = run.result
        decision, reason = route_with_reason(result, self._policy)
        fix = result.proposed_fix
        now = self._clock()
        duration_s = self._perf() - started
        cost = cost_usd(run.model, run.input_tokens, run.output_tokens)

        # A write fix is only offered for approval if it can be re-derived from the case's own
        # evidence (right trade, supported values). Otherwise a human handles it instead.
        if decision is Decision.AUTO_RESOLVE and fix is not None and fix.requires_write:
            try:
                resolve_write_fix(event, result)
            except FixNotExecutableError as exc:
                decision, reason = Decision.HUMAN_REVIEW, f"write fix not executable: {exc}"

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
                _completed_payload(run, cost, duration_s),
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
                if fix is not None and decision is Decision.AUTO_RESOLVE:
                    await record_read_only_resolution(audit, case_id, fix, now)
                await audit.append(case_id, ActorType.SYSTEM, action, {"status": target.value}, now)
        log.info(
            "case routed",
            extra={
                "status": target.value,
                "decision": decision.value,
                "confidence": result.confidence,
                "tokens": run.tokens,
                "cost_usd": cost,
            },
        )
        trace.get_current_span().set_attributes(
            {
                "reason_code": event.reason_code.value,
                "category": result.reason_category.value,
                "confidence": result.confidence,
                "tokens": run.tokens,
                "cost_usd": cost,
                "decision": decision.value,
                "model": run.model,
            }
        )
        t = self._telemetry
        t.cases_processed.add(
            1,
            {
                "status": target.value,
                "decision": decision.value,
                "reason_code": event.reason_code.value,
            },
        )
        t.case_duration.record(duration_s, {"status": target.value})
        t.case_cost.record(cost, {"model": run.model})
        if self._cost_tracker is not None:
            self._cost_tracker.record(case_id, cost)
        return case

    # ---------------------------------------------------------------- recovery
    async def incomplete_case_ids(self) -> list[UUID]:
        """Cases whose investigation did not finish (e.g. process crashed mid-run)."""
        async with self._sessions() as session:
            cases = await CaseRepository(session).list_by_statuses(
                [CaseStatus.RECEIVED, CaseStatus.INVESTIGATING]
            )
        return [c.id for c in cases]


def _completed_payload(run: AgentRun, cost: float, duration_s: float) -> dict[str, Any]:
    r = run.result
    return {
        "category": r.reason_category.value,
        "confidence": r.confidence,
        "fix_action": r.proposed_fix.action.value if r.proposed_fix else None,
        "steps_used": run.steps_used,
        "tokens": run.tokens,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "cost_usd": cost,
        "duration_ms": int(duration_s * 1000),
        "model": run.model,
        "notes": run.notes,
    }
