"""Typed-ish HTTP client for the control-plane API. The dashboard's ONLY data source."""

from typing import Any

import httpx

API_KEY_HEADER = "X-API-Key"


class ApiError(Exception):
    """The API answered with an error status."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail

    @property
    def retryable(self) -> bool:
        return self.status in {502, 503, 504}


class ApiUnavailable(Exception):
    """The API could not be reached at all."""


class ApiClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_s: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={API_KEY_HEADER: api_key},
            timeout=timeout_s,
            transport=transport,
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ApiUnavailable(f"cannot reach the API: {exc}") from exc
        if response.is_error:
            try:
                detail = str(response.json().get("detail", response.text))
            except ValueError:
                detail = response.text[:200]
            raise ApiError(response.status_code, detail)
        return response.json()

    # ------------------------------------------------------------------ reads
    def list_cases(
        self,
        *,
        queue: bool = False,
        statuses: list[str] | None = None,
        sort: str = "created_at",
        order: str = "desc",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        params: list[tuple[str, str]] = [
            ("sort", sort),
            ("order", order),
            ("limit", str(limit)),
            ("offset", str(offset)),
        ]
        if queue:
            params.append(("queue", "true"))
        params += [("status", s) for s in statuses or []]
        result: dict[str, Any] = self._request("GET", "/cases", params=params)
        return result

    def case_detail(self, case_id: str) -> dict[str, Any]:
        result: dict[str, Any] = self._request("GET", f"/cases/{case_id}")
        return result

    def list_approvals(self, status: str = "PENDING", limit: int = 100) -> dict[str, Any]:
        result: dict[str, Any] = self._request(
            "GET", "/approvals", params={"status": status, "limit": limit}
        )
        return result

    def metrics(self) -> dict[str, Any]:
        result: dict[str, Any] = self._request("GET", "/metrics/summary")
        return result

    # ---------------------------------------------------------------- actions
    def approve(self, approval_id: str, decided_by: str) -> dict[str, Any]:
        return self._decide("approve", approval_id, decided_by, None)

    def reject(self, approval_id: str, decided_by: str, reason: str | None) -> dict[str, Any]:
        return self._decide("reject", approval_id, decided_by, reason)

    def reverse(self, approval_id: str, decided_by: str, reason: str | None) -> dict[str, Any]:
        return self._decide("reverse", approval_id, decided_by, reason)

    def _decide(
        self, action: str, approval_id: str, decided_by: str, reason: str | None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"decided_by": decided_by}
        if reason:
            body["reason"] = reason
        result: dict[str, Any] = self._request(
            "POST", f"/approvals/{approval_id}/{action}", json=body
        )
        return result
