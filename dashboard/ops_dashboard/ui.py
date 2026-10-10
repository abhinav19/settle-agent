"""Streamlit ops console. Pure presentation over an injected API client (never the database)."""

from dataclasses import dataclass
from typing import Any, Protocol

import streamlit as st

from ops_dashboard.client import ApiError, ApiUnavailable
from ops_dashboard.format import case_label, millis, pct, queue_rows, trace_url, usd

SORTS: dict[str, tuple[str, str]] = {
    "Oldest first": ("created_at", "asc"),
    "Newest first": ("created_at", "desc"),
    "Lowest confidence first": ("confidence", "asc"),
    "Highest confidence first": ("confidence", "desc"),
}
QUEUE_STATUS_CHOICES = ["AWAITING_APPROVAL", "ESCALATED"]


class DashboardApi(Protocol):
    def list_cases(
        self,
        *,
        queue: bool = ...,
        statuses: list[str] | None = ...,
        sort: str = ...,
        order: str = ...,
        limit: int = ...,
        offset: int = ...,
    ) -> dict[str, Any]: ...
    def case_detail(self, case_id: str) -> dict[str, Any]: ...
    def list_approvals(self, status: str = ..., limit: int = ...) -> dict[str, Any]: ...
    def metrics(self) -> dict[str, Any]: ...
    def approve(self, approval_id: str, decided_by: str) -> dict[str, Any]: ...
    def reject(self, approval_id: str, decided_by: str, reason: str | None) -> dict[str, Any]: ...
    def reverse(self, approval_id: str, decided_by: str, reason: str | None) -> dict[str, Any]: ...


@dataclass(frozen=True)
class UiConfig:
    api_url: str = ""
    trace_url_template: str | None = None


def _call[T](fn: Any, *args: Any, **kwargs: Any) -> T | None:
    """Run an API call; show a friendly error instead of a stack trace."""
    try:
        return fn(*args, **kwargs)  # type: ignore[no-any-return]
    except ApiUnavailable as exc:
        st.error(f"API unreachable. {exc}")
    except ApiError as exc:
        if exc.status == 401:
            st.error("The API rejected our key (401). Check SETTLE_API_KEY.")
        elif exc.retryable:
            st.warning(
                f"The API is temporarily unavailable ({exc.status}); try again. {exc.detail}"
            )
        else:
            st.error(f"{exc.status}: {exc.detail}")
    return None


def _flash(kind: str, message: str) -> None:
    st.session_state.setdefault("flash", []).append((kind, message))


def _show_flash() -> None:
    for kind, message in st.session_state.pop("flash", []):
        {"success": st.success, "warning": st.warning}.get(kind, st.error)(message)


# ------------------------------------------------------------------ case panel
def case_panel(
    client: DashboardApi, case_id: str, operator: str, config: UiConfig, prefix: str
) -> None:
    detail: dict[str, Any] | None = _call(client.case_detail, case_id)
    if detail is None:
        return
    summary, case = detail["summary"], detail["case"]
    event, result = case["event"], case["result"]

    st.subheader(f"{summary['trade_id']} · {summary['reason_code']} · {summary['status']}")
    left, right = st.columns(2)
    with left:
        st.markdown("**Settlement failure**")
        st.write(
            {
                "trade": event["trade_id"],
                "isin": event["isin"],
                "counterparty": event["counterparty_bic"],
                "quantity": event["quantity"],
                "cash": f"{event['cash_amount']} {event['currency']}",
                "settlement date": event["settlement_date"],
                "reported reason": event["reason_code"],
            }
        )
        with st.expander("Raw message"):
            st.code(event["raw_message"])
    with right:
        st.markdown("**Agent conclusion**")
        if result is None:
            st.info("Not investigated yet.")
        else:
            st.write(f"**{result['root_cause']}**")
            st.caption(f"category {result['reason_category']} · model {result['model']}")
            st.progress(float(result["confidence"]), text=f"confidence {pct(result['confidence'])}")
            link = trace_url(config.trace_url_template, result["reasoning_trace_id"])
            st.caption(
                f"[trace {result['reasoning_trace_id']}]({link})"
                if link
                else f"trace id {result['reasoning_trace_id']}"
            )
            usage = detail.get("usage")
            if usage and usage.get("cost_usd") is not None:
                st.caption(
                    f"{usage['input_tokens']:,} in / {usage['output_tokens']:,} out tokens · "
                    f"{usd(usage['cost_usd'])} · {millis(usage['duration_ms'])} · "
                    f"{usage['steps_used']} steps"
                )
            else:
                st.caption(f"{result['tokens_used']} tokens")

    if result is not None:
        st.markdown("**Evidence**")
        if not result["evidence"]:
            st.caption("No evidence cited.")
        for item in result["evidence"]:
            st.markdown(f"- `{item['source']}` — {item['statement']}")
            with st.expander("raw tool output"):
                st.json(item["raw"])
        fix = result["proposed_fix"]
        st.markdown("**Proposed fix**")
        if fix is None:
            st.caption("None proposed.")
        else:
            write = "changes state (needs approval)" if fix["requires_write"] else "read-only"
            st.write(f"`{fix['action']}` — {write}")
            st.json(fix["parameters"])

    _approval_controls(client, detail, operator, prefix)

    st.markdown("**Audit trail**")
    st.dataframe(
        [
            {
                "When": a["ts"],
                "Actor": a["actor_id"] or a["actor"],
                "Action": a["action"],
                "Detail": str(a["payload"])[:160],
            }
            for a in detail["audit"]
        ],
        hide_index=True,
        width="stretch",
    )
    if detail["tool_calls"]:
        with st.expander(f"Tool calls ({len(detail['tool_calls'])})"):
            st.dataframe(
                [
                    {
                        "Tool": t["tool"],
                        "Request": str(t["request"]),
                        "ms": t["latency_ms"],
                        "Error": t["error"] or "",
                    }
                    for t in detail["tool_calls"]
                ],
                hide_index=True,
                width="stretch",
            )


