"""API request/response models (the public contract; snapshot-tested via OpenAPI)."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from settleagent.domain.case import (
    Approval,
    AuditEvent,
    Case,
    CaseStatus,
    Decision,
    ToolCallRecord,
)


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Page[T](ApiModel):
    items: list[T]
    total: int = Field(description="Total rows matching the filter, across all pages")
    limit: int
    offset: int


class CaseSummary(ApiModel):
    """One row of the ops queue."""

    id: UUID
    event_id: UUID
    status: CaseStatus
    decision: Decision | None
    trade_id: str
    isin: str
    reason_code: str
    category: str | None = Field(description="Agent's root-cause category, once investigated")
    root_cause: str | None
    confidence: float | None
    fix_action: str | None
    requires_write: bool | None
    trace_id: str | None
    created_at: datetime
    updated_at: datetime


class ApprovalItem(ApiModel):
    approval: Approval
    case: CaseSummary


class CaseUsage(ApiModel):
    """What the investigation cost (from the audit trail). Fields are null for older cases."""

    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    steps_used: int | None = None


class CaseDetail(ApiModel):
    """Everything an operator needs: event, agent result with evidence, fix, trail."""

    summary: CaseSummary
    case: Case
    approval: Approval | None
    audit: list[AuditEvent]
    tool_calls: list[ToolCallRecord]
    usage: CaseUsage | None = Field(description="Tokens, USD cost and latency of the investigation")


Operator = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


class DecisionRequest(ApiModel):
    decided_by: Operator = Field(description="Operator identity recorded in the audit trail")
    reason: str | None = Field(default=None, max_length=1000)


class ActionResponse(ApiModel):
    changed: bool = Field(description="False when the call was an idempotent replay")
    case: CaseSummary
    approval: Approval


class CheckResultModel(ApiModel):
    ok: bool
    detail: str
    latency_ms: int


class ReadyResponse(ApiModel):
    status: str
    checks: dict[str, CheckResultModel]


class HealthResponse(ApiModel):
    status: str


class BudgetStatus(ApiModel):
    ceiling_usd: float
    used_usd: float
    used_fraction: float
    level: str = Field(description="ok | warning (>= warn fraction) | exceeded (>= ceiling)")


class MetricsResponse(ApiModel):
    total_cases: int
    by_status: dict[str, int]
    queue_depth: int = Field(description="ESCALATED + AWAITING_APPROVAL: cases needing a human")
    pending_approvals: int
    decided_cases: int = Field(description="Cases the confidence gate has routed")
    auto_resolve_decisions: int
    auto_resolve_rate: float | None = Field(description="auto-resolve decisions / decided cases")
    average_confidence: float | None
    total_tokens: int = Field(description="LLM tokens spent across all investigations")
    investigations: int
    p50_run_latency_ms: float | None
    p95_run_latency_ms: float | None
    tool_calls: int
    tool_infra_failures: int = Field(
        description="Timeouts, outages, open circuits (not 'not found')"
    )
    tool_error_rate: float | None
    total_cost_usd: float
    average_cost_usd: float | None
    budget: BudgetStatus


class ErrorResponse(ApiModel):
    detail: str
