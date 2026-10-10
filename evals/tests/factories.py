from datetime import UTC, datetime
from typing import Any

from evalharness.models import CaseResult, RunMeta

_n = 0


def res(
    *,
    conf: float = 0.95,
    truth: str = "STATIC_DATA",
    pred: str | None = None,
    fix: str | None = "AMEND_SSI",
    expected_fix: str | None = "AMEND_SSI",
    auto: bool = True,
    difficulty: str = "clear",
    code: str = "SSIM",
    executable: bool | None = True,
    degraded: bool = False,
    cost: float = 0.05,
    latency: float = 3.0,
    **over: Any,
) -> CaseResult:
    """A CaseResult with sensible 'correct and confident' defaults."""
    global _n
    _n += 1
    fields: dict[str, Any] = {
        "case_id": f"C-{_n:04d}",
        "difficulty": difficulty,
        "reason_code": code,
        "expected_category": truth,
        "expected_fix_action": expected_fix,
        "auto_resolvable": auto,
        "predicted_category": pred if pred is not None else truth,
        "predicted_fix_action": fix,
        "requires_write": fix is not None,
        "executable": executable if fix is not None else None,
        "confidence": conf,
        "root_cause": "x",
        "steps_used": 7,
        "input_tokens": 8000,
        "output_tokens": 600,
        "cost_usd": cost,
        "latency_s": latency,
        "degraded": degraded,
        "notes": [],
        "trace_id": "t",
    }
    fields.update(over)
    return CaseResult(**fields)


def meta(**over: Any) -> RunMeta:
    fields: dict[str, Any] = {
        "run_id": "r1",
        "started_at": datetime(2026, 10, 10, tzinfo=UTC),
        "model": "claude-opus-5-5",
        "prompt_version": "v1",
        "dataset_version": "1",
        "effort": "medium",
        "concurrency": 4,
        "simulated": False,
        "n_cases": 0,
    }
    fields.update(over)
    fields.setdefault("dataset_cases", fields["n_cases"])  # a full run unless a test says otherwise
    return RunMeta(**fields)


def hand_checked_run() -> list[CaseResult]:
    """10 cases whose precision/recall are worked out by hand in test_metrics.py.

    7 are auto-resolvable in the truth. Confidences and outcomes:
      safe     : 0.95, 0.90, 0.85, 0.60
      clear, right category, WRONG fix            : 0.92
      clear, WRONG category                       : 0.88
      ambiguous, confidently wrong (unsafe)       : 0.91, 0.50
      clear but agent gave up (no fix)            : 0.10   (missed automation)
      underdetermined, correctly gave up          : 0.05
    """
    return [
        res(conf=0.95),
        res(conf=0.90),
        res(conf=0.85),
        res(conf=0.60),
        res(conf=0.92, fix="RESUBMIT"),
        res(conf=0.88, pred="FUNDING"),
        res(conf=0.91, auto=False, difficulty="ambiguous", expected_fix=None),
        res(conf=0.50, auto=False, difficulty="ambiguous", expected_fix=None),
        res(conf=0.10, fix=None, pred="UNDETERMINED"),
        res(
            conf=0.05,
            fix=None,
            truth="UNDETERMINED",
            auto=False,
            difficulty="underdetermined",
            expected_fix=None,
        ),
    ]