def _approval_controls(
    client: DashboardApi, detail: dict[str, Any], operator: str, prefix: str
) -> None:
    approval = detail["approval"]
    if approval is None:
        return
    status, approval_id = approval["status"], approval["id"]
    st.markdown("**Approval**")
    if status == "PENDING":
        st.write("Waiting for a human decision. Approving **executes** the fix.")
        if not operator:
            st.warning("Enter your operator name in the sidebar to decide.")
        reason = st.text_input(
            "Reason (optional, recorded in the audit trail)", key=f"{prefix}-reason"
        )
        approve_col, reject_col = st.columns(2)
        if approve_col.button(
            "✅ Approve & execute", key=f"{prefix}-approve", disabled=not operator
        ):
            if (out := _call(client.approve, approval_id, operator)) is not None:
                _flash("success", f"Approved by {operator}; case is {out['case']['status']}.")
                st.rerun()
        if reject_col.button("⛔ Reject", key=f"{prefix}-reject", disabled=not operator):
            if (out := _call(client.reject, approval_id, operator, reason or None)) is not None:
                _flash("success", f"Rejected by {operator}; case is {out['case']['status']}.")
                st.rerun()
        return
    by = approval.get("decided_by") or "?"
    st.write(f"**{status}** by {by} at {approval.get('decided_at')}")
    if status == "APPROVED" and detail["summary"]["status"] == "RESOLVED":
        confirm = st.checkbox("I understand this undoes the executed fix", key=f"{prefix}-confirm")
        if st.button("↩️ Reverse fix", key=f"{prefix}-reverse", disabled=not (operator and confirm)):
            if (out := _call(client.reverse, approval_id, operator, None)) is not None:
                _flash("success", f"Reversed by {operator}; case is {out['case']['status']}.")
                st.rerun()


# ------------------------------------------------------------------------ tabs
def _case_picker(
    client: DashboardApi, items: list[dict[str, Any]], operator: str, config: UiConfig, prefix: str
) -> None:
    if not items:
        return
    labels = {c["id"]: case_label(c) for c in items}
    chosen = st.selectbox(
        "Open case",
        options=[""] + list(labels),
        format_func=lambda cid: "— select a case —" if not cid else labels[cid],
        key=f"{prefix}-picker",
    )
    if chosen:
        st.divider()
        case_panel(client, chosen, operator, config, prefix)


ALL_STATUSES = [
    "RECEIVED",
    "INVESTIGATING",
    "AWAITING_APPROVAL",
    "AUTO_RESOLVED",
    "RESOLVED",
    "ESCALATED",
    "FAILED",
]


def _case_list(
    client: DashboardApi,
    operator: str,
    config: UiConfig,
    *,
    prefix: str,
    queue: bool,
    choices: list[str],
    default: list[str],
) -> None:
    c1, c2 = st.columns([2, 3])
    sort_label = c1.selectbox("Sort", list(SORTS), key=f"{prefix}-sort")
    statuses = c2.multiselect("Status", choices, default=default, key=f"{prefix}-status")
    sort, order = SORTS[sort_label]
    page: dict[str, Any] | None = _call(
        client.list_cases, queue=queue, statuses=statuses or None, sort=sort, order=order, limit=100
    )
    if page is None:
        return
    noun = "need attention" if queue else "match"
    st.caption(f"{page['total']} case(s) {noun} (showing {len(page['items'])})")
    if not page["items"]:
        st.success("The queue is empty. 🎉" if queue else "No cases match.")
        return
    st.dataframe(queue_rows(page["items"]), hide_index=True, width="stretch")
    _case_picker(client, page["items"], operator, config, prefix)


