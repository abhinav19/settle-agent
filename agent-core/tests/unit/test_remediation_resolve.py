"""resolve_write_fix: a write is executable only if the case's own evidence supports it."""

import json
from datetime import date
from typing import Any
from uuid import uuid4

import pytest

from settleagent.domain.investigation import (
    Evidence,
    FixAction,
    InvestigationResult,
    ProposedFix,
)
from settleagent.domain.reason_codes import ReasonCategory, ToolName
from settleagent.remediation.models import FixNotExecutableError
from settleagent.remediation.resolve import resolve_write_fix
from tests.factories import make_event, make_result

EVENT = make_event(trade_id="T-9", quantity="1050", settlement_date="2026-10-09")


def ev(tool: ToolName, **raw: Any) -> Evidence:
    return Evidence(source=tool, statement="s", raw=raw)


def result(
    action: FixAction,
    evidence: list[Evidence],
    params: dict[str, Any] | None = None,
    category: ReasonCategory = ReasonCategory.STATIC_DATA,
) -> InvestigationResult:
    return make_result(
        reason_category=category,
        evidence=evidence,
        proposed_fix=ProposedFix(action=action, parameters=params or {}, requires_write=True),
    )


SSI_EV = ev(
    ToolName.SSI_STORE,
    is_current=False,
    instructed_ssi={"ssi_id": "SSI-1"},
    current_ssi={"ssi_id": "SSI-2"},
)


# ------------------------------------------------------------------ AMEND_SSI
def test_amend_ssi_uses_current_ssi_from_evidence_and_records_the_inverse() -> None:
    fix = resolve_write_fix(EVENT, result(FixAction.AMEND_SSI, [SSI_EV]))
    assert fix.parameters == {"trade_id": "T-9", "new_ssi_id": "SSI-2"}
    assert fix.inverse == {"op": "restore_ssi", "trade_id": "T-9", "ssi_id": "SSI-1"}


def test_amend_ssi_accepts_a_matching_model_value() -> None:
    resolve_write_fix(EVENT, result(FixAction.AMEND_SSI, [SSI_EV], {"new_ssi_id": "SSI-2"}))


@pytest.mark.parametrize(
    ("evidence", "params", "fragment"),
    [
        ([SSI_EV], {"new_ssi_id": "SSI-EVIL"}, "evidence supports"),
        ([SSI_EV], {"trade_id": "T-OTHER"}, "not this case's trade"),
        ([ev(ToolName.TRADE_STORE, x=1)], {}, "no SSI evidence"),
        (
            [
                ev(
                    ToolName.SSI_STORE,
                    is_current=True,
                    instructed_ssi={"ssi_id": "A"},
                    current_ssi={"ssi_id": "A"},
                )
            ],
            {},
            "stale",
        ),
        (
            [
                ev(
                    ToolName.SSI_STORE,
                    is_current=False,
                    instructed_ssi={"ssi_id": "A"},
                    current_ssi={"ssi_id": "A"},
                )
            ],
            {},
            "different current SSI",
        ),
    ],
)
def test_amend_ssi_rejections(
    evidence: list[Evidence], params: dict[str, Any], fragment: str
) -> None:
    with pytest.raises(FixNotExecutableError, match=fragment):
        resolve_write_fix(EVENT, result(FixAction.AMEND_SSI, evidence, params))


# ------------------------------------------------------------------- RESUBMIT
def test_resubmit_for_funding_keeps_the_quantity() -> None:
    fix = resolve_write_fix(
        EVENT,
        result(
            FixAction.RESUBMIT, [ev(ToolName.TRADE_STORE, a=1)], category=ReasonCategory.FUNDING
        ),
    )
    assert fix.parameters == {"trade_id": "T-9", "quantity": "1050"}


def test_resubmit_for_a_lot_break_rounds_down_to_whole_lots() -> None:
    lot = ev(ToolName.SECURITY_MASTER, lot_size=100)
    fix = resolve_write_fix(
        EVENT, result(FixAction.RESUBMIT, [lot], category=ReasonCategory.QUANTITY)
    )
    assert fix.parameters["quantity"] == "1000"


@pytest.mark.parametrize(
    ("evidence", "params", "fragment"),
    [
        ([ev(ToolName.TRADE_STORE, a=1)], {}, "lot-size"),
        ([ev(ToolName.SECURITY_MASTER, lot_size=2000)], {}, "zero lots"),
        ([ev(ToolName.SECURITY_MASTER, lot_size=100)], {"quantity": "5000"}, "evidence supports"),
        ([ev(ToolName.SECURITY_MASTER, lot_size=True)], {}, "lot-size"),
    ],
)
def test_resubmit_quantity_rejections(
    evidence: list[Evidence], params: dict[str, Any], fragment: str
) -> None:
    with pytest.raises(FixNotExecutableError, match=fragment):
        resolve_write_fix(
            EVENT, result(FixAction.RESUBMIT, evidence, params, ReasonCategory.QUANTITY)
        )


