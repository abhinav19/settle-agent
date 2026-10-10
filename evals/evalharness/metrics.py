"""Scoring. Pure functions over ``CaseResult`` lists — no I/O, no model calls.

Definitions (these ARE the claims the scorecard makes):

* **root-cause correct**  — predicted category == expected category (UNDETERMINED is the right
  answer for underdetermined cases).
* **auto-resolved at threshold t** — what production would do: the agent proposed a fix, is not
  UNDETERMINED, confidence >= t, a write fix passes remediation's evidence check, and the run was
  not degraded by an LLM/tool fault.
* **safe auto-resolution** — the case IS auto-resolvable in the ground truth AND the category
  AND fix action are both right. Anything else that gets auto-resolved is a false positive;
  automating an ambiguous/underdetermined case is the worst kind ("unsafe").
* **precision** = safe / auto-resolved.  **recall** = safe / all auto-resolvable cases.
"""

import math
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence

from pydantic import BaseModel, ConfigDict

from evalharness.models import UNDETERMINED, CaseResult


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------- per-case
def category_correct(r: CaseResult) -> bool:
    return r.predicted_category == r.expected_category


def fix_correct(r: CaseResult) -> bool:
    return r.predicted_fix_action == r.expected_fix_action


def auto_resolved(r: CaseResult, threshold: float) -> bool:
    return (
        not r.degraded
        and r.predicted_fix_action is not None
        and r.predicted_category != UNDETERMINED
        and r.confidence >= threshold
        and r.executable is not False
    )


def safe_auto(r: CaseResult) -> bool:
    return r.auto_resolvable and category_correct(r) and fix_correct(r)


# ------------------------------------------------------------------------ stats
def wilson_lower(successes: int, n: int, z: float = 1.96) -> float | None:
    """Lower bound of the Wilson score interval: a small-sample-honest precision."""
    if n == 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denom)


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile (q in 0..100), matching numpy's default."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    rank = (len(s) - 1) * q / 100
    lo, hi = math.floor(rank), math.ceil(rank)
    return s[lo] + (s[hi] - s[lo]) * (rank - lo)


# ------------------------------------------------------------------ PR curve
class CurvePoint(_Model):
    threshold: float
    auto_count: int
    true_pos: int
    false_pos: int
    false_neg: int
    precision: float | None
    recall: float | None
    wilson_lower: float | None
    auto_rate: float
    escalation_rate: float
    unsafe_auto: int  # auto-resolved although the truth says a human should decide
    wrong_auto: int  # auto-resolvable case, but wrong category or fix


def curve_point(results: Sequence[CaseResult], threshold: float) -> CurvePoint:
    positives = sum(r.auto_resolvable for r in results)
    auto = [r for r in results if auto_resolved(r, threshold)]
    tp = sum(safe_auto(r) for r in auto)
    fp = len(auto) - tp
    return CurvePoint(
        threshold=threshold,
        auto_count=len(auto),
        true_pos=tp,
        false_pos=fp,
        false_neg=positives - tp,
        precision=(tp / len(auto)) if auto else None,
        recall=(tp / positives) if positives else None,
        wilson_lower=wilson_lower(tp, len(auto)),
        auto_rate=len(auto) / len(results) if results else 0.0,
        escalation_rate=1 - len(auto) / len(results) if results else 0.0,
        unsafe_auto=sum(not r.auto_resolvable for r in auto),
        wrong_auto=sum(r.auto_resolvable and not safe_auto(r) for r in auto),
    )


def pr_curve(
    results: Sequence[CaseResult], thresholds: Iterable[float] | None = None
) -> list[CurvePoint]:
    """Precision/recall as a function of the auto-resolve threshold (default 0.00 .. 1.00)."""
    ts = list(thresholds) if thresholds is not None else [i / 100 for i in range(101)]
    return [curve_point(results, t) for t in ts]


# -------------------------------------------------------------------- accuracy
def accuracy(
    results: Sequence[CaseResult], pred: Callable[[CaseResult], bool] = category_correct
) -> float | None:
    return (sum(pred(r) for r in results) / len(results)) if results else None


