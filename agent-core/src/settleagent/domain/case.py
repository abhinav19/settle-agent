"""Case lifecycle, routing decision, approvals and audit records."""

from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any, Final
from uuid import UUID

from pydantic import AwareDatetime, Field

from settleagent.domain.base import DomainModel
from settleagent.domain.events import SettlementFailureEvent
from settleagent.domain.investigation import InvestigationResult, ProposedFix
from settleagent.domain.reason_codes import ToolName


class CaseStatus(StrEnum):
    """Case lifecycle; legal moves are defined in ``ALLOWED_TRANSITIONS``."""

    RECEIVED = "RECEIVED"
    INVESTIGATING = "INVESTIGATING"
    AUTO_RESOLVED = "AUTO_RESOLVED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"


class Decision(StrEnum):
    """Output of the deterministic confidence gate."""

    AUTO_RESOLVE = "AUTO_RESOLVE"
    HUMAN_REVIEW = "HUMAN_REVIEW"


class ApprovalStatus(StrEnum):
    """State of an approval request for a write fix."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ActorType(StrEnum):
    """Who performed an audited action."""

    AGENT = "agent"
    SYSTEM = "system"
    USER = "user"


ALLOWED_TRANSITIONS: Final = MappingProxyType(
    {
        CaseStatus.RECEIVED: frozenset({CaseStatus.INVESTIGATING, CaseStatus.FAILED}),
        CaseStatus.INVESTIGATING: frozenset(
            {
                CaseStatus.AUTO_RESOLVED,
                CaseStatus.AWAITING_APPROVAL,
                CaseStatus.ESCALATED,
                CaseStatus.FAILED,
            }
        ),
        CaseStatus.AWAITING_APPROVAL: frozenset(
            {CaseStatus.RESOLVED, CaseStatus.ESCALATED, CaseStatus.FAILED}
        ),
        CaseStatus.AUTO_RESOLVED: frozenset(),
        CaseStatus.RESOLVED: frozenset({CaseStatus.ESCALATED}),  # only via a reversal
        CaseStatus.ESCALATED: frozenset(),
        CaseStatus.FAILED: frozenset(),
    }
)
"""Legal status transitions. Terminal states map to an empty set.

A resolved write remediation can be reversed, which reopens the case as ESCALATED.
"""


class InvalidTransitionError(ValueError):
    """Raised when a case status change is not in ``ALLOWED_TRANSITIONS``."""

    def __init__(self, current: CaseStatus, target: CaseStatus) -> None:
        super().__init__(f"illegal case transition {current} -> {target}")
        self.current = current
        self.target = target


class Case(DomainModel):
    """A settlement-failure case and its investigation outcome."""

    id: UUID
    event_id: UUID
    status: CaseStatus
    event: SettlementFailureEvent
    result: InvestigationResult | None = None
    decision: Decision | None = None
    trace_id: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class Approval(DomainModel):
    """Approval request for a ``requires_write`` fix."""

    id: UUID
    case_id: UUID
    status: ApprovalStatus
    proposed_fix: ProposedFix
    decided_by: str | None = None
    decided_at: AwareDatetime | None = None
    created_at: AwareDatetime


class AuditEvent(DomainModel):
    """Immutable audit record (append-only)."""

    id: int | None = None
    case_id: UUID
    actor: ActorType
    actor_id: str | None = None
    action: Annotated[str, Field(min_length=1)]
    payload: dict[str, Any]
    ts: AwareDatetime


class ToolCallRecord(DomainModel):
    """Audit of one MCP tool call."""

    id: int | None = None
    case_id: UUID
    tool: ToolName
    request: dict[str, Any]
    response: dict[str, Any] | None
    latency_ms: Annotated[int, Field(ge=0)]
    error: str | None = None
    ts: AwareDatetime
