"""Control-plane API against real Postgres + real remediation service (oracle LLM upstream)."""

import itertools
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from settleagent.api.app import ApiDeps, create_app
from settleagent.api.security import API_KEY_HEADER
from settleagent.remediation.models import SettlementSystemError
from settleagent.remediation.service import RemediationService
from settleagent.remediation.system import SyntheticSettlementSystem
from tests.doubles import RoutedLlm, oracle_reply, pick
from tests.integration.helpers import NOW, make_processor, process_scenario

pytestmark = pytest.mark.integration
_TICK = itertools.count(1)
KEY = "integration-key-0123456789"
HEADERS = {API_KEY_HEADER: KEY}


class FlakySystem:
    """Delegates to the real synthetic system; can fail transiently on demand."""

    def __init__(self, inner: SyntheticSettlementSystem) -> None:
        self.inner, self.fail_transient = inner, False

    async def execute(self, fix: Any, key: str) -> Any:
        if self.fail_transient:
            self.fail_transient = False
            raise SettlementSystemError("blip", transient=True)
        return await self.inner.execute(fix, key)

    async def reverse(self, key: str) -> Any:
        return await self.inner.reverse(key)


class Api:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        ceiling_usd: float = 50.0,
        warn_fraction: float = 0.8,
    ) -> None:
        self.sessions = sessions
        self.system = FlakySystem(SyntheticSettlementSystem(sessions, clock=lambda: NOW))
        remediation = RemediationService(sessions, self.system, clock=lambda: NOW)
        app = create_app(
            ApiDeps(
                sessions=sessions,
                remediation=remediation,
                api_key=KEY,
                cost_ceiling_usd=ceiling_usd,
                budget_warn_fraction=warn_fraction,
            )
        )
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=HEADERS
        )

    async def make_case(self, difficulty: str, code: str) -> UUID:
        sc = pick(difficulty, code)
        # strictly increasing creation times, so "newest first" is meaningful in a shared DB
        proc = make_processor(
            self.sessions,
            RoutedLlm(synthesize=oracle_reply(sc)),
            clock=lambda: NOW + timedelta(seconds=next(_TICK)),
        )
        return await process_scenario(proc, sc)

    async def approval_id(self, case_id: UUID) -> UUID:
        detail = (await self.http.get(f"/cases/{case_id}")).json()
        return UUID(detail["approval"]["id"])


@pytest.fixture
async def api(sessions: async_sessionmaker[AsyncSession]) -> Any:
    a = Api(sessions)
    yield a
    await a.http.aclose()


# ------------------------------------------------------------------- cases
async def test_list_cases_and_filters(api: Api) -> None:
    awaiting = await api.make_case("clear", "SSIM")
    escalated = await api.make_case("ambiguous", "SSIM")
    resolved = await api.make_case("clear", "CALD")

    r = await api.http.get("/cases", params={"limit": 200})
    assert r.status_code == 200
    ids = {c["id"] for c in r.json()["items"]}
    assert (
        r.json()["total"] >= 3
        and {str(awaiting), str(escalated), str(resolved)} <= ids
        or r.json()["total"] > 200
    )

    queue = (await api.http.get("/cases", params={"queue": "true", "limit": 200})).json()["items"]
    assert {c["status"] for c in queue} <= {"ESCALATED", "AWAITING_APPROVAL"}
    assert str(resolved) not in {c["id"] for c in queue}

    only = (await api.http.get("/cases", params={"status": "AUTO_RESOLVED", "limit": 200})).json()
    assert {c["status"] for c in only["items"]} == {"AUTO_RESOLVED"}
    both = (
        await api.http.get("/cases", params=[("status", "ESCALATED"), ("status", "AUTO_RESOLVED")])
    ).json()
    assert {c["status"] for c in both["items"]} <= {"ESCALATED", "AUTO_RESOLVED"}

    by_code = (await api.http.get("/cases", params={"reason_code": "CALD", "limit": 200})).json()
    assert by_code["items"] and {c["reason_code"] for c in by_code["items"]} == {"CALD"}


