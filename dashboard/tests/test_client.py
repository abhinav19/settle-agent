import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from ops_dashboard.client import API_KEY_HEADER, ApiClient, ApiError, ApiUnavailable


def make(handler: Callable[[httpx.Request], httpx.Response]) -> ApiClient:
    return ApiClient("http://api/", "secret-key", transport=httpx.MockTransport(handler))


def test_sends_the_api_key_and_builds_query_parameters() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"items": [], "total": 0, "limit": 100, "offset": 0})

    make(handler).list_cases(queue=True, statuses=["ESCALATED"], sort="confidence", order="asc")
    [req] = seen
    assert req.headers[API_KEY_HEADER] == "secret-key"
    assert req.url.path == "/cases"
    q = req.url.params
    assert q["queue"] == "true" and q["sort"] == "confidence" and q["order"] == "asc"
    assert q.get_list("status") == ["ESCALATED"]


def test_decisions_post_json_bodies() -> None:
    bodies: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"changed": True})

    c = make(handler)
    c.approve("a1", "alice")
    c.reject("a2", "bob", "wrong SSI")
    c.reverse("a3", "carol", None)
    assert bodies == [
        ("/approvals/a1/approve", {"decided_by": "alice"}),
        ("/approvals/a2/reject", {"decided_by": "bob", "reason": "wrong SSI"}),
        ("/approvals/a3/reverse", {"decided_by": "carol"}),
    ]


def test_read_endpoints() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"ok": True})

    c = make(handler)
    c.case_detail("abc")
    c.list_approvals("ALL")
    c.metrics()
    assert paths == ["/cases/abc", "/approvals", "/metrics/summary"]


@pytest.mark.parametrize(
    ("status", "retryable"), [(401, False), (404, False), (409, False), (502, True), (503, True)]
)
def test_error_statuses_become_api_errors_with_the_detail(status: int, retryable: bool) -> None:
    c = make(lambda r: httpx.Response(status, json={"detail": "nope"}))
    with pytest.raises(ApiError) as exc:
        c.metrics()
    assert exc.value.status == status and exc.value.detail == "nope"
    assert exc.value.retryable is retryable


def test_non_json_error_body_is_tolerated() -> None:
    c = make(lambda r: httpx.Response(500, text="<html>boom</html>"))
    with pytest.raises(ApiError) as exc:
        c.metrics()
    assert "boom" in exc.value.detail


def test_connection_failure_is_api_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(ApiUnavailable, match="cannot reach"):
        make(handler).metrics()
