"""FastAPI control plane. Handlers are thin: persistence reads and the RemediationService."""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from settleagent.api import schemas as s
from settleagent.api.readiness import ReadinessCheck, run_checks
from settleagent.api.security import require_api_key
from settleagent.domain.case import ApprovalStatus, AuditEvent, Case, CaseStatus
from settleagent.persistence.metrics import COMPLETED, QUEUE_STATUSES, compute_metrics
from settleagent.persistence.repositories import (
    ApprovalAlreadyDecidedError,
    ApprovalNotFoundError,
    ApprovalRepository,
    AuditRepository,
    CaseNotFoundError,
    CaseRepository,
    CaseSort,
    ToolCallRepository,
)
from settleagent.remediation.models import CaseStateError, RemediationOutcome, SettlementSystemError
from settleagent.remediation.service import RemediationService

log = logging.getLogger(__name__)

MAX_PAGE = 200


@dataclass
class ApiDeps:
    sessions: async_sessionmaker[AsyncSession]
    remediation: RemediationService
    api_key: str
    readiness: Mapping[str, ReadinessCheck] = field(default_factory=dict)
    cost_ceiling_usd: float = 50.0
    budget_warn_fraction: float = 0.8
    on_shutdown: Callable[[], Awaitable[None]] | None = None


def get_deps(request: Request) -> ApiDeps:
    deps: ApiDeps = request.app.state.deps
    return deps


Deps = Annotated[ApiDeps, Depends(get_deps)]
Limit = Annotated[int, Query(ge=1, le=MAX_PAGE)]
Offset = Annotated[int, Query(ge=0)]

AUTH_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": s.ErrorResponse, "description": "Missing or invalid API key"}
}
CONFLICT: dict[int | str, dict[str, object]] = {
    404: {"model": s.ErrorResponse, "description": "Approval not found"},
    409: {"model": s.ErrorResponse, "description": "Conflicting decision or case state"},
    503: {"model": s.ErrorResponse, "description": "Settlement system unavailable; retry"},
}


def summarize(case: Case) -> s.CaseSummary:
    r, fix = case.result, case.result.proposed_fix if case.result else None
    return s.CaseSummary(
        id=case.id,
        event_id=case.event_id,
        status=case.status,
        decision=case.decision,
        trade_id=case.event.trade_id,
        isin=case.event.isin,
        reason_code=case.event.reason_code.value,
        category=r.reason_category.value if r else None,
        root_cause=r.root_cause if r else None,
        confidence=r.confidence if r else None,
        fix_action=fix.action.value if fix else None,
        requires_write=fix.requires_write if fix else None,
        trace_id=case.trace_id,
        created_at=case.created_at,
        updated_at=case.updated_at,
    )


def usage_from_audit(audit: list[AuditEvent]) -> s.CaseUsage | None:
    """The latest ``investigation.completed`` payload, as typed usage figures."""
    payload = next((a.payload for a in reversed(audit) if a.action == COMPLETED), None)
    if payload is None:
        return None
    return s.CaseUsage(
        model=payload.get("model"),
        input_tokens=payload.get("input_tokens"),
        output_tokens=payload.get("output_tokens"),
        cost_usd=payload.get("cost_usd"),
        duration_ms=payload.get("duration_ms"),
        steps_used=payload.get("steps_used"),
    )


def budget_status(used: float, ceiling: float, warn_fraction: float) -> s.BudgetStatus:
    fraction = used / ceiling
    level = "exceeded" if fraction >= 1.0 else "warning" if fraction >= warn_fraction else "ok"
    return s.BudgetStatus(ceiling_usd=ceiling, used_usd=used, used_fraction=fraction, level=level)


def _action(outcome: RemediationOutcome) -> s.ActionResponse:
    return s.ActionResponse(
        changed=outcome.changed, case=summarize(outcome.case), approval=outcome.approval
    )


