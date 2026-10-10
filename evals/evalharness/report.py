"""The scorecard: Markdown and self-contained HTML, built from one structured ``Scorecard``."""

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from evalharness.baseline import Baseline, GuardReport
from evalharness.metrics import (
    CalibrationBin,
    CurvePoint,
    Summary,
    calibration,
    category_correct,
    confusion,
    curve_point,
    pr_curve,
    summarize,
)
from evalharness.models import CaseResult, RunMeta
from evalharness.threshold import DEFAULT_MIN_PRECISION, DEFAULT_MIN_SUPPORT, select_threshold

KEY_THRESHOLDS = (0.5, 0.7, 0.8, 0.9, 0.95, 0.99)
SIMULATED_BANNER = (
    "SIMULATED RUN — no real model was called. These numbers show the harness works; "
    "they say nothing about Claude's accuracy and must not be cited or used as a baseline."
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Scorecard(_Model):
    meta: RunMeta
    summary: Summary
    curve: list[CurvePoint]
    key_points: list[CurvePoint]
    chosen: CurvePoint | None
    min_precision: float
    min_support: int
    confusion: dict[str, dict[str, int]]
    calibration: list[CalibrationBin]
    unsafe_examples: list[str]
    wrong_examples: list[str]
    guard: GuardReport | None
    baseline: Baseline | None


def build_scorecard(
    meta: RunMeta,
    results: Sequence[CaseResult],
    *,
    min_precision: float = DEFAULT_MIN_PRECISION,
    min_support: int = DEFAULT_MIN_SUPPORT,
    baseline: Baseline | None = None,
    guard: GuardReport | None = None,
) -> Scorecard:
    curve = pr_curve(results)
    chosen = select_threshold(curve, min_precision=min_precision, min_support=min_support)
    ref = chosen.threshold if chosen else 0.9
    unsafe = sorted(
        (r for r in results if not r.auto_resolvable and curve_point([r], ref).auto_count),
        key=lambda r: -r.confidence,
    )
    wrong = sorted(
        (r for r in results if r.auto_resolvable and not category_correct(r)),
        key=lambda r: -r.confidence,
    )
    bins, _ = calibration(results)
    return Scorecard(
        meta=meta,
        summary=summarize(results),
        curve=curve,
        key_points=[curve_point(results, t) for t in KEY_THRESHOLDS],
        chosen=chosen,
        min_precision=min_precision,
        min_support=min_support,
        confusion=confusion(results),
        calibration=bins,
        unsafe_examples=[
            f"{r.case_id} ({r.reason_code}, conf {r.confidence:.2f}) → {r.predicted_category}"
            for r in unsafe[:10]
        ],
        wrong_examples=[
            f"{r.case_id} ({r.reason_code}, conf {r.confidence:.2f}) → "
            f"{r.predicted_category}, expected {r.expected_category}"
            for r in wrong[:10]
        ],
        guard=guard,
        baseline=baseline,
    )


# ------------------------------------------------------------------ formatting
def _pct(x: float | None, digits: int = 1) -> str:
    return "—" if x is None else f"{x * 100:.{digits}f}%"


def _num(x: float | None, digits: int = 2) -> str:
    return "—" if x is None else f"{x:,.{digits}f}"


def _usd(x: float | None) -> str:
    return "—" if x is None else f"${x:,.4f}" if abs(x) < 1 else f"${x:,.2f}"


def _row(cells: Sequence[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _table(head: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    return [_row(head), _row(["---"] * len(head)), *(_row(r) for r in rows)]


def _curve_rows(points: Sequence[CurvePoint], chosen: CurvePoint | None) -> list[list[str]]:
    rows = []
    for p in points:
        mark = " ◀ chosen" if chosen and p.threshold == chosen.threshold else ""
        rows.append(
            [
                f"{p.threshold:.2f}{mark}",
                str(p.auto_count),
                _pct(p.precision),
                _pct(p.wilson_lower),
                _pct(p.recall),
                _pct(p.auto_rate),
                str(p.unsafe_auto),
                str(p.wrong_auto),
            ]
        )
    return rows


CURVE_HEAD = [
    "Threshold",
    "Auto-resolved",
    "Precision",
    "Precision (95% lower)",
    "Recall",
    "Auto rate",
    "Unsafe auto",
    "Wrong auto",
]


def to_markdown(card: Scorecard) -> str:
    m, s = card.meta, card.summary
    out: list[str] = [f"# SettleAgent evaluation scorecard — run `{m.run_id}`", ""]
    if m.simulated:
        out += [f"> **{SIMULATED_BANNER}**", ""]
    out += [
        f"- **Model:** `{m.model}` (effort `{m.effort}`) · **prompt:** `{m.prompt_version}` · "
        f"**dataset:** v{m.dataset_version} ({m.dataset_cases} cases)",
        f"- **Cases run:** {m.n_completed} of {m.n_cases}"
        + (f" · **ABORTED:** {m.aborted}" if m.aborted else ""),
        f"- **Started:** {m.started_at:%Y-%m-%d %H:%M} UTC · **Spend:** {_usd(m.total_cost_usd)}",
        "",
        "## Headline",
        "",
    ]
    headline = [
        ["Root-cause accuracy", _pct(s.accuracy), f"{s.n} cases"],
        ["Fix accuracy (auto-resolvable cases)", _pct(s.fix_accuracy_clear), ""],
        [
            "Calibration error (ECE, lower is better)",
            _num(s.ece, 3),
            "0 = confidence is a true probability",
        ],
        [
            "Infrastructure-degraded runs",
            str(s.n_degraded),
            "LLM/tool faults, scored separately from model errors",
        ],
    ]
    if card.chosen:
        c = card.chosen
        headline += [
            [
                "**Chosen auto-resolve threshold**",
                f"**{c.threshold:.2f}**",
                f"highest recall with precision ≥ {card.min_precision:.0%} "
                f"and ≥ {card.min_support} cases",
            ],
            [
                "Precision at threshold",
                _pct(c.precision),
                f"95% lower bound {_pct(c.wilson_lower)}",
            ],
            [
                "Recall at threshold",
                _pct(c.recall),
                f"{c.true_pos} of {c.true_pos + c.false_neg} auto-resolvable cases",
            ],
            ["Escalation rate at threshold", _pct(c.escalation_rate), "cases sent to a human"],
            [
                "Unsafe automation at threshold",
                str(c.unsafe_auto),
                "cases that needed a human but were auto-resolved",
            ],
        ]
    else:
        headline += [
            [
                "**Chosen auto-resolve threshold**",
                "**none**",
                f"no threshold reaches precision ≥ {card.min_precision:.0%} "
                f"with ≥ {card.min_support} "
                "cases: do NOT auto-resolve yet",
            ]
        ]
    out += _table(["Metric", "Value", "Note"], headline)

    out += ["", "## Accuracy by difficulty", ""]
    out += _table(
        ["Difficulty", "Cases", "Accuracy"],
        [[k, str(n), _pct(a)] for k, (n, a) in s.accuracy_by_difficulty.items()],
    )
    out += ["", "## Accuracy by reason code", ""]
    out += _table(
        ["Reason code", "Cases", "Accuracy"],
        [[k, str(n), _pct(a)] for k, (n, a) in s.accuracy_by_reason_code.items()],
    )

    out += [
        "",
        "## Auto-resolution precision / recall vs. threshold",
        "",
        "*Precision = of the cases auto-resolved, the share that were safely auto-resolvable "
        "(right cause **and** right fix). Unsafe = a case that needed a human but was automated.*",
        "",
    ]
    pts = list(card.key_points)
    if card.chosen and all(card.chosen.threshold != p.threshold for p in pts):
        pts = sorted([*pts, card.chosen], key=lambda p: p.threshold)
    out += _table(CURVE_HEAD, _curve_rows(pts, card.chosen))

    out += ["", "## Confusion matrix (rows: expected, columns: predicted)", ""]
    cols = sorted({c for row in card.confusion.values() for c in row} | set(card.confusion))
    out += _table(
        ["expected \\ predicted", *cols],
        [[e, *[str(card.confusion[e].get(c, 0)) for c in cols]] for e in sorted(card.confusion)],
    )

    out += ["", "## Calibration", ""]
    out += _table(
        ["Confidence bin", "Cases", "Mean confidence", "Accuracy"],
        [
            [f"{b.low:.1f}–{b.high:.1f}", str(b.n), _pct(b.mean_confidence), _pct(b.accuracy)]
            for b in card.calibration
        ],
    )

    out += ["", "## Cost and latency", ""]
    out += _table(
        ["", "Mean", "p50", "p90", "p95", "p99", "Total"],
        [
            ["Cost / case"]
            + [_usd(getattr(s.cost_usd, k)) for k in ("mean", "p50", "p90", "p95", "p99", "total")],
            ["Latency (s)"]
            + [_num(getattr(s.latency_s, k), 1) for k in ("mean", "p50", "p90", "p95", "p99")]
            + ["—"],
            ["Tokens / case"]
            + [
                _num(getattr(s.tokens, k), 0) for k in ("mean", "p50", "p90", "p95", "p99", "total")
            ],
            ["Steps / case"]
            + [_num(getattr(s.steps, k), 1) for k in ("mean", "p50", "p90", "p95", "p99")]
            + ["—"],
        ],
    )

    out += ["", "## Where it goes wrong", ""]
    out += ["**Unsafe automation** (needed a human, would be auto-resolved):", ""]
    out += [f"- {x}" for x in card.unsafe_examples] or ["- none"]
    out += [
        "",
        "**Auto-resolvable cases with the wrong root cause** (highest confidence first):",
        "",
    ]
    out += [f"- {x}" for x in card.wrong_examples] or ["- none"]

    if card.guard is not None:
        out += ["", "## Regression guard", ""]
        out += (
            [
                f"Baseline run `{card.baseline.run_id}` ({card.baseline.model}, "
                f"prompt {card.baseline.prompt_version}, "
                f"threshold {card.baseline.threshold:.2f}).",
                "",
            ]
            if card.baseline
            else []
        )
        verdict = "**PASSED**" if card.guard.passed else "**FAILED**"
        out += [verdict, ""]
        out += [f"- `{f.level.upper()}` {f.metric}: {f.message}" for f in card.guard.findings] or [
            "- no regressions"
        ]
    return "\n".join(out) + "\n"
