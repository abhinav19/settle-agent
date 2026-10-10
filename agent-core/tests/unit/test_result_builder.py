import json
from typing import Any
from uuid import uuid4

import pytest

from settleagent.agent.result_builder import (
    InvalidModelOutputError,
    build_result,
    escalation_result,
)
from settleagent.domain.investigation import FixAction
from settleagent.domain.reason_codes import ReasonCategory
from tests.factories import make_evidence

POOL = [make_evidence(), make_evidence()]


def raw(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "root_cause": "Trade references a superseded SSI",
        "reason_category": "STATIC_DATA",
        "evidence_refs": [0],
        "fix": {"action": "AMEND_SSI", "parameters_json": json.dumps({"trade_id": "T1"})},
        "confidence": 0.93,
    }
    base.update(over)
    return base


def build(r: dict[str, Any], missing: list[str] | None = None, cap: float = 0.6):  # type: ignore[no-untyped-def]
    return build_result(
        r,
        case_id=uuid4(),
        evidence_pool=POOL,
        missing=missing or [],
        model="m",
        tokens=7,
        trace_id="tr",
        degraded_cap=cap,
    )


def test_valid_output_builds_result() -> None:
    result, notes = build(raw())
    assert result.confidence == 0.93 and result.tokens_used == 7
    assert result.reasoning_trace_id == "tr" and notes == []
    assert result.evidence == [POOL[0]]


def test_requires_write_is_derived_from_the_action_not_the_model() -> None:
    result, _ = build(raw())
    assert result.proposed_fix is not None and result.proposed_fix.requires_write is True
    ro, _ = build(raw(fix={"action": "NO_ACTION", "parameters_json": "{}"}))
    assert ro.proposed_fix is not None and ro.proposed_fix.requires_write is False
    assert ro.proposed_fix.action is FixAction.NO_ACTION


def test_duplicate_refs_are_deduplicated() -> None:
    result, _ = build(raw(evidence_refs=[1, 1, 0]))
    assert result.evidence == [POOL[1], POOL[0]]


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"evidence_refs": [5]}, "out of range"),
        ({"evidence_refs": [-1]}, "out of range"),
        ({"evidence_refs": ["a"]}, "list of integers"),
        ({"evidence_refs": [True]}, "list of integers"),
        ({"evidence_refs": []}, "Evidence"),
        ({"confidence": 1.5}, "within"),
        ({"confidence": "high"}, "number"),
        ({"confidence": True}, "number"),
        ({"reason_category": "NOPE"}, "reason_category"),
        ({"root_cause": "  "}, "root_cause"),
        ({"root_cause": "undetermined"}, "undetermined"),
        ({"fix": {"action": "DELETE_ALL", "parameters_json": "{}"}}, "action"),
        ({"fix": {"action": "AMEND_SSI", "parameters_json": "not json"}}, "valid JSON"),
        ({"fix": {"action": "AMEND_SSI", "parameters_json": "[1]"}}, "JSON object"),
        ({"fix": "AMEND_SSI"}, "object"),
    ],
)
def test_malformed_output_rejected(override: dict[str, Any], fragment: str) -> None:
    with pytest.raises(InvalidModelOutputError, match=fragment):
        build(raw(**override))


@pytest.mark.parametrize(
    "key", ["root_cause", "reason_category", "evidence_refs", "fix", "confidence"]
)
def test_missing_field_rejected(key: str) -> None:
    r = raw()
    del r[key]
    with pytest.raises(InvalidModelOutputError, match=key):
        build(r)


def test_missing_evidence_caps_confidence() -> None:
    result, notes = build(raw(confidence=0.97), missing=["trade-store down"], cap=0.6)
    assert result.confidence == 0.6 and any("capped" in n for n in notes)


def test_cap_does_not_raise_lower_confidence() -> None:
    result, notes = build(raw(confidence=0.4), missing=["x"], cap=0.6)
    assert result.confidence == 0.4 and notes == []


def test_undetermined_drops_fix_and_normalises_text() -> None:
    result, notes = build(
        raw(root_cause="not sure", reason_category="UNDETERMINED", evidence_refs=[], confidence=0.1)
    )
    assert result.proposed_fix is None and result.root_cause == "undetermined"
    assert any("normalised" in n for n in notes) and any("dropped" in n for n in notes)


def test_very_low_confidence_drops_fix() -> None:
    result, notes = build(raw(confidence=0.1))
    assert result.proposed_fix is None and any("dropped" in n for n in notes)


def test_escalation_result_is_a_valid_human_bound_result() -> None:
    r = escalation_result(case_id=uuid4(), evidence=POOL, model="m", tokens=1, trace_id="t")
    assert r.reason_category is ReasonCategory.UNDETERMINED
    assert r.confidence == 0.0 and r.proposed_fix is None and r.evidence == POOL