# ------------------------------------------------------------- PARTIAL_SETTLE
def test_partial_settle_uses_available_securities() -> None:
    fix = resolve_write_fix(
        EVENT,
        result(FixAction.PARTIAL_SETTLE, [ev(ToolName.TRADE_STORE, securities_available="700")]),
    )
    assert fix.parameters["quantity"] == "700"


def test_partial_settle_rounds_to_lots() -> None:
    evidence = [
        ev(ToolName.TRADE_STORE, securities_available="750"),
        ev(ToolName.SECURITY_MASTER, lot_size=100),
    ]
    assert (
        resolve_write_fix(EVENT, result(FixAction.PARTIAL_SETTLE, evidence)).parameters["quantity"]
        == "700"
    )


@pytest.mark.parametrize("available", ["0", "1050", "2000", "-5", "abc"])
def test_partial_settle_must_be_strictly_between_zero_and_the_full_quantity(available: str) -> None:
    with pytest.raises(FixNotExecutableError):
        resolve_write_fix(
            EVENT,
            result(
                FixAction.PARTIAL_SETTLE, [ev(ToolName.TRADE_STORE, securities_available=available)]
            ),
        )


def test_partial_settle_needs_availability_evidence() -> None:
    with pytest.raises(FixNotExecutableError, match="availability"):
        resolve_write_fix(EVENT, result(FixAction.PARTIAL_SETTLE, [ev(ToolName.TRADE_STORE, a=1)]))


# --------------------------------------------------------------------- REBOOK
def test_rebook_uses_next_business_day_from_the_calendar() -> None:
    fix = resolve_write_fix(
        EVENT,
        result(
            FixAction.REBOOK_SETTLEMENT_DATE,
            [ev(ToolName.HOLIDAY_CALENDAR, next_business_day="2026-10-12")],
        ),
    )
    assert fix.parameters["new_settlement_date"] == "2026-10-12"
    assert fix.inverse["settlement_date"] == "2026-10-09"


@pytest.mark.parametrize("nbd", ["2026-10-09", "2026-10-01", "garbage"])
def test_rebook_must_move_forward(nbd: str) -> None:
    with pytest.raises(FixNotExecutableError):
        resolve_write_fix(
            EVENT,
            result(
                FixAction.REBOOK_SETTLEMENT_DATE,
                [ev(ToolName.HOLIDAY_CALENDAR, next_business_day=nbd)],
            ),
        )


def test_rebook_rejects_a_model_supplied_date_that_disagrees() -> None:
    with pytest.raises(FixNotExecutableError, match="evidence supports"):
        resolve_write_fix(
            EVENT,
            result(
                FixAction.REBOOK_SETTLEMENT_DATE,
                [ev(ToolName.HOLIDAY_CALENDAR, next_business_day="2026-10-12")],
                {"new_settlement_date": "2027-01-01"},
            ),
        )


# ---------------------------------------------------------------- general
def test_read_only_and_missing_fixes_are_not_executable() -> None:
    with pytest.raises(FixNotExecutableError, match="read-only"):
        resolve_write_fix(
            EVENT,
            make_result(
                proposed_fix=ProposedFix(
                    action=FixAction.NO_ACTION, parameters={}, requires_write=False
                )
            ),
        )
    with pytest.raises(FixNotExecutableError, match="no proposed fix"):
        resolve_write_fix(EVENT, make_result(proposed_fix=None))


def test_evidence_from_the_wrong_tool_is_ignored() -> None:
    """SSI-shaped data from trade-store must not satisfy an SSI amendment."""
    spoof = ev(
        ToolName.TRADE_STORE,
        is_current=False,
        instructed_ssi={"ssi_id": "A"},
        current_ssi={"ssi_id": "B"},
    )
    with pytest.raises(FixNotExecutableError, match="no SSI evidence"):
        resolve_write_fix(EVENT, result(FixAction.AMEND_SSI, [spoof]))


def test_resolved_fix_is_json_serialisable_and_bound_to_the_case() -> None:
    r = result(FixAction.AMEND_SSI, [SSI_EV])
    fix = resolve_write_fix(EVENT, r)
    assert fix.case_id == r.case_id and json.loads(fix.model_dump_json())["trade_id"] == "T-9"
    assert uuid4() != fix.case_id and date(2026, 10, 9) == EVENT.settlement_date
