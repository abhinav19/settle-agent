"""The regression guard: what must fail, what must pass, and what may never become a baseline."""

from typing import Any

import pytest

from evalharness.baseline import (
    Baseline,
    BaselineError,
    Tolerances,
    check,
    make_baseline,
)
from evalharness.models import CaseResult
from tests.factories import meta, res


def good_run(n: int = 50) -> list[CaseResult]:
    """n auto-resolvable cases, all right and confident, plus a few correctly escalated ones."""
    clear = [res(conf=0.97) for _ in range(n)]
    hard = [
        res(
            conf=0.3,
            fix=None,
            pred="UNDETERMINED",
            auto=False,
            difficulty="ambiguous",
            expected_fix=None,
        )
        for _ in range(10)
    ]
    return clear + hard


def baseline_for(
    run: list[CaseResult], threshold: float = 0.9, tolerances: Tolerances | None = None
) -> Baseline:
    return make_baseline(meta(n_cases=len(run)), run, threshold, tolerances)


def levels(run: list[CaseResult], base: Baseline, **meta_over: Any) -> dict[str, str]:
    report = check(base, meta(n_cases=len(run), **meta_over), run)
    return {f.metric: f.level for f in report.findings}


# ------------------------------------------------------------ making a baseline
def test_baseline_records_the_run_at_the_chosen_threshold() -> None:
    run = good_run()
    b = baseline_for(run)
    assert b.threshold == 0.9 and b.n_cases == 60 and b.model == "claude-opus-5-5"
    assert b.auto_precision == 1.0 and b.auto_recall == 1.0 and b.unsafe_auto == 0
    assert b.accuracy == pytest.approx(
        50 / 60
    )  # the 10 hard cases are 'wrong' (UNDETERMINED vs truth)


def test_a_simulated_run_can_never_become_a_baseline() -> None:
    with pytest.raises(BaselineError, match="simulated"):
        make_baseline(meta(n_cases=60, simulated=True), good_run(), 0.9)


def test_an_incomplete_or_aborted_run_cannot_become_a_baseline() -> None:
    with pytest.raises(BaselineError, match="incomplete"):
        make_baseline(meta(n_cases=100), good_run(), 0.9)  # 60 results of 100
    with pytest.raises(BaselineError, match="incomplete"):
        make_baseline(meta(n_cases=60, aborted="budget"), good_run(), 0.9)


def test_a_run_dominated_by_infrastructure_faults_cannot_become_a_baseline() -> None:
    run = good_run() + [
        res(degraded=True, fix=None, pred="UNDETERMINED", conf=0.0) for _ in range(10)
    ]
    with pytest.raises(BaselineError, match="degraded"):
        make_baseline(meta(n_cases=len(run)), run, 0.9)


# --------------------------------------------------------------------- the guard
def test_an_identical_run_passes_cleanly() -> None:
    run = good_run()
    report = check(baseline_for(run), meta(n_cases=len(run)), run)
    assert report.passed and report.findings == []


def test_an_accuracy_drop_beyond_tolerance_fails_but_within_tolerance_passes() -> None:
    run = good_run()
    base = baseline_for(run, tolerances=Tolerances(accuracy=0.05))
    # wrong answers the agent was NOT confident about: they cost accuracy but are never automated
    one_wrong = [res(conf=0.5, pred="FUNDING")] + run[1:]  # -1/60 = -1.7 pts: within the 5 allowed
    assert check(base, meta(n_cases=60), one_wrong).passed
    five_wrong = [res(conf=0.5, pred="FUNDING") for _ in range(5)] + run[5:]  # -8.3 pts
    assert levels(five_wrong, base)["accuracy"] == "fail"


def test_a_precision_drop_at_the_production_threshold_fails() -> None:
    run = good_run()
    base = baseline_for(run, tolerances=Tolerances(auto_precision=0.0))
    # same number of cases, but one is now a confidently-wrong auto-resolution
    worse = [res(conf=0.95, pred="FUNDING")] + run[1:]
    report = check(base, meta(n_cases=len(worse)), worse)
    assert not report.passed
    assert {f.metric for f in report.findings if f.level == "fail"} >= {"auto_precision"}


def test_automating_an_unsafe_case_fails_even_when_precision_barely_moves() -> None:
    run = good_run(n=200)
    base = baseline_for(run, tolerances=Tolerances(auto_precision=0.5, accuracy=1.0))
    unsafe = res(conf=0.99, auto=False, difficulty="ambiguous", expected_fix=None)
    worse = [unsafe] + run[1:]  # a case that needed a human is now auto-resolved
    assert levels(worse, base)["unsafe_auto"] == "fail"


def test_nothing_auto_resolved_at_the_baseline_threshold_fails() -> None:
    run = good_run()
    base = baseline_for(run)
    timid = [res(conf=0.4) for _ in range(50)] + run[50:]  # now below the 0.9 threshold
    assert levels(timid, base)["auto_precision"] == "fail"


def test_lost_automation_is_a_warning_not_a_failure() -> None:
    run = good_run()
    base = baseline_for(run)
    timid = [res(conf=0.4) for _ in range(25)] + run[25:]  # half the automation, still all safe
    got = levels(timid, base)
    assert got.get("auto_recall") == "warn" and "auto_precision" not in got


def test_an_improvement_passes() -> None:
    weak = good_run()[:50] + [res(conf=0.97, pred="FUNDING") for _ in range(5)]
    better = good_run()[:55]
    base = baseline_for(weak, tolerances=Tolerances())
    assert check(base, meta(n_cases=len(weak)), better[: len(weak)]).passed


def test_a_run_from_another_dataset_version_is_not_comparable() -> None:
    run = good_run()
    assert levels(run, baseline_for(run), dataset_version="2") == {"dataset": "fail"}


def test_a_partial_run_is_not_comparable() -> None:
    run = good_run()
    base = baseline_for(run)
    assert levels(run[:30], base) == {"coverage": "fail"}
    assert levels(run, base, aborted="over budget") == {"coverage": "fail"}


def test_a_simulated_run_is_never_accepted_by_the_guard() -> None:
    run = good_run()
    assert levels(run, baseline_for(run), simulated=True) == {"run": "fail"}


def test_too_many_infrastructure_faults_invalidate_the_run() -> None:
    run = good_run()
    base = baseline_for(run)
    flaky = run[:-8] + [
        res(degraded=True, fix=None, pred="UNDETERMINED", conf=0.0) for _ in range(8)
    ]
    assert levels(flaky, base)["degraded"] == "fail"


def test_model_and_prompt_changes_are_reported_but_not_failures() -> None:
    run = good_run()
    base = baseline_for(run)
    got = levels(run, base, model="claude-sonnet-5-5", prompt_version="v2")
    assert got == {"model": "info", "prompt": "info"}
    assert check(base, meta(n_cases=60, model="claude-sonnet-5-5", prompt_version="v2"), run).passed


def test_a_sampled_run_is_not_a_full_run() -> None:
    """--sample runs are for measuring cost; they must not become or be judged against baselines."""
    run = good_run(n=10)
    with pytest.raises(BaselineError, match="incomplete"):
        make_baseline(meta(n_cases=len(run), dataset_cases=300), run, 0.9)
    full = good_run()
    base = baseline_for(full)
    assert levels(full, base, dataset_cases=300) == {"coverage": "fail"}
