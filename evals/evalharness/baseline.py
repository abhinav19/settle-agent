"""Regression guard: a committed baseline that prompt/model changes must not fall below.

The guard compares accuracy and auto-resolve PRECISION at the baseline's own production
threshold (the number that decides safety), and treats extra unsafe automation as a failure.
Simulated runs can never create or satisfy a baseline.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from evalharness.metrics import accuracy, curve_point
from evalharness.models import CaseResult, RunMeta

MAX_DEGRADED_FRACTION = 0.05
"""More infrastructure-degraded runs than this makes the whole run untrustworthy as evidence."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Tolerances(_Model):
    accuracy: float = Field(default=0.02, ge=0.0, description="LLM output is not bit-deterministic")
    auto_precision: float = Field(default=0.01, ge=0.0)
    unsafe_auto_extra: int = Field(
        default=0, ge=0, description="Extra unsafe auto-resolutions allowed"
    )


class Baseline(_Model):
    dataset_version: str
    run_id: str
    model: str
    prompt_version: str
    threshold: float = Field(ge=0.0, le=1.0)
    n_cases: int
    accuracy: float
    auto_precision: float | None
    auto_recall: float | None
    unsafe_auto: int
    created_at: datetime
    tolerances: Tolerances = Tolerances()


class Finding(_Model):
    level: Literal["fail", "warn", "info"]
    metric: str
    message: str


class GuardReport(_Model):
    findings: list[Finding]

    @property
    def passed(self) -> bool:
        return not any(f.level == "fail" for f in self.findings)


class BaselineError(ValueError):
    pass


def make_baseline(
    meta: RunMeta,
    results: Sequence[CaseResult],
    threshold: float,
    tolerances: Tolerances | None = None,
) -> Baseline:
    """Build a baseline from a REAL, complete, healthy run."""
    if meta.simulated:
        raise BaselineError("refusing to create a baseline from a simulated run")
    if meta.aborted or len(results) != meta.n_cases or meta.n_cases != meta.dataset_cases:
        raise BaselineError(
            "refusing to create a baseline from an incomplete run "
            f"({len(results)} of {meta.dataset_cases} cases, aborted={meta.aborted})"
        )
    degraded = sum(r.degraded for r in results) / len(results)
    if degraded > MAX_DEGRADED_FRACTION:
        raise BaselineError(f"{degraded:.0%} of the run was degraded by LLM/tool faults")
    point = curve_point(results, threshold)
    return Baseline(
        dataset_version=meta.dataset_version,
        run_id=meta.run_id,
        model=meta.model,
        prompt_version=meta.prompt_version,
        threshold=threshold,
        n_cases=len(results),
        accuracy=accuracy(results) or 0.0,
        auto_precision=point.precision,
        auto_recall=point.recall,
        unsafe_auto=point.unsafe_auto,
        created_at=datetime.now(UTC),
        tolerances=tolerances or Tolerances(),
    )


def check(baseline: Baseline, meta: RunMeta, results: Sequence[CaseResult]) -> GuardReport:
    """Compare a run against the baseline. Any 'fail' finding fails the guard."""
    f: list[Finding] = []

    def fail(metric: str, msg: str) -> None:
        f.append(Finding(level="fail", metric=metric, message=msg))

    if meta.simulated:
        fail("run", "a simulated run cannot be checked against a real baseline")
        return GuardReport(findings=f)
    if meta.dataset_version != baseline.dataset_version:
        fail(
            "dataset",
            f"run used dataset v{meta.dataset_version}, baseline is v{baseline.dataset_version}",
        )
        return GuardReport(findings=f)
    if meta.aborted or len(results) != baseline.n_cases or meta.n_cases != meta.dataset_cases:
        fail(
            "coverage",
            f"run has {len(results)} of {baseline.n_cases} cases (aborted={meta.aborted})",
        )
        return GuardReport(findings=f)
    degraded = sum(r.degraded for r in results) / len(results)
    if degraded > MAX_DEGRADED_FRACTION:
        fail("degraded", f"{degraded:.1%} of cases hit LLM/tool faults; the run is not trustworthy")

    tol = baseline.tolerances
    acc = accuracy(results) or 0.0
    if acc < baseline.accuracy - tol.accuracy:
        fail("accuracy", f"{acc:.3f} < baseline {baseline.accuracy:.3f} (tolerance {tol.accuracy})")
    point = curve_point(results, baseline.threshold)
    if baseline.auto_precision is not None:
        if point.precision is None:
            fail(
                "auto_precision",
                f"nothing is auto-resolved at the baseline threshold {baseline.threshold}",
            )
        elif point.precision < baseline.auto_precision - tol.auto_precision:
            fail(
                "auto_precision",
                f"{point.precision:.3f} < baseline {baseline.auto_precision:.3f} at threshold "
                f"{baseline.threshold} (tolerance {tol.auto_precision})",
            )
    if point.unsafe_auto > baseline.unsafe_auto + tol.unsafe_auto_extra:
        fail(
            "unsafe_auto",
            f"{point.unsafe_auto} unsafe auto-resolutions vs baseline {baseline.unsafe_auto}",
        )
    recall = point.recall or 0.0
    if baseline.auto_recall is not None and recall < baseline.auto_recall - 0.05:
        msg = (
            f"recall fell to {recall:.3f} (baseline {baseline.auto_recall:.3f}); "
            "safe, but less automation"
        )
        f.append(Finding(level="warn", metric="auto_recall", message=msg))
    if meta.model != baseline.model:
        f.append(
            Finding(
                level="info",
                metric="model",
                message=f"model changed {baseline.model} -> {meta.model}",
            )
        )
    if meta.prompt_version != baseline.prompt_version:
        f.append(
            Finding(
                level="info",
                metric="prompt",
                message=f"prompt changed {baseline.prompt_version} -> {meta.prompt_version}",
            )
        )
    return GuardReport(findings=f)