def build_router() -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_api_key)], responses=AUTH_ERRORS)

    @router.get(
        "/cases", response_model=s.Page[s.CaseSummary], tags=["cases"], summary="List cases"
    )
    async def list_cases(
        deps: Deps,
        status: Annotated[list[CaseStatus] | None, Query(description="Repeat to filter")] = None,
        queue: Annotated[bool, Query(description="Only cases needing a human")] = False,
        reason_code: str | None = None,
        sort: CaseSort = CaseSort.CREATED_AT,
        order: Literal["asc", "desc"] = "desc",
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> s.Page[s.CaseSummary]:
        statuses = list(status or [])
        if queue:
            statuses += [q for q in QUEUE_STATUSES if q not in statuses]
        async with deps.sessions() as session:
            cases, total = await CaseRepository(session).list_page(
                statuses=statuses or None,
                reason_code=reason_code,
                sort=sort,
                descending=order == "desc",
                limit=limit,
                offset=offset,
            )
        return s.Page(items=[summarize(c) for c in cases], total=total, limit=limit, offset=offset)

    @router.get(
        "/cases/{case_id}",
        response_model=s.CaseDetail,
        tags=["cases"],
        summary="Case with evidence, fix, approval and audit trail",
        responses={404: {"model": s.ErrorResponse, "description": "Case not found"}},
    )
    async def get_case(case_id: UUID, deps: Deps) -> s.CaseDetail:
        async with deps.sessions() as session:
            case = await CaseRepository(session).get(case_id)
            audit = await AuditRepository(session).list_for_case(case_id)
            return s.CaseDetail(
                summary=summarize(case),
                case=case,
                approval=await ApprovalRepository(session).get_by_case(case_id),
                audit=audit,
                tool_calls=list(await ToolCallRepository(session).list_for_case(case_id)),
                usage=usage_from_audit(audit),
            )

    @router.get(
        "/approvals",
        response_model=s.Page[s.ApprovalItem],
        tags=["approvals"],
        summary="List approvals",
    )
    async def list_approvals(
        deps: Deps,
        status: Annotated[ApprovalStatus | Literal["ALL"], Query()] = ApprovalStatus.PENDING,
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> s.Page[s.ApprovalItem]:
        async with deps.sessions() as session:
            approvals, total = await ApprovalRepository(session).list_page(
                None if status == "ALL" else ApprovalStatus(status), limit, offset
            )
            cases = await CaseRepository(session).get_many([a.case_id for a in approvals])
        items = [s.ApprovalItem(approval=a, case=summarize(cases[a.case_id])) for a in approvals]
        return s.Page(items=items, total=total, limit=limit, offset=offset)

    @router.post(
        "/approvals/{approval_id}/approve",
        response_model=s.ActionResponse,
        tags=["approvals"],
        summary="Approve and execute the proposed fix (idempotent)",
        responses=CONFLICT,
    )
    async def approve(approval_id: UUID, body: s.DecisionRequest, deps: Deps) -> s.ActionResponse:
        return _action(await deps.remediation.approve(approval_id, body.decided_by))

    @router.post(
        "/approvals/{approval_id}/reject",
        response_model=s.ActionResponse,
        tags=["approvals"],
        summary="Reject the proposed fix; the case goes to the human queue (idempotent)",
        responses=CONFLICT,
    )
    async def reject(approval_id: UUID, body: s.DecisionRequest, deps: Deps) -> s.ActionResponse:
        return _action(await deps.remediation.reject(approval_id, body.decided_by, body.reason))

    @router.post(
        "/approvals/{approval_id}/reverse",
        response_model=s.ActionResponse,
        tags=["approvals"],
        summary="Reverse an executed fix and reopen the case (idempotent)",
        responses=CONFLICT,
    )
    async def reverse(approval_id: UUID, body: s.DecisionRequest, deps: Deps) -> s.ActionResponse:
        return _action(await deps.remediation.reverse(approval_id, body.decided_by, body.reason))

    @router.get(
        "/metrics/summary",
        response_model=s.MetricsResponse,
        tags=["metrics"],
        summary="Operational KPIs",
    )
    async def metrics_summary(deps: Deps) -> s.MetricsResponse:
        async with deps.sessions() as session:
            m = await compute_metrics(session)
        return s.MetricsResponse(
            **m.__dict__,
            budget=budget_status(
                m.total_cost_usd, deps.cost_ceiling_usd, deps.budget_warn_fraction
            ),
        )

    return router


def create_app(deps: ApiDeps) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if deps.on_shutdown is not None:
            await deps.on_shutdown()

    app = FastAPI(
        title="SettleAgent control plane",
        version="1.0.0",
        description="Cases, approvals and metrics for the settlement-failure agent. "
        "All endpoints except /health and /ready require the `X-API-Key` header.",
        lifespan=lifespan,
    )
    app.state.deps = deps
    app.include_router(build_router())

    @app.get("/health", response_model=s.HealthResponse, tags=["ops"], summary="Liveness")
    async def health() -> s.HealthResponse:
        return s.HealthResponse(status="ok")

    @app.get(
        "/ready",
        response_model=s.ReadyResponse,
        tags=["ops"],
        summary="Readiness: database, Kafka and the OTel collector are reachable",
        responses={503: {"model": s.ReadyResponse, "description": "A dependency is unavailable"}},
    )
    async def ready(response: Response) -> s.ReadyResponse:
        results = await run_checks(deps.readiness)
        ok = all(r.ok for r in results.values())
        response.status_code = 200 if ok else 503
        return s.ReadyResponse(
            status="ready" if ok else "unavailable",
            checks={
                n: s.CheckResultModel(ok=r.ok, detail=r.detail, latency_ms=r.latency_ms)
                for n, r in results.items()
            },
        )

    def error(
        status_code: int, exc: Exception, headers: dict[str, str] | None = None
    ) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=status_code, headers=headers)

    @app.exception_handler(CaseNotFoundError)
    async def _case_missing(_: Request, exc: CaseNotFoundError) -> JSONResponse:
        return error(404, exc)

    @app.exception_handler(ApprovalNotFoundError)
    async def _approval_missing(_: Request, exc: ApprovalNotFoundError) -> JSONResponse:
        return error(404, exc)

    @app.exception_handler(ApprovalAlreadyDecidedError)
    async def _already_decided(_: Request, exc: ApprovalAlreadyDecidedError) -> JSONResponse:
        return error(409, exc)

    @app.exception_handler(CaseStateError)
    async def _bad_state(_: Request, exc: CaseStateError) -> JSONResponse:
        return error(409, exc)

    @app.exception_handler(SettlementSystemError)
    async def _settlement(_: Request, exc: SettlementSystemError) -> JSONResponse:
        log.warning("settlement system error surfaced to API: %s", exc)
        if exc.transient:  # the approval decision is durable; calling approve again resumes
            return error(503, exc, {"Retry-After": "5"})
        return error(502, exc)

    return app
