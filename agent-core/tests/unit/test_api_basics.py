"""API behaviour that needs no database: auth, liveness, readiness, OpenAPI contract."""

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from settleagent.api.app import ApiDeps, create_app
from settleagent.api.readiness import CheckResult, run_checks, tcp_check
from settleagent.api.security import API_KEY_HEADER

KEY = "test-key-0123456789"
SNAPSHOT = Path(__file__).resolve().parents[1] / "snapshots" / "openapi.json"


def make_app(checks: dict[str, Any] | None = None) -> Any:
    # sessions/remediation are never touched: auth and probes run before any handler logic
    return create_app(
        ApiDeps(sessions=None, remediation=None, api_key=KEY, readiness=checks or {})  # type: ignore[arg-type]
    )


def client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


PROTECTED = [
    ("GET", "/cases"),
    ("GET", f"/cases/{uuid4()}"),
    ("GET", "/approvals"),
    ("POST", f"/approvals/{uuid4()}/approve"),
    ("POST", f"/approvals/{uuid4()}/reject"),
    ("POST", f"/approvals/{uuid4()}/reverse"),
    ("GET", "/metrics/summary"),
]


@pytest.mark.parametrize(("method", "path"), PROTECTED)
@pytest.mark.parametrize(
    "headers", [{}, {API_KEY_HEADER: "wrong"}, {API_KEY_HEADER: ""}, {API_KEY_HEADER: KEY + "x"}]
)
async def test_every_protected_endpoint_rejects_missing_or_wrong_keys(
    method: str, path: str, headers: dict[str, str]
) -> None:
    async with client(make_app()) as c:
        r = await c.request(method, path, headers=headers, json={"decided_by": "a"})
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid or missing API key"}


async def test_health_and_docs_are_public() -> None:
    async with client(make_app()) as c:
        assert (await c.get("/health")).json() == {"status": "ok"}
        assert (await c.get("/openapi.json")).status_code == 200
        assert (await c.get("/docs")).status_code == 200


