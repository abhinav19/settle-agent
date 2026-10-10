"""Investigation output contract: ``InvestigationResult`` v1.0."""

from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from settleagent.domain.base import DomainModel
from settleagent.domain.reason_codes import ReasonCategory, ToolName

UNDETERMINED_ROOT_CAUSE: Final = "undetermined"
LOW_CONFIDENCE_CEILING: Final = 0.2
"""Below this confidence the agent may not propose any fix."""


class FixAction(StrEnum):
    """Remediation actions the agent may propose."""

    AMEND_SSI = "AMEND_SSI"
    RESUBMIT = "RESUBMIT"
    PARTIAL_SETTLE = "PARTIAL_SETTLE"
    REBOOK_SETTLEMENT_DATE = "REBOOK_SETTLEMENT_DATE"
    CHASE_COUNTERPARTY = "CHASE_COUNTERPARTY"
    NO_ACTION = "NO_ACTION"


ACTION_REQUIRES_WRITE: Final = {
    FixAction.AMEND_SSI: True,
    FixAction.RESUBMIT: True,
    FixAction.PARTIAL_SETTLE: True,
    FixAction.REBOOK_SETTLEMENT_DATE: True,
    FixAction.CHASE_COUNTERPARTY: False,
    FixAction.NO_ACTION: False,
}
"""Whether each action changes state. A write action may never be declared read-only."""


class Evidence(DomainModel):
    """One finding, tied to the read-only tool that produced it."""

    source: ToolName
    statement: Annotated[str, Field(min_length=1)]
    raw: dict[str, Any]


class ProposedFix(DomainModel):
    """A remediation proposal. ``requires_write`` fixes always need approval."""

    action: FixAction
    parameters: dict[str, Any]
    requires_write: bool

    @model_validator(mode="after")
    def _write_actions_need_approval(self) -> Self:
        if ACTION_REQUIRES_WRITE[self.action] and not self.requires_write:
            raise ValueError(f"{self.action} changes state, so requires_write must be True")
        return self


class InvestigationResult(DomainModel):
    """Structured, evidence-backed root cause with a confidence score."""

    schema_version: Literal["1.0"]
    case_id: UUID
    root_cause: Annotated[str, Field(min_length=1)]
    reason_category: ReasonCategory
    evidence: list[Evidence]
    proposed_fix: ProposedFix | None
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    model: Annotated[str, Field(min_length=1)]
    tokens_used: Annotated[int, Field(ge=0)]
    reasoning_trace_id: Annotated[str, Field(min_length=1)]

    @model_validator(mode="after")
    def _enforce_invariants(self) -> Self:
        undetermined_text = self.root_cause.strip().lower() == UNDETERMINED_ROOT_CAUSE
        undetermined_cat = self.reason_category is ReasonCategory.UNDETERMINED
        if undetermined_text != undetermined_cat:
            raise ValueError("root_cause 'undetermined' must match reason_category UNDETERMINED")
        if not undetermined_cat and not self.evidence:
            raise ValueError("a determined root cause requires at least one Evidence")
        if self.confidence < LOW_CONFIDENCE_CEILING and self.proposed_fix is not None:
            raise ValueError(f"confidence < {LOW_CONFIDENCE_CEILING} forbids a proposed_fix")
        if undetermined_cat and self.proposed_fix is not None:
            raise ValueError("an undetermined result cannot carry a proposed_fix")
        return self
