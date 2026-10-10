"""Test data builders shared by unit and integration tests."""

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

from settleagent.domain.events import SettlementFailureEvent
from settleagent.domain.investigation import (
    Evidence,
    FixAction,
    InvestigationResult,
    ProposedFix,
)
from settleagent.domain.reason_codes import ReasonCategory, ReasonCode, ToolName

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
VALID_ISIN = "US0378331005"


def event_payload(**overrides: Any) -> dict[str, Any]:
    """JSON-compatible dict for a valid SettlementFailureEvent."""
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "event_id": str(uuid4()),
        "emitted_at": NOW.isoformat(),
        "trade_id": "T-0001",
        "isin": VALID_ISIN,
        "settlement_date": date(2026, 10, 9).isoformat(),
        "counterparty_bic": "DEUTDEFF",
        "reason_code": "SSIM",
        "quantity": "1000",
        "cash_amount": "185000.50",
        "currency": "USD",
        "raw_message": ":16R:GENL ... (synthetic MT548)",
    }
    payload.update(overrides)
    return payload


def make_event(**overrides: Any) -> SettlementFailureEvent:
    return SettlementFailureEvent.model_validate_json(_dumps(event_payload(**overrides)))


def make_evidence() -> Evidence:
    return Evidence(source=ToolName.SSI_STORE, statement="SSI is stale", raw={"bic": "DEUTDEFF"})


def make_fix(*, requires_write: bool = True) -> ProposedFix:
    return ProposedFix(
        action=FixAction.AMEND_SSI, parameters={"account": "X"}, requires_write=requires_write
    )


def make_result(case_id: UUID | None = None, **overrides: Any) -> InvestigationResult:
    fields: dict[str, Any] = {
        "schema_version": "1.0",
        "case_id": case_id or uuid4(),
        "root_cause": "Counterparty SSI changed on 2026-10-01",
        "reason_category": ReasonCategory.STATIC_DATA,
        "evidence": [make_evidence()],
        "proposed_fix": make_fix(),
        "confidence": 0.95,
        "model": "claude-sonnet-5-5",
        "tokens_used": 1234,
        "reasoning_trace_id": "trace-1",
    }
    fields.update(overrides)
    return InvestigationResult(**fields)


def _dumps(d: dict[str, Any]) -> str:
    import json

    return json.dumps(d)


__all__ = ["NOW", "VALID_ISIN", "ReasonCode", "event_payload", "make_event", "make_result"]