def queue_tab(client: DashboardApi, operator: str, config: UiConfig) -> None:
    """Cases a human must look at, oldest first by default."""
    _case_list(
        client,
        operator,
        config,
        prefix="queue",
        queue=True,
        choices=QUEUE_STATUS_CHOICES,
        default=QUEUE_STATUS_CHOICES,
    )


def cases_tab(client: DashboardApi, operator: str, config: UiConfig) -> None:
    """Every case, any status — e.g. to review or reverse a resolved one."""
    _case_list(
        client, operator, config, prefix="cases", queue=False, choices=ALL_STATUSES, default=[]
    )


def approvals_tab(client: DashboardApi, operator: str, config: UiConfig) -> None:
    page: dict[str, Any] | None = _call(client.list_approvals, status="PENDING", limit=100)
    if page is None:
        return
    st.caption(f"{page['total']} approval(s) waiting for a decision")
    if not page["items"]:
        st.success("Nothing is waiting for approval.")
        return
    st.dataframe(
        [
            {
                "Trade": i["case"]["trade_id"],
                "Fix": i["approval"]["proposed_fix"]["action"],
                "Confidence": pct(i["case"]["confidence"]),
                "Requested": i["approval"]["created_at"],
                "Case": i["case"]["id"],
            }
            for i in page["items"]
        ],
        hide_index=True,
        width="stretch",
    )
    _case_picker(client, [i["case"] for i in page["items"]], operator, config, "approvals")


def metrics_tab(client: DashboardApi) -> None:
    m: dict[str, Any] | None = _call(client.metrics)
    if m is None:
        return
    cols = st.columns(3)
    cols[0].metric("Total cases", m["total_cases"])
    cols[1].metric("Queue depth", m["queue_depth"], help="Escalated + awaiting approval")
    cols[2].metric("Pending approvals", m["pending_approvals"])
    cols = st.columns(3)
    cols[0].metric("Auto-resolve rate", pct(m["auto_resolve_rate"], 1))
    cols[1].metric("Average confidence", pct(m["average_confidence"], 1))
    cols[2].metric("LLM tokens", f"{m['total_tokens']:,}")

    st.markdown("**Run health**")
    cols = st.columns(3)
    cols[0].metric("Run latency p50", millis(m["p50_run_latency_ms"]))
    cols[1].metric("Run latency p95", millis(m["p95_run_latency_ms"]))
    cols[2].metric(
        "Tool error rate",
        pct(m["tool_error_rate"], 1),
        help=f"{m['tool_infra_failures']} timeouts/outages in {m['tool_calls']} calls "
        "('not found' is a finding, not an error)",
    )

    st.markdown("**Cost & budget**")
    b = m["budget"]
    cols = st.columns(3)
    cols[0].metric("Spend to date", usd(m["total_cost_usd"]))
    cols[1].metric("Average cost / case", usd(m["average_cost_usd"]))
    cols[2].metric("Budget", usd(b["ceiling_usd"]))
    st.progress(
        min(1.0, float(b["used_fraction"])), text=f"{pct(b['used_fraction'], 1)} of budget used"
    )
    if b["level"] == "exceeded":
        st.error("Budget exceeded. Investigations are over the configured ceiling.")
    elif b["level"] == "warning":
        st.warning("Budget warning: spend has passed the alert threshold.")
    if m["by_status"]:
        st.bar_chart(m["by_status"])


def render(client: DashboardApi, config: UiConfig) -> None:
    """The whole app. Called by ``app.py``; tests call it with a fake client."""
    st.set_page_config(page_title="SettleAgent Ops", page_icon="🧾", layout="wide")
    st.title("SettleAgent — ops console")
    operator = st.sidebar.text_input(
        "Operator name", key="operator", help="Recorded in the audit trail with every decision"
    ).strip()
    st.sidebar.caption(f"API: {config.api_url or 'n/a'}")
    st.sidebar.button("🔄 Refresh")
    _show_flash()
    queue, approvals, cases, metrics = st.tabs(["Ops queue", "Approvals", "All cases", "Metrics"])
    with queue:
        queue_tab(client, operator, config)
    with cases:
        cases_tab(client, operator, config)
    with approvals:
        approvals_tab(client, operator, config)
    with metrics:
        metrics_tab(client)
