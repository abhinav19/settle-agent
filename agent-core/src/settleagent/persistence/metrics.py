"""Aggregate operational metrics, computed in the database."""

from dataclasses import dataclass

from sqlalchemy import BigInteger, Float, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from settleagent.domain.case import ApprovalStatus, CaseStatus, Decision
from settleagent.persistence.models import ApprovalRow, AuditEventRow, CaseRow, ToolCallRow

INFRA_ERROR_KINDS = ("TIMEOUT", "UNAVAILABLE", "CIRCUIT_OPEN")
"""Tool errors meaning the server could not answer (not 'not found', which is a finding)."""
COMPLETED = "investigation.completed"

QUEUE_STATUSES = (CaseStatus.ESCALATED, CaseStatus.AWAITING_APPROVAL)
"""Cases a human has to look at: handed over (ESCALATED) or waiting for a decision."""


@dataclass(frozen=True)
class Metrics:
    total_cases: int
    by_status: dict[str, int]
    queue_depth: int
    pending_approvals: int
    decided_cases: int
    auto_resolve_decisions: int
    auto_resolve_rate: float | None
    average_confidence: float | None
    total_tokens: int
    investigations: int
    p50_run_latency_ms: float | None
    p95_run_latency_ms: float | None
    tool_calls: int
    tool_infra_failures: int
    tool_error_rate: float | None
    total_cost_usd: float
    average_cost_usd: float | None


async def total_cost(session: AsyncSession) -> float:
    """Cumulative LLM spend, from the audit trail (the source of truth for the budget)."""
    value = (
        await session.execute(
            select(
                func.coalesce(func.sum(cast(AuditEventRow.payload["cost_usd"].astext, Float)), 0.0)
            ).where(AuditEventRow.action == COMPLETED)
        )
    ).scalar_one()
    return float(value)


async def compute_metrics(session: AsyncSession) -> Metrics:
    by_status = {
        status: int(n)
        for status, n in (
            await session.execute(select(CaseRow.status, func.count()).group_by(CaseRow.status))
        ).all()
    }
    decisions = {
        d: int(n)
        for d, n in (
            await session.execute(
                select(CaseRow.decision, func.count())
                .where(CaseRow.decision.is_not(None))
                .group_by(CaseRow.decision)
            )
        ).all()
    }
    avg_conf = (
        await session.execute(
            select(func.avg(cast(CaseRow.result["confidence"].astext, Float))).where(
                CaseRow.result.is_not(None)
            )
        )
    ).scalar_one()
    tokens = (
        await session.execute(
            select(
                func.coalesce(func.sum(cast(AuditEventRow.payload["tokens"].astext, BigInteger)), 0)
            ).where(AuditEventRow.action == "investigation.completed")
        )
    ).scalar_one()
    pending = (
        await session.execute(
            select(func.count())
            .select_from(ApprovalRow)
            .where(ApprovalRow.status == ApprovalStatus.PENDING.value)
        )
    ).scalar_one()

    run_stats = (
        await session.execute(
            select(
                func.count(),
                func.percentile_cont(0.5).within_group(
                    cast(AuditEventRow.payload["duration_ms"].astext, Float)
                ),
                func.percentile_cont(0.95).within_group(
                    cast(AuditEventRow.payload["duration_ms"].astext, Float)
                ),
                func.coalesce(func.sum(cast(AuditEventRow.payload["cost_usd"].astext, Float)), 0.0),
                # COUNT(expr) skips NULLs: runs recorded before cost tracking have no cost_usd
                func.count(cast(AuditEventRow.payload["cost_usd"].astext, Float)),
            ).where(AuditEventRow.action == COMPLETED)
        )
    ).one()
    investigations, p50, p95 = int(run_stats[0]), run_stats[1], run_stats[2]
    cost_sum, costed_runs = run_stats[3], int(run_stats[4])
    tool_total = (await session.execute(select(func.count()).select_from(ToolCallRow))).scalar_one()
    tool_infra = (
        await session.execute(
            select(func.count())
            .select_from(ToolCallRow)
            .where(or_(*[ToolCallRow.error.like(f"{k}%") for k in INFRA_ERROR_KINDS]))
        )
    ).scalar_one()

    decided = sum(decisions.values())
    auto = decisions.get(Decision.AUTO_RESOLVE.value, 0)
    return Metrics(
        total_cases=sum(by_status.values()),
        by_status=by_status,
        queue_depth=sum(by_status.get(s.value, 0) for s in QUEUE_STATUSES),
        pending_approvals=int(pending),
        decided_cases=decided,
        auto_resolve_decisions=auto,
        auto_resolve_rate=(auto / decided) if decided else None,
        average_confidence=float(avg_conf) if avg_conf is not None else None,
        total_tokens=int(tokens),
        investigations=investigations,
        p50_run_latency_ms=float(p50) if p50 is not None else None,
        p95_run_latency_ms=float(p95) if p95 is not None else None,
        tool_calls=int(tool_total),
        tool_infra_failures=int(tool_infra),
        tool_error_rate=(tool_infra / tool_total) if tool_total else None,
        total_cost_usd=float(cost_sum),
        average_cost_usd=(float(cost_sum) / costed_runs) if costed_runs else None,
    )