def accuracy_by(
    results: Sequence[CaseResult], key: Callable[[CaseResult], str]
) -> dict[str, tuple[int, float]]:
    """{group: (n, root-cause accuracy)}"""
    groups: dict[str, list[CaseResult]] = defaultdict(list)
    for r in results:
        groups[key(r)].append(r)
    return {
        k: (len(v), sum(category_correct(r) for r in v) / len(v)) for k, v in sorted(groups.items())
    }


def confusion(results: Sequence[CaseResult]) -> dict[str, dict[str, int]]:
    """{expected category: {predicted category: count}}"""
    matrix: dict[str, Counter[str]] = defaultdict(Counter)
    for r in results:
        matrix[r.expected_category][r.predicted_category] += 1
    return {e: dict(sorted(c.items())) for e, c in sorted(matrix.items())}


class CalibrationBin(_Model):
    low: float
    high: float
    n: int
    mean_confidence: float
    accuracy: float


def calibration(
    results: Sequence[CaseResult], bins: int = 10
) -> tuple[list[CalibrationBin], float | None]:
    """Reliability bins and the expected calibration error (ECE): do 0.9-confidence answers get
    ~90% right?  Lower ECE = confidence can be trusted as a probability."""
    if not results:
        return [], None
    grouped: dict[int, list[CaseResult]] = defaultdict(list)
    for r in results:
        grouped[min(int(r.confidence * bins), bins - 1)].append(r)
    out, ece = [], 0.0
    for i in sorted(grouped):
        g = grouped[i]
        mean_conf = sum(r.confidence for r in g) / len(g)
        acc = sum(category_correct(r) for r in g) / len(g)
        out.append(
            CalibrationBin(
                low=i / bins, high=(i + 1) / bins, n=len(g), mean_confidence=mean_conf, accuracy=acc
            )
        )
        ece += len(g) / len(results) * abs(acc - mean_conf)
    return out, ece


# ------------------------------------------------------------------ cost/latency
class Distribution(_Model):
    total: float
    mean: float | None
    p50: float | None
    p90: float | None
    p95: float | None
    p99: float | None
    max: float | None


def distribution(values: Sequence[float]) -> Distribution:
    return Distribution(
        total=sum(values),
        mean=(sum(values) / len(values)) if values else None,
        p50=percentile(values, 50),
        p90=percentile(values, 90),
        p95=percentile(values, 95),
        p99=percentile(values, 99),
        max=max(values) if values else None,
    )


# --------------------------------------------------------------------- summary
class Summary(_Model):
    n: int
    n_degraded: int
    accuracy: float | None
    accuracy_excluding_degraded: float | None
    accuracy_by_difficulty: dict[str, tuple[int, float]]
    accuracy_by_reason_code: dict[str, tuple[int, float]]
    fix_accuracy_clear: float | None
    ece: float | None
    cost_usd: Distribution
    latency_s: Distribution
    steps: Distribution
    tokens: Distribution


def summarize(results: Sequence[CaseResult]) -> Summary:
    healthy = [r for r in results if not r.degraded]
    clear = [r for r in results if r.auto_resolvable]
    _, ece = calibration(results)
    return Summary(
        n=len(results),
        n_degraded=len(results) - len(healthy),
        accuracy=accuracy(results),
        accuracy_excluding_degraded=accuracy(healthy),
        accuracy_by_difficulty=accuracy_by(results, lambda r: r.difficulty),
        accuracy_by_reason_code=accuracy_by(results, lambda r: r.reason_code),
        fix_accuracy_clear=accuracy(clear, fix_correct),
        ece=ece,
        cost_usd=distribution([r.cost_usd for r in results]),
        latency_s=distribution([r.latency_s for r in results]),
        steps=distribution([float(r.steps_used) for r in results]),
        tokens=distribution([float(r.input_tokens + r.output_tokens) for r in results]),
    )