async def test_case_summary_fields(api: Api) -> None:
    case_id = await api.make_case("clear", "SSIM")
    item = next(
        c
        for c in (
            await api.http.get("/cases", params={"limit": 200, "status": "AWAITING_APPROVAL"})
        ).json()["items"]
        if c["id"] == str(case_id)
    )
    assert item["status"] == "AWAITING_APPROVAL" and item["decision"] == "AUTO_RESOLVE"
    assert item["category"] == "STATIC_DATA" and item["fix_action"] == "AMEND_SSI"
    assert item["requires_write"] is True and item["confidence"] == pytest.approx(0.97)
    assert item["trace_id"] and item["reason_code"] == "SSIM"


@pytest.mark.parametrize("sort", ["confidence", "created_at", "updated_at"])
@pytest.mark.parametrize("order", ["asc", "desc"])
async def test_sorting_is_monotonic_with_nulls_last(api: Api, sort: str, order: str) -> None:
    await api.make_case("clear", "SSIM")
    await api.make_case("ambiguous", "LACK")
    items = (
        await api.http.get("/cases", params={"sort": sort, "order": order, "limit": 200})
    ).json()["items"]
    key = {"confidence": "confidence", "created_at": "created_at", "updated_at": "updated_at"}[sort]
    values = [i[key] for i in items if i[key] is not None]
    assert values == sorted(values, reverse=(order == "desc"))
    if sort == "confidence":  # cases without a result sort after all that have one
        seen_null = False
        for i in items:
            seen_null |= i["confidence"] is None
            assert not (seen_null and i["confidence"] is not None)


async def test_pagination_is_consistent(api: Api) -> None:
    for _ in range(3):
        await api.make_case("clear", "CALD")
    params = {"sort": "created_at", "order": "asc"}
    page1 = (await api.http.get("/cases", params={**params, "limit": 2, "offset": 0})).json()
    page2 = (await api.http.get("/cases", params={**params, "limit": 2, "offset": 2})).json()
    assert page1["total"] == page2["total"] >= 3 and len(page1["items"]) == 2
    assert not {c["id"] for c in page1["items"]} & {c["id"] for c in page2["items"]}
    beyond = (await api.http.get("/cases", params={"offset": page1["total"] + 10})).json()
    assert beyond["items"] == [] and beyond["total"] == page1["total"]


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 201},
        {"offset": -1},
        {"status": "NOPE"},
        {"sort": "nope"},
        {"order": "up"},
    ],
)
async def test_invalid_query_parameters_are_rejected(api: Api, params: dict[str, Any]) -> None:
    assert (await api.http.get("/cases", params=params)).status_code == 422


async def test_case_detail_has_evidence_audit_tool_calls_and_approval(api: Api) -> None:
    case_id = await api.make_case("clear", "SSIM")
    r = await api.http.get(f"/cases/{case_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["id"] == str(case_id)
    result = body["case"]["result"]
    assert result["confidence"] == pytest.approx(0.97) and result["evidence"]
    assert {e["source"] for e in result["evidence"]} >= {"ssi-store", "trade-store"}
    assert result["proposed_fix"]["action"] == "AMEND_SSI"
    assert body["case"]["event"]["trade_id"] and body["case"]["event"]["raw_message"]
    actions = [a["action"] for a in body["audit"]]
    assert actions[0] == "case.received" and actions[-1] == "approval.requested"
    assert body["tool_calls"] and body["approval"]["status"] == "PENDING"


async def test_case_detail_not_found_and_bad_id(api: Api) -> None:
    assert (await api.http.get(f"/cases/{uuid4()}")).status_code == 404
    assert (await api.http.get("/cases/not-a-uuid")).status_code == 422


# --------------------------------------------------------------- approvals
async def test_pending_approvals_listing(api: Api) -> None:
    case_id = await api.make_case("clear", "DENO")
    r = await api.http.get("/approvals", params={"limit": 200})
    assert r.status_code == 200
    mine = next(i for i in r.json()["items"] if i["case"]["id"] == str(case_id))
    assert mine["approval"]["status"] == "PENDING"
    assert (
        mine["case"]["fix_action"] == "RESUBMIT"
        and mine["approval"]["proposed_fix"]["requires_write"]
    )
    assert (await api.http.get("/approvals", params={"status": "ALL"})).status_code == 200
    assert (await api.http.get("/approvals", params={"status": "BOGUS"})).status_code == 422


async def test_approve_executes_and_is_idempotent(api: Api) -> None:
    case_id = await api.make_case("clear", "SSIM")
    approval_id = await api.approval_id(case_id)
    r = await api.http.post(f"/approvals/{approval_id}/approve", json={"decided_by": "alice"})
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] is True and body["case"]["status"] == "RESOLVED"
    assert body["approval"]["status"] == "APPROVED" and body["approval"]["decided_by"] == "alice"

    again = await api.http.post(f"/approvals/{approval_id}/approve", json={"decided_by": "bob"})
    assert again.status_code == 200 and again.json()["changed"] is False
    assert again.json()["approval"]["decided_by"] == "alice"

    detail = (await api.http.get(f"/cases/{case_id}")).json()
    executed = [a for a in detail["audit"] if a["action"] == "remediation.executed"]
    assert len(executed) == 1 and executed[0]["actor_id"] == "alice"
    still_pending = (await api.http.get("/approvals", params={"limit": 200})).json()["items"]
    assert str(approval_id) not in {i["approval"]["id"] for i in still_pending}


