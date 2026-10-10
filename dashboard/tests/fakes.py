"""A stateful in-memory stand-in for the control-plane API (module-level so it survives reruns)."""

from typing import Any

from ops_dashboard.client import ApiError, ApiUnavailable

AWAITING = "11111111-1111-1111-1111-111111111111"
ESCALATED = "22222222-2222-2222-2222-222222222222"
APPROVAL = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _summary(
    cid: str, status: str, conf: float | None, fix: str | None, write: bool | None
) -> dict[str, Any]:
    return {
        "id": cid,
        "event_id": cid,
        "status": status,
        "decision": "AUTO_RESOLVE",
        "trade_id": f"T-{cid[:2]}",
        "isin": "US0378331005",
        "reason_code": "SSIM",
        "category": "STATIC_DATA",
        "root_cause": "Trade uses a superseded SSI",
        "confidence": conf,
        "fix_action": fix,
        "requires_write": write,
        "trace_id": "trace123",
        "created_at": "2026-10-10T10:00:00+00:00",
        "updated_at": "2026-10-10T10:05:00+00:00",
    }


class FakeApi:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.fail: Exception | None = None
        self.queue_empty = False
        self.usage: dict[str, Any] | None = {
            "model": "claude-opus-5-5",
            "input_tokens": 12000,
            "output_tokens": 800,
            "cost_usd": 0.064,
            "duration_ms": 4200,
            "steps_used": 7,
        }
        self.budget_level = "ok"
        self.awaiting = _summary(AWAITING, "AWAITING_APPROVAL", 0.95, "AMEND_SSI", True)
        self.escalated = _summary(ESCALATED, "ESCALATED", 0.4, None, None)
        self.approval: dict[str, Any] = {
            "id": APPROVAL,
            "case_id": AWAITING,
            "status": "PENDING",
            "proposed_fix": {
                "action": "AMEND_SSI",
                "parameters": {"trade_id": "T-11"},
                "requires_write": True,
            },
            "decided_by": None,
            "decided_at": None,
            "created_at": "2026-10-10T10:05:00+00:00",
        }

    def _maybe_fail(self) -> None:
        if self.fail is not None:
            raise self.fail

    def list_cases(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("list_cases", (), kw))
        self._maybe_fail()
        items = [] if self.queue_empty else [self.awaiting, self.escalated]
        if kw.get("queue"):
            items = [c for c in items if c["status"] in {"AWAITING_APPROVAL", "ESCALATED"}]
        if kw.get("statuses"):
            items = [c for c in items if c["status"] in kw["statuses"]]
        return {"items": items, "total": len(items), "limit": 100, "offset": 0}

    def case_detail(self, case_id: str) -> dict[str, Any]:
        self.calls.append(("case_detail", (case_id,), {}))
        self._maybe_fail()
        summary = self.awaiting if case_id == AWAITING else self.escalated
        fix = (
            {"action": "AMEND_SSI", "parameters": {"trade_id": "T-11"}, "requires_write": True}
            if case_id == AWAITING
            else None
        )
        return {
            "summary": summary,
            "case": {
                "event": {
                    "trade_id": summary["trade_id"],
                    "isin": "US0378331005",
                    "counterparty_bic": "DEUTDEFF",
                    "quantity": "1000",
                    "cash_amount": "185000.50",
                    "currency": "USD",
                    "settlement_date": "2026-10-09",
                    "reason_code": "SSIM",
                    "raw_message": ":16R:GENL ...",
                },
                "result": {
                    "root_cause": summary["root_cause"],
                    "reason_category": "STATIC_DATA",
                    "confidence": summary["confidence"],
                    "model": "claude-test",
                    "tokens_used": 321,
                    "reasoning_trace_id": "trace123",
                    "evidence": [
                        {
                            "source": "ssi-store",
                            "statement": "SSI superseded",
                            "raw": {"is_current": False},
                        }
                    ],
                    "proposed_fix": fix,
                },
            },
            "approval": self.approval if case_id == AWAITING else None,
            "audit": [
                {
                    "ts": "2026-10-10T10:00:00+00:00",
                    "actor": "system",
                    "actor_id": None,
                    "action": "case.received",
                    "payload": {},
                }
            ],
            "tool_calls": [
                {
                    "tool": "ssi-store",
                    "request": {"trade_id": "T-11"},
                    "latency_ms": 12,
                    "error": None,
                }
            ],
            "usage": self.usage,
        }

    def list_approvals(self, status: str = "PENDING", limit: int = 100) -> dict[str, Any]:
        self.calls.append(("list_approvals", (status,), {}))
        self._maybe_fail()
        pending = self.approval["status"] == "PENDING"
        items = [{"approval": self.approval, "case": self.awaiting}] if pending else []
        return {"items": items, "total": len(items), "limit": limit, "offset": 0}

    def metrics(self) -> dict[str, Any]:
        self.calls.append(("metrics", (), {}))
        self._maybe_fail()
        return {
            "total_cases": 7,
            "by_status": {"ESCALATED": 3, "AUTO_RESOLVED": 4},
            "queue_depth": 3,
            "pending_approvals": 1,
            "decided_cases": 7,
            "auto_resolve_decisions": 4,
            "auto_resolve_rate": 4 / 7,
            "average_confidence": 0.8,
            "total_tokens": 12345,
            "investigations": 7,
            "p50_run_latency_ms": 850.0,
            "p95_run_latency_ms": 4200.0,
            "tool_calls": 40,
            "tool_infra_failures": 2,
            "tool_error_rate": 0.05,
            "total_cost_usd": 0.448,
            "average_cost_usd": 0.064,
            "budget": {
                "ceiling_usd": 50.0,
                "used_usd": 0.448,
                "used_fraction": 0.00896,
                "level": self.budget_level,
            },
        }

    def _decide(
        self, name: str, status: str, case_status: str, by: str, reason: str | None
    ) -> dict[str, Any]:
        self.calls.append((name, (APPROVAL, by), {"reason": reason}))
        self._maybe_fail()
        self.approval = {**self.approval, "status": status, "decided_by": by, "decided_at": "now"}
        self.awaiting = {**self.awaiting, "status": case_status}
        return {"changed": True, "case": self.awaiting, "approval": self.approval}

    def approve(self, approval_id: str, decided_by: str) -> dict[str, Any]:
        return self._decide("approve", "APPROVED", "RESOLVED", decided_by, None)

    def reject(self, approval_id: str, decided_by: str, reason: str | None) -> dict[str, Any]:
        return self._decide("reject", "REJECTED", "ESCALATED", decided_by, reason)

    def reverse(self, approval_id: str, decided_by: str, reason: str | None) -> dict[str, Any]:
        return self._decide("reverse", "APPROVED", "ESCALATED", decided_by, reason)


API = FakeApi()
errors = {"unavailable": ApiUnavailable("cannot reach the API: refused")}
__all__ = ["API", "APPROVAL", "AWAITING", "ESCALATED", "ApiError", "errors"]