async def test_auth_does_not_leak_the_expected_key(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        async with client(make_app()) as c:
            await c.get("/cases", headers={API_KEY_HEADER: "attacker-guess"})
    assert KEY not in caplog.text and "rejected API request" in caplog.text


# ------------------------------------------------------------------ readiness
async def ok() -> CheckResult:
    return CheckResult(True, "fine")


async def bad() -> CheckResult:
    return CheckResult(False, "down")


async def boom() -> CheckResult:
    raise ConnectionError("refused")


async def slow() -> CheckResult:
    await asyncio.sleep(5)
    return CheckResult(True, "late")


async def test_ready_when_all_checks_pass() -> None:
    async with client(make_app({"database": ok, "kafka": ok})) as c:
        r = await c.get("/ready")
    assert r.status_code == 200 and r.json()["status"] == "ready"
    assert set(r.json()["checks"]) == {"database", "kafka"}


@pytest.mark.parametrize("failing", [bad, boom])
async def test_not_ready_when_a_dependency_fails(failing: Any) -> None:
    async with client(make_app({"database": ok, "kafka": failing})) as c:
        r = await c.get("/ready")
    body = r.json()
    assert r.status_code == 503 and body["status"] == "unavailable"
    assert body["checks"]["database"]["ok"] is True and body["checks"]["kafka"]["ok"] is False


async def test_slow_check_times_out_without_blocking_the_probe() -> None:
    results = await run_checks({"slow": slow, "fast": ok}, timeout_s=0.05)
    assert results["fast"].ok and not results["slow"].ok and "timed out" in results["slow"].detail


async def test_tcp_check_against_a_live_and_a_dead_port() -> None:
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        assert (await tcp_check(f"http://127.0.0.1:{port}")()).ok
    results = await run_checks({"otel": tcp_check(f"http://127.0.0.1:{port}", 0.5)}, 2)
    assert not results["otel"].ok  # the listener is gone


# --------------------------------------------------------------- OpenAPI contract
def test_openapi_matches_the_committed_snapshot() -> None:
    """The API is a public contract. Intentional changes: UPDATE_SNAPSHOTS=1 pytest, review diff."""
    current = make_app().openapi()
    if os.environ.get("UPDATE_SNAPSHOTS") or not SNAPSHOT.exists():
        SNAPSHOT.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    assert current == json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def test_openapi_documents_security_and_error_responses() -> None:
    spec = make_app().openapi()
    assert spec["components"]["securitySchemes"]["APIKeyHeader"]["name"] == API_KEY_HEADER
    approve = spec["paths"]["/approvals/{approval_id}/approve"]["post"]
    assert {"401", "404", "409", "503"} <= set(approve["responses"])
    assert "security" not in spec["paths"]["/health"]["get"]
    assert spec["paths"]["/cases"]["get"]["security"]


# ------------------------------------------------------- lifecycle and wiring
async def test_shutdown_hook_runs_when_the_app_stops() -> None:
    stopped: list[bool] = []

    async def on_shutdown() -> None:
        stopped.append(True)

    app = create_app(
        ApiDeps(sessions=None, remediation=None, api_key=KEY, on_shutdown=on_shutdown)  # type: ignore[arg-type]
    )
    async with app.router.lifespan_context(app):
        assert stopped == []
    assert stopped == [True]


async def test_permanent_settlement_error_maps_to_502() -> None:
    from settleagent.remediation.models import SettlementSystemError

    class Broken:
        async def approve(self, *a: Any, **k: Any) -> Any:
            raise SettlementSystemError("rejected downstream", transient=False)

    app = create_app(ApiDeps(sessions=None, remediation=Broken(), api_key=KEY))  # type: ignore[arg-type]
    async with client(app) as c:
        r = await c.post(
            f"/approvals/{uuid4()}/approve", json={"decided_by": "a"}, headers={API_KEY_HEADER: KEY}
        )
    assert r.status_code == 502 and "rejected downstream" in r.json()["detail"]


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    from settleagent.config import get_settings

    for k, v in {
        "KAFKA_BOOTSTRAP_SERVERS": "localhost:9092",
        "DATABASE_URL": "postgresql+psycopg://u:p@localhost/db",
        "API_KEY": "0123456789abcdef",
    }.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("otel_required", "expected"),
    [("true", {"database", "kafka", "otel"}), ("false", {"database", "kafka"})],
)
async def test_app_factory_wires_dependencies_from_settings(
    env: pytest.MonkeyPatch, otel_required: str, expected: set[str]
) -> None:
    from settleagent.api.main import create_app_from_settings

    env.setenv("READY_REQUIRES_OTEL", otel_required)
    app = create_app_from_settings()
    assert set(app.state.deps.readiness) == expected
    assert app.state.deps.api_key == "0123456789abcdef"
    async with app.router.lifespan_context(app):
        pass  # shutdown disposes the engine without error


# ------------------------------------------------------------ budget and usage helpers
@pytest.mark.parametrize(
    ("used", "level"),
    [
        (0.0, "ok"),
        (7.99, "ok"),
        (8.0, "warning"),
        (9.99, "warning"),
        (10.0, "exceeded"),
        (50.0, "exceeded"),
    ],
)
def test_budget_levels_at_the_boundaries(used: float, level: str) -> None:
    from settleagent.api.app import budget_status

    b = budget_status(used, ceiling=10.0, warn_fraction=0.8)
    assert b.level == level and b.used_fraction == pytest.approx(used / 10.0)


def test_usage_from_audit_handles_missing_and_partial_payloads() -> None:
    from datetime import UTC, datetime

    from settleagent.api.app import usage_from_audit
    from settleagent.domain.case import ActorType, AuditEvent

    def event(action: str, payload: dict[str, Any]) -> AuditEvent:
        return AuditEvent(
            case_id=uuid4(),
            actor=ActorType.AGENT,
            action=action,
            payload=payload,
            ts=datetime(2026, 1, 1, tzinfo=UTC),
        )

    assert usage_from_audit([]) is None
    assert usage_from_audit([event("case.received", {})]) is None
    old = usage_from_audit([event("investigation.completed", {"tokens": 5, "model": "m"})])
    assert old is not None and old.model == "m" and old.cost_usd is None and old.duration_ms is None
    latest = usage_from_audit(
        [
            event("investigation.completed", {"cost_usd": 1.0}),
            event("investigation.completed", {"cost_usd": 2.0}),
        ]
    )
    assert latest is not None and latest.cost_usd == 2.0  # the most recent run wins
