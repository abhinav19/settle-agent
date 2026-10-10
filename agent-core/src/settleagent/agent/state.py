"""Graph state and the planned-call value type."""

from typing import Any, TypedDict
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from settleagent.domain.events import SettlementFailureEvent
from settleagent.domain.investigation import Evidence, InvestigationResult
from settleagent.domain.reason_codes import ToolName
from settleagent.tools.gateway import ToolOutcome

MARKET_PLACEHOLDER = "$market"
"""Argument value resolved at gather time from instrument / trade data."""


class PlannedCall(BaseModel):
    """A read-only tool call the agent intends to make."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: ToolName
    name: str
    args: dict[str, Any]

    @property
    def key(self) -> str:
        """Stable identity used to avoid repeating the same call."""
        items = ",".join(f"{k}={self.args[k]}" for k in sorted(self.args))
        return f"{self.tool.value}.{self.name}({items})"


class InvestigationState(TypedDict):
    """Everything one run carries. Isolated per ``case_id``; nodes return partial updates."""

    event: SettlementFailureEvent
    case_id: UUID
    trace_id: str
    pending: list[PlannedCall]
    done_keys: list[str]
    outcomes: list[ToolOutcome]
    evidence: list[Evidence]
    missing: list[str]
    steps_used: int
    analyze_rounds: int
    hypothesis: str
    sufficient: bool
    budget_exhausted: bool
    tokens: int
    input_tokens: int
    output_tokens: int
    model: str
    notes: list[str]
    result: InvestigationResult | None


def initial_state(
    event: SettlementFailureEvent, case_id: UUID, trace_id: str
) -> InvestigationState:
    return InvestigationState(
        event=event,
        case_id=case_id,
        trace_id=trace_id,
        pending=[],
        done_keys=[],
        outcomes=[],
        evidence=[],
        missing=[],
        steps_used=0,
        analyze_rounds=0,
        hypothesis="",
        sufficient=False,
        budget_exhausted=False,
        tokens=0,
        input_tokens=0,
        output_tokens=0,
        model="none",
        notes=[],
        result=None,
    )
