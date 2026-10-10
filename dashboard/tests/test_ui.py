"""Drive the real Streamlit script with Streamlit's AppTest and a fake API."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from ops_dashboard.client import ApiError
from tests.fakes import API, APPROVAL, AWAITING, ESCALATED, errors

APP = Path(__file__).resolve().parents[1] / "app.py"


def script() -> None:
    from ops_dashboard.ui import UiConfig, render
    from tests.fakes import API

    render(API, UiConfig(api_url="http://api", trace_url_template="http://jaeger/trace/{trace_id}"))


@pytest.fixture(autouse=True)
def fresh_api() -> None:
    API.reset()


def app() -> AppTest:
    at = AppTest.from_function(script, default_timeout=15)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def texts(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    parts += (
        [s.value for s in at.subheader]
        + [e.value for e in at.error]
        + [w.value for w in at.warning]
    )
    parts += [s.value for s in at.success]
    return "\n".join(str(p) for p in parts)


def open_case(at: AppTest, case_id: str, prefix: str = "queue") -> AppTest:
    at.selectbox(key=f"{prefix}-picker").select(case_id).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


# ------------------------------------------------------------------ queue
def test_queue_shows_cases_needing_attention() -> None:
    at = app()
    assert at.title[0].value == "SettleAgent — ops console"
    assert "2 case(s) need attention" in texts(at)
    queue_call = next(k for n, _, k in API.calls if n == "list_cases")
    assert queue_call["queue"] is True and queue_call["sort"] == "created_at"
    assert queue_call["order"] == "asc"  # oldest first by default


@pytest.mark.parametrize(
    ("label", "sort", "order"),
    [
        ("Lowest confidence first", "confidence", "asc"),
        ("Newest first", "created_at", "desc"),
        ("Highest confidence first", "confidence", "desc"),
    ],
)
def test_sort_choice_is_sent_to_the_api(label: str, sort: str, order: str) -> None:
    at = app()
    API.calls.clear()
    at.selectbox(key="queue-sort").select(label).run()
    call = next(k for n, _, k in API.calls if n == "list_cases")
    assert (call["sort"], call["order"]) == (sort, order)


def test_status_filter_is_sent_to_the_api() -> None:
    at = app()
    API.calls.clear()
    at.multiselect(key="queue-status").unselect("AWAITING_APPROVAL").run()
    call = next(k for n, _, k in API.calls if n == "list_cases")
    assert call["statuses"] == ["ESCALATED"]
    assert "1 case(s) need attention" in texts(at)


def test_empty_queue_is_celebrated() -> None:
    API.queue_empty = True
    assert "The queue is empty" in texts(app())


# ------------------------------------------------------------------ detail
def test_opening_a_case_shows_event_conclusion_evidence_and_fix() -> None:
    at = open_case(app(), AWAITING)
    body = texts(at)
    assert "T-11 · SSIM · AWAITING_APPROVAL" in body
    assert (
        "Trade uses a superseded SSI" in body and "ssi-store" in body and "SSI superseded" in body
    )
    assert "AMEND_SSI" in body and "needs approval" in body
    assert "[trace trace123](http://jaeger/trace/trace123)" in body  # trace link from the template
    labels = [b.proto.text for b in at.get("progress")]  # the metrics tab has a budget bar too
    assert "confidence 95%" in labels


def test_case_without_a_fix_or_approval_has_no_decision_controls() -> None:
    at = open_case(app(), ESCALATED)
    assert "None proposed." in texts(at)
    assert not any(b.key and b.key.endswith("-approve") for b in at.button)


# ---------------------------------------------------------------- decisions
def test_decision_buttons_are_disabled_until_an_operator_is_named() -> None:
    at = open_case(app(), AWAITING)
    assert at.button(key="queue-approve").disabled and at.button(key="queue-reject").disabled
    assert "Enter your operator name" in texts(at)
    assert not [c for c in API.calls if c[0] in {"approve", "reject"}]


def test_approve_calls_the_api_as_the_named_operator_and_confirms() -> None:
    at = app()
    at.sidebar.text_input(key="operator").set_value("alice").run()
    open_case(at, AWAITING)
    at.button(key="queue-approve").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert ("approve", (APPROVAL, "alice"), {"reason": None}) in API.calls
    assert "Approved by alice; case is RESOLVED." in texts(at)


def test_reject_sends_the_reason() -> None:
    at = app()
    at.sidebar.text_input(key="operator").set_value("bob").run()
    open_case(at, AWAITING)
    at.text_input(key="queue-reason").set_value("SSI looks right").run()
    at.button(key="queue-reject").click().run()
    assert ("reject", (APPROVAL, "bob"), {"reason": "SSI looks right"}) in API.calls
    assert "Rejected by bob" in texts(at)


def test_reverse_needs_confirmation_and_an_operator() -> None:
    """A resolved case is not in the queue; the All-cases tab is how an operator reaches it."""
    API.approval = {**API.approval, "status": "APPROVED", "decided_by": "alice", "decided_at": "t"}
    API.awaiting = {**API.awaiting, "status": "RESOLVED"}
    at = app()
    at.sidebar.text_input(key="operator").set_value("carol").run()
    open_case(at, AWAITING, prefix="cases")
    assert at.button(key="cases-reverse").disabled  # not confirmed yet
    at.checkbox(key="cases-confirm").check().run()
    at.button(key="cases-reverse").click().run()
    assert ("reverse", (APPROVAL, "carol"), {"reason": None}) in API.calls
    assert "Reversed by carol" in texts(at)


def test_reverse_is_disabled_without_an_operator() -> None:
    API.approval = {**API.approval, "status": "APPROVED", "decided_by": "alice", "decided_at": "t"}
    API.awaiting = {**API.awaiting, "status": "RESOLVED"}
    at = open_case(app(), AWAITING, prefix="cases")
    at.checkbox(key="cases-confirm").check().run()
    assert at.button(key="cases-reverse").disabled


def test_all_cases_tab_filters_by_any_status() -> None:
    at = app()
    API.calls.clear()
    at.multiselect(key="cases-status").select("RESOLVED").run()
    calls = [k for n, _, k in API.calls if n == "list_cases"]
    all_cases = next(k for k in calls if k["queue"] is False)  # the queue tab also renders
    assert all_cases["statuses"] == ["RESOLVED"]
    assert next(k for k in calls if k["queue"] is True)["statuses"] != ["RESOLVED"]


# ------------------------------------------------------------------ errors
def test_api_unreachable_is_a_friendly_error_not_a_crash() -> None:
    API.fail = errors["unavailable"]
    at = AppTest.from_function(script, default_timeout=15).run()
    assert not at.exception
    assert any("API unreachable" in e.value for e in at.error)


@pytest.mark.parametrize(
    ("status", "detail", "where", "needle"),
    [
        (401, "bad key", "error", "rejected our key"),
        (409, "already approved", "error", "409: already approved"),
        (503, "settlement down", "warning", "temporarily unavailable"),
    ],
)
def test_api_errors_are_explained(status: int, detail: str, where: str, needle: str) -> None:
    API.fail = ApiError(status, detail)
    at = AppTest.from_function(script, default_timeout=15).run()
    assert not at.exception
    assert any(needle in e.value for e in getattr(at, where))


def test_a_conflict_on_approve_is_shown_and_nothing_is_flashed_as_success() -> None:
    at = app()
    at.sidebar.text_input(key="operator").set_value("alice").run()
    open_case(at, AWAITING)
    API.fail = None
    original = API.approve
    API.approve = lambda *a, **k: (_ for _ in ()).throw(ApiError(409, "already rejected"))  # type: ignore[method-assign]
    at.button(key="queue-approve").click().run()
    API.approve = original  # type: ignore[method-assign]
    assert any("409: already rejected" in e.value for e in at.error)
    assert not any("Approved by" in s.value for s in at.success)


# --------------------------------------------------------------- other tabs
def test_approvals_tab_lists_pending_and_opens_the_case() -> None:
    at = app()
    assert "1 approval(s) waiting for a decision" in texts(at)
    open_case(at, AWAITING, prefix="approvals")
    assert at.button(key="approvals-approve") is not None


def test_approvals_tab_when_nothing_is_pending() -> None:
    API.approval = {**API.approval, "status": "APPROVED"}
    assert "Nothing is waiting for approval." in texts(app())


def test_case_panel_shows_cost_latency_and_steps() -> None:
    body = texts(open_case(app(), AWAITING))
    assert "12,000 in / 800 out tokens" in body and "$0.0640" in body
    assert "4.2 s" in body and "7 steps" in body


def test_case_panel_falls_back_to_total_tokens_for_old_cases() -> None:
    API.usage = None
    assert "321 tokens" in texts(open_case(app(), AWAITING))


def test_metrics_tab_shows_run_health_and_cost() -> None:
    values = {m.label: m.value for m in app().metric}
    assert values["Run latency p50"] == "850 ms" and values["Run latency p95"] == "4.2 s"
    assert values["Tool error rate"] == "5.0%"
    assert values["Spend to date"] == "$0.4480" and values["Average cost / case"] == "$0.0640"
    assert values["Budget"] == "$50.00"


@pytest.mark.parametrize(
    ("level", "where", "needle"),
    [("warning", "warning", "Budget warning"), ("exceeded", "error", "Budget exceeded")],
)
def test_budget_banner_escalates_with_the_level(level: str, where: str, needle: str) -> None:
    API.budget_level = level
    at = app()
    assert any(needle in e.value for e in getattr(at, where))


def test_no_budget_banner_when_spend_is_healthy() -> None:
    at = app()
    assert not any("Budget" in e.value for e in list(at.error) + list(at.warning))


def test_metrics_tab_shows_the_kpis() -> None:
    at = app()
    values = {m.label: m.value for m in at.metric}
    assert values["Total cases"] == "7" and values["Queue depth"] == "3"
    assert values["Auto-resolve rate"] == "57.1%" and values["Average confidence"] == "80.0%"
    assert values["LLM tokens"] == "12,345"


# ----------------------------------------------------------------- app.py
def test_entrypoint_requires_an_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SETTLE_API_KEY", raising=False)
    at = AppTest.from_file(str(APP), default_timeout=15).run()
    assert any("SETTLE_API_KEY is not set" in e.value for e in at.error)


def test_entrypoint_with_a_key_degrades_gracefully_when_the_api_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SETTLE_API_KEY", "k" * 20)
    monkeypatch.setenv("SETTLE_API_URL", "http://127.0.0.1:1")  # nothing listens here
    at = AppTest.from_file(str(APP), default_timeout=20).run()
    assert not at.exception
    assert any("API unreachable" in e.value for e in at.error)
