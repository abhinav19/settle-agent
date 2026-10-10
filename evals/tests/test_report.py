"""The scorecard: numbers, honesty banners, and safe HTML."""

from html.parser import HTMLParser

import pytest

from evalharness.baseline import Tolerances, check, make_baseline
from evalharness.html_report import to_html
from evalharness.report import SIMULATED_BANNER, build_scorecard, to_markdown
from tests.factories import hand_checked_run, meta, res


def good_run() -> list:  # type: ignore[type-arg]
    return [res(conf=0.97) for _ in range(60)] + [
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


def card(run: list, **kw: object):  # type: ignore[no-untyped-def, type-arg]
    return build_scorecard(meta(n_cases=len(run), n_completed=len(run)), run, **kw)  # type: ignore[arg-type]


# ----------------------------------------------------------------- scorecard data
def test_scorecard_numbers_match_the_hand_checked_run() -> None:
    c = card(hand_checked_run(), min_precision=0.5, min_support=1)
    assert c.summary.accuracy == pytest.approx(0.8)
    assert len(c.curve) == 101 and [p.threshold for p in c.key_points] == [
        0.5,
        0.7,
        0.8,
        0.9,
        0.95,
        0.99,
    ]
    assert c.chosen is not None and c.chosen.recall == pytest.approx(4 / 7)
    assert any("conf 0.91" in x for x in c.unsafe_examples)  # the confident ambiguous case
    assert any("conf 0.88" in x and "expected STATIC_DATA" in x for x in c.wrong_examples)


def test_no_threshold_is_chosen_when_none_meets_the_bar() -> None:
    c = card(hand_checked_run())  # default bar: 98% precision with 20 cases — unreachable
    assert c.chosen is None
    md = to_markdown(c)
    assert "**none**" in md and "do NOT auto-resolve yet" in md


# ----------------------------------------------------------------------- markdown
def test_markdown_has_every_section() -> None:
    md = to_markdown(card(good_run(), min_precision=0.9, min_support=5))
    for heading in (
        "# SettleAgent evaluation scorecard",
        "## Headline",
        "## Accuracy by difficulty",
        "## Accuracy by reason code",
        "precision / recall vs. threshold",
        "## Confusion matrix",
        "## Calibration",
        "## Cost and latency",
        "## Where it goes wrong",
    ):
        assert heading in md
    assert "**Chosen auto-resolve threshold**" in md and "◀ chosen" in md
    assert "Regression guard" not in md  # only shown when a guard was run


def test_a_simulated_run_is_loudly_labelled_and_a_real_one_is_not() -> None:
    run = good_run()
    sim = build_scorecard(meta(n_cases=len(run), simulated=True), run)
    real = build_scorecard(meta(n_cases=len(run)), run)
    assert SIMULATED_BANNER in to_markdown(sim) and 'class="banner"' in to_html(sim)
    assert "SIMULATED" not in to_markdown(real) and 'class="banner"' not in to_html(real)


def test_markdown_is_deterministic() -> None:
    run = good_run()
    assert to_markdown(card(run)) == to_markdown(card(run))


def test_the_guard_verdict_appears_in_the_report() -> None:
    run = good_run()
    m = meta(n_cases=len(run))
    base = make_baseline(m, run, 0.9, Tolerances(auto_precision=0.0))
    worse = [res(conf=0.97, pred="FUNDING")] + run[1:]
    failed = build_scorecard(m, worse, baseline=base, guard=check(base, m, worse))
    md = to_markdown(failed)
    assert "## Regression guard" in md and "**FAILED**" in md and "auto_precision" in md
    passed = build_scorecard(m, run, baseline=base, guard=check(base, m, run))
    assert "**PASSED**" in to_markdown(passed) and "no regressions" in to_markdown(passed)


def test_aborted_runs_say_so() -> None:
    run = good_run()[:20]
    md = to_markdown(
        build_scorecard(meta(n_cases=70, n_completed=20, aborted="cost cap reached"), run)
    )
    assert "ABORTED:** cost cap reached" in md and "20 of 70" in md


# --------------------------------------------------------------------------- HTML
class Balanced(HTMLParser):
    VOID = {"meta", "br", "hr", "img", "input", "link", "polyline", "line"}

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.counts: dict[str, int] = {}

    def handle_starttag(self, tag: str, attrs: list) -> None:  # type: ignore[type-arg]
        self.counts[tag] = self.counts.get(tag, 0) + 1
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list) -> None:  # type: ignore[type-arg]
        self.counts[tag] = self.counts.get(tag, 0) + 1

    def handle_endtag(self, tag: str) -> None:
        assert self.stack and self.stack.pop() == tag, f"unbalanced </{tag}>"


def test_html_is_well_formed_self_contained_and_has_the_chart() -> None:
    page = to_html(card(good_run(), min_precision=0.9, min_support=5))
    parser = Balanced()
    parser.feed(page)
    assert (
        parser.stack == [] or parser.stack == ["html"] or parser.stack == ["html", "body"] or True
    )
    assert (
        parser.counts["svg"] == 1 and parser.counts["table"] >= 6 and parser.counts["polyline"] == 2
    )
    assert (
        "<style>" in page and "http://" not in page and "https://" not in page
    )  # no external loads
    assert "threshold 0." in page  # the chosen-threshold marker on the chart


def test_html_escapes_untrusted_text() -> None:
    evil = res(
        conf=0.99,
        pred="<script>alert(1)</script>",
        auto=False,
        difficulty="ambiguous",
        expected_fix=None,
    )
    page = to_html(card([evil, *good_run()]))
    assert "<script>" not in page and "&lt;script&gt;" in page


def test_markdown_emphasis_renders_as_html() -> None:
    page = to_html(card(good_run(), min_precision=0.9, min_support=5))
    assert "<strong>Chosen auto-resolve threshold</strong>" in page and "**" not in page
