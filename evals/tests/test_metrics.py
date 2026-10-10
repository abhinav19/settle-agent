"""Metrics against hand-computed values. If these change, the scorecard's claims changed."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from evalharness.metrics import (
    accuracy,
    accuracy_by,
    auto_resolved,
    calibration,
    confusion,
    curve_point,
    distribution,
    percentile,
    pr_curve,
    safe_auto,
    summarize,
    wilson_lower,
)
from evalharness.threshold import select_threshold
from tests.factories import hand_checked_run, res


# ----------------------------------------------------------- per-case predicates
def test_auto_resolved_needs_a_fix_a_cause_confidence_and_a_healthy_executable_run() -> None:
    assert auto_resolved(res(conf=0.9), 0.9)  # boundary is inclusive
    assert not auto_resolved(res(conf=0.8999), 0.9)
    assert not auto_resolved(res(fix=None), 0.0)  # nothing to do
    assert not auto_resolved(res(pred="UNDETERMINED", truth="UNDETERMINED"), 0.0)
    assert not auto_resolved(res(executable=False), 0.0)  # remediation would refuse it
    assert not auto_resolved(res(degraded=True), 0.0)  # infra fault: never counted as automation
    assert auto_resolved(res(executable=None), 0.0)  # read-only fix: no executability check


def test_safe_auto_requires_truth_category_and_fix_to_all_agree() -> None:
    assert safe_auto(res())
    assert not safe_auto(res(auto=False))  # automating a case that needed a human
    assert not safe_auto(res(pred="FUNDING"))
    assert not safe_auto(res(fix="RESUBMIT"))


# ----------------------------------------------------------------------- PR curve
def test_pr_curve_matches_the_hand_worked_example() -> None:
    run = hand_checked_run()
    at = {t: curve_point(run, t) for t in (0.0, 0.5, 0.9, 0.95, 1.0)}

    p = at[0.0]  # everything with a fix is auto-resolved
    assert (p.auto_count, p.true_pos, p.false_pos, p.false_neg) == (8, 4, 4, 3)
    assert p.precision == 0.5 and p.recall == pytest.approx(4 / 7)
    assert (p.unsafe_auto, p.wrong_auto) == (2, 2)

    p = at[0.9]  # 0.95 and 0.90 (safe), 0.92 (wrong fix), 0.91 (ambiguous)
    assert (p.auto_count, p.true_pos, p.false_pos, p.false_neg) == (4, 2, 2, 5)
    assert p.precision == 0.5 and p.recall == pytest.approx(2 / 7)
    assert (p.unsafe_auto, p.wrong_auto) == (1, 1)
    assert p.auto_rate == pytest.approx(0.4) and p.escalation_rate == pytest.approx(0.6)

    p = at[0.95]  # only the single most confident (and safe) case
    assert (p.auto_count, p.true_pos, p.false_pos) == (1, 1, 0)
    assert p.precision == 1.0 and p.recall == pytest.approx(1 / 7)
    assert p.wilson_lower == pytest.approx(0.2065, abs=1e-3)  # 1/1 is weak evidence

    p = at[1.0]
    assert p.auto_count == 0 and p.precision is None and p.wilson_lower is None
    assert p.recall == 0.0 and p.escalation_rate == 1.0


def test_curve_has_101_default_points_and_recall_never_rises_with_the_threshold() -> None:
    curve = pr_curve(hand_checked_run())
    assert len(curve) == 101 and curve[0].threshold == 0.0 and curve[-1].threshold == 1.0
    recalls = [p.recall or 0.0 for p in curve]
    assert all(a >= b for a, b in zip(recalls, recalls[1:], strict=False))
    counts = [p.auto_count for p in curve]
    assert all(a >= b for a, b in zip(counts, counts[1:], strict=False))


@given(
    st.lists(
        st.tuples(st.floats(0, 1), st.booleans(), st.booleans(), st.booleans()),
        min_size=1,
        max_size=40,
    ),
    st.floats(0, 1),
)
def test_curve_invariants_hold_for_any_results(
    rows: list[tuple[float, bool, bool, bool]], t: float
) -> None:
    run = [
        res(
            conf=c,
            auto=a,
            pred=None if ok else "OTHER",
            fix="AMEND_SSI" if a else None,
            expected_fix="AMEND_SSI" if a else None,
        )
        for c, a, ok, _ in rows
    ]
    p = curve_point(run, t)
    assert p.true_pos + p.false_pos == p.auto_count
    assert p.true_pos + p.false_neg == sum(r.auto_resolvable for r in run)
    assert p.unsafe_auto + p.wrong_auto <= p.false_pos
    assert 0.0 <= p.auto_rate <= 1.0 and p.auto_rate + p.escalation_rate == pytest.approx(1.0)
    if p.precision is not None:
        assert (
            0.0 <= p.precision <= 1.0
            and p.wilson_lower is not None
            and p.wilson_lower <= p.precision + 1e-9
        )


# ----------------------------------------------------------- threshold selection
def test_select_threshold_takes_the_highest_recall_that_meets_the_precision_bar() -> None:
    curve = pr_curve(hand_checked_run())
    only_perfect = select_threshold(curve, min_precision=1.0, min_support=1)
    assert only_perfect is not None
    assert (
        only_perfect.precision == 1.0 and only_perfect.threshold == 0.95
    )  # ties -> most conservative
    loose = select_threshold(curve, min_precision=0.5, min_support=1)
    assert loose is not None and loose.recall == pytest.approx(4 / 7)  # highest recall wins
    assert (loose.recall or 0) > (only_perfect.recall or 0)  # a looser bar buys more automation


def test_select_threshold_refuses_to_pick_when_nothing_qualifies() -> None:
    curve = pr_curve(hand_checked_run())
    assert select_threshold(curve, min_precision=1.0, min_support=2) is None  # one lucky case
    assert select_threshold(curve, min_precision=0.99, min_support=100) is None
    assert (
        select_threshold(pr_curve([res(conf=0.5, pred="X")]), min_precision=0.5, min_support=1)
        is None
    )


def test_select_threshold_ignores_a_lucky_tiny_high_confidence_tail() -> None:
    lucky = [res(conf=0.99) for _ in range(3)] + [res(conf=0.7, pred="X") for _ in range(30)]
    chosen = select_threshold(pr_curve(lucky), min_precision=0.98, min_support=20)
    assert chosen is None  # 3 perfect cases do not meet the support bar


# ------------------------------------------------------------------ statistics
@pytest.mark.parametrize(
    ("q", "expected"), [(0, 1.0), (50, 2.5), (90, 3.7), (100, 4.0), (25, 1.75)]
)
def test_percentile_matches_numpy_linear_interpolation(q: float, expected: float) -> None:
    assert percentile([4, 1, 3, 2], q) == pytest.approx(expected)


def test_percentile_edge_cases() -> None:
    assert percentile([], 50) is None and percentile([7.0], 99) == 7.0


def test_wilson_lower_bound() -> None:
    assert wilson_lower(0, 0) is None
    assert wilson_lower(50, 100) == pytest.approx(0.4038, abs=1e-3)
    assert wilson_lower(100, 100) == pytest.approx(0.9630, abs=1e-3)
    assert (wilson_lower(100, 100) or 0) > (wilson_lower(10, 10) or 0)  # more evidence, tighter


def test_distribution() -> None:
    d = distribution([1.0, 2.0, 3.0, 4.0])
    assert d.total == 10 and d.mean == 2.5 and d.p50 == 2.5 and d.max == 4
    empty = distribution([])
    assert empty.total == 0 and empty.mean is None and empty.p95 is None


# ---------------------------------------------------------------------- accuracy
def test_accuracy_and_grouping() -> None:
    run = hand_checked_run()
    assert accuracy(run) == pytest.approx(
        8 / 10
    )  # only the wrong-category case and the gave-up case miss
    by = accuracy_by(run, lambda r: r.difficulty)
    # 7 clear: 4 safe + wrong-fix (category still right) = 5 correct; the other two miss
    n_clear, acc_clear = by["clear"]
    assert n_clear == 7 and acc_clear == pytest.approx(5 / 7)
    assert by["ambiguous"] == (2, 1.0) and by["underdetermined"] == (1, 1.0)
    assert accuracy([]) is None


def test_confusion_matrix() -> None:
    m = confusion(hand_checked_run())
    assert m["STATIC_DATA"] == {"FUNDING": 1, "STATIC_DATA": 7, "UNDETERMINED": 1}
    assert m["UNDETERMINED"] == {"UNDETERMINED": 1}
    assert sum(sum(row.values()) for row in m.values()) == 10


def test_calibration_and_ece_by_hand() -> None:
    run = [res(conf=0.95), res(conf=0.95, pred="X"), res(conf=0.15, pred="X"), res(conf=0.05)]
    bins, ece = calibration(run)
    by = {b.low: b for b in bins}
    assert (
        by[0.9].n == 2
        and by[0.9].accuracy == 0.5
        and by[0.9].mean_confidence == pytest.approx(0.95)
    )
    assert ece == pytest.approx(0.5 * 0.45 + 0.25 * 0.15 + 0.25 * 0.95)
    perfect = [res(conf=1.0) for _ in range(5)]
    assert calibration(perfect)[1] == pytest.approx(0.0)
    assert calibration([]) == ([], None)


def test_summarize_pulls_it_together() -> None:
    run = hand_checked_run() + [res(degraded=True, pred="UNDETERMINED", fix=None, conf=0.0)]
    s = summarize(run)
    assert s.n == 11 and s.n_degraded == 1
    assert s.accuracy == pytest.approx(8 / 11)
    assert s.accuracy_excluding_degraded == pytest.approx(8 / 10)
    assert s.cost_usd.total == pytest.approx(0.55) and s.tokens.mean == 8600
    assert s.fix_accuracy_clear is not None
