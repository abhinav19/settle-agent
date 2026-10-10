"""Records the harness reads and writes. Strict: unknown fields are rejected, not ignored."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Difficulty = Literal["clear", "ambiguous", "underdetermined"]
UNDETERMINED = "UNDETERMINED"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvalCase(_Model):
    """One labelled case: the failure event plus the ground truth."""

    case_id: str
    difficulty: Difficulty
    event: dict[str, str]
    expected_category: str
    secondary_category: str | None = None
    expected_fix_action: str | None = None
    expected_requires_write: bool | None = None
    auto_resolvable: bool
    note: str

    @property
    def reason_code(self) -> str:
        return self.event["reason_code"]


class Manifest(_Model):
    dataset_version: str
    seed: int
    n_cases: int
    mix: dict[str, int]
    reason_codes: list[str]
    reference_data_sha256: str = Field(
        description="Hash of the synthetic reference data the labels were derived from"
    )
    cases_sha256: str


class CaseResult(_Model):
    """What the agent did on one case, with the ground truth denormalised alongside it so
    metrics need only this file."""

    case_id: str
    difficulty: Difficulty
    reason_code: str
    expected_category: str
    expected_fix_action: str | None
    auto_resolvable: bool

    predicted_category: str
    predicted_fix_action: str | None
    requires_write: bool | None
    executable: bool | None = Field(
        description="Would a write fix pass remediation's evidence check? None for no/read-only fix"
    )
    confidence: float = Field(ge=0.0, le=1.0)
    root_cause: str

    steps_used: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_s: float
    degraded: bool = Field(
        description="The LLM or a tool failed during the run — an infrastructure fault, "
        "not a modelling error"
    )
    error: str | None = None
    notes: list[str] = Field(default_factory=list)
    trace_id: str


class RunMeta(_Model):
    run_id: str
    started_at: datetime
    finished_at: datetime | None = None
    model: str
    prompt_version: str
    dataset_version: str
    effort: str | None
    concurrency: int
    simulated: bool = Field(
        description="True when no real model was called. Such runs prove the harness works; "
        "they say nothing about model quality and must never become a baseline."
    )
    n_cases: int = Field(description="Cases selected for THIS run (a sample or the full set)")
    dataset_cases: int = Field(
        description="Cases in the whole dataset; a full run has n_cases == dataset_cases"
    )
    n_completed: int = 0
    total_cost_usd: float = 0.0
    aborted: str | None = None