async def test_reject_then_conflicting_approve_is_409(api: Api) -> None:
    case_id = await api.make_case("clear", "SSIM")
    approval_id = await api.approval_id(case_id)
    r = await api.http.post(
        f"/approvals/{approval_id}/reject", json={"decided_by": "alice", "reason": "wrong SSI"}
    )
    assert r.status_code == 200 and r.json()["case"]["status"] == "ESCALATED"
    conflict = await api.http.post(f"/approvals/{approval_id}/approve", json={"decided_by": "bob"})
    assert conflict.status_code == 409 and "already" in conflict.json()["detail"]
    rejected = [
        a
        for a in (await api.http.get(f"/cases/{case_id}")).json()["audit"]
        if a["action"] == "approval.rejected"
    ]
    assert rejected[0]["payload"]["reason"] == "wrong SSI"


async def test_reverse_flow_and_its_preconditions(api: Api) -> None:
    case_id = await api.make_case("clear", "SSIM")
    approval_id = await api.approval_id(case_id)
    early = await api.http.post(f"/approvals/{approval_id}/reverse", json={"decided_by": "carol"})
    assert early.status_code == 409  # nothing was executed yet
    await api.http.post(f"/approvals/{approval_id}/approve", json={"decided_by": "alice"})
    r = await api.http.post(
        f"/approvals/{approval_id}/reverse", json={"decided_by": "carol", "reason": "oops"}
    )
    assert (
        r.status_code == 200 and r.json()["case"]["status"] == "ESCALATED" and r.json()["changed"]
    )
    again = await api.http.post(f"/approvals/{approval_id}/reverse", json={"decided_by": "carol"})
    assert again.status_code == 200 and again.json()["changed"] is False


async def test_unknown_approval_is_404(api: Api) -> None:
    for action in ("approve", "reject", "reverse"):
        r = await api.http.post(f"/approvals/{uuid4()}/{action}", json={"decided_by": "a"})
        assert r.status_code == 404, action


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"decided_by": ""},
        {"decided_by": "   "},
        {"decided_by": "a" * 129},
        {"decided_by": "a", "x": 1},
    ],
)
async def test_decision_body_is_validated(api: Api, body: dict[str, Any]) -> None:
    case_id = await api.make_case("clear", "SSIM")
    approval_id = await api.approval_id(case_id)
    assert (await api.http.post(f"/approvals/{approval_id}/approve", json=body)).status_code == 422
    detail = (await api.http.get(f"/cases/{case_id}")).json()
    assert detail["approval"]["status"] == "PENDING"  # a malformed request changed nothing


async def test_transient_settlement_failure_is_503_and_retry_succeeds(api: Api) -> None:
    case_id = await api.make_case("clear", "SSIM")
    approval_id = await api.approval_id(case_id)
    api.system.fail_transient = True
    r = await api.http.post(f"/approvals/{approval_id}/approve", json={"decided_by": "alice"})
    assert r.status_code == 503 and r.headers["retry-after"] == "5"
    retry = await api.http.post(f"/approvals/{approval_id}/approve", json={"decided_by": "alice"})
    assert retry.status_code == 200 and retry.json()["case"]["status"] == "RESOLVED"


async def test_approve_for_a_case_that_left_awaiting_is_409(api: Api) -> None:
    case_id = await api.make_case("clear", "SSIM")
    approval_id = await api.approval_id(case_id)
    from settleagent.domain.case import CaseStatus
    from settleagent.persistence.repositories import CaseRepository

    async with api.sessions() as s, s.begin():
        await CaseRepository(s).transition(case_id, CaseStatus.ESCALATED, NOW)
    r = await api.http.post(f"/approvals/{approval_id}/approve", json={"decided_by": "alice"})
    assert r.status_code == 409


# ----------------------------------------------------------------- metrics
async def test_metrics_are_consistent_with_the_listings(api: Api) -> None:
    await api.make_case("clear", "SSIM")
    await api.make_case("clear", "CALD")
    await api.make_case("ambiguous", "LACK")
    m = (await api.http.get("/metrics/summary")).json()
    total = (await api.http.get("/cases", params={"limit": 1})).json()["total"]
    queue = (await api.http.get("/cases", params={"queue": "true", "limit": 1})).json()["total"]
    pending = (await api.http.get("/approvals", params={"limit": 1})).json()["total"]
    assert m["total_cases"] == total == sum(m["by_status"].values())
    assert m["queue_depth"] == queue and m["pending_approvals"] == pending
    assert m["decided_cases"] >= 3 and m["auto_resolve_decisions"] >= 2
    assert 0 < m["auto_resolve_rate"] <= 1
    assert m["auto_resolve_rate"] == pytest.approx(m["auto_resolve_decisions"] / m["decided_cases"])
    assert 0 <= m["average_confidence"] <= 1
    assert m["total_tokens"] > 0


# ------------------------------------------------- cost, latency, tool health, budget
async def test_case_detail_reports_usage(api: Api) -> None:
    case_id = await api.make_case("clear", "SSIM")
    usage = (await api.http.get(f"/cases/{case_id}")).json()["usage"]
    assert usage["model"] == "fake-model" and usage["steps_used"] >= 3
    assert usage["input_tokens"] > 0 and usage["output_tokens"] > 0
    assert usage["cost_usd"] > 0 and usage["duration_ms"] >= 0


async def test_metrics_include_latency_cost_and_tool_health(
    api: Api, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await api.make_case("clear", "SSIM")
    await api.make_case("clear", "CALD")
    m = (await api.http.get("/metrics/summary")).json()
    assert m["investigations"] >= 2 and m["tool_calls"] > 0
    assert 0 <= m["p50_run_latency_ms"] <= m["p95_run_latency_ms"]
    assert m["tool_error_rate"] == pytest.approx(m["tool_infra_failures"] / m["tool_calls"])

    from sqlalchemy import text

    async with sessions() as s:  # the API figure must equal an independent sum over the audit trail
        raw, costed = (
            await s.execute(
                text(
                    "SELECT COALESCE(SUM((payload->>'cost_usd')::float), 0), "
                    "COUNT(payload->>'cost_usd') FROM audit_events "
                    "WHERE action = 'investigation.completed'"
                )
            )
        ).one()
    assert m["total_cost_usd"] == pytest.approx(float(raw))
    # the average only counts runs that recorded a cost (older audit rows have none)
    assert m["average_cost_usd"] == pytest.approx(float(raw) / costed)


@pytest.mark.parametrize(
    ("ceiling", "warn", "level"),
    [(1_000_000.0, 0.8, "ok"), (1e-9, 0.8, "exceeded")],
)
async def test_budget_level_reflects_spend(
    sessions: async_sessionmaker[AsyncSession], ceiling: float, warn: float, level: str
) -> None:
    a = Api(sessions, ceiling_usd=ceiling, warn_fraction=warn)
    try:
        await a.make_case("clear", "CALD")
        budget = (await a.http.get("/metrics/summary")).json()["budget"]
    finally:
        await a.http.aclose()
    assert budget["level"] == level and budget["ceiling_usd"] == ceiling
    assert budget["used_fraction"] == pytest.approx(budget["used_usd"] / ceiling)
