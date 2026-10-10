"""Turn raw model output into a validated ``InvestigationResult`` — deterministically.

The model proposes; this module disposes. It enforces, in code and not in the prompt:
- schema-valid shape and ranges, evidence references that exist;
- ``requires_write`` comes from the action, never from the model;
- fixes are dropped for undetermined / very-low-confidence results;
- confidence is capped when tool evidence is missing (graceful degradation).
"""

import json
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from settleagent.domain.investigation import (
    ACTION_REQUIRES_WRITE,
    LOW_CONFIDENCE_CEILING,
    UNDETERMINED_ROOT_CAUSE,
    Evidence,
    FixAction,
    InvestigationResult,
    ProposedFix,
)
from settleagent.domain.reason_codes import ReasonCategory


class InvalidModelOutputError(ValueError):
    """Model output failed validation; the message is fed back for one repair attempt."""


def _require(raw: dict[str, Any], key: str) -> Any:
    if key not in raw:
        raise InvalidModelOutputError(f"missing field '{key}'")
    return raw[key]


def _parse_fix(raw_fix: Any) -> ProposedFix | None:
    if raw_fix is None:
        return None
    if not isinstance(raw_fix, dict):
        raise InvalidModelOutputError("'fix' must be null or an object")
    try:
        action = FixAction(_require(raw_fix, "action"))
    except ValueError as exc:
        raise InvalidModelOutputError(f"unknown fix action: {exc}") from exc
    params_json = _require(raw_fix, "parameters_json")
    try:
        params = json.loads(params_json) if isinstance(params_json, str) else None
    except ValueError as exc:
        raise InvalidModelOutputError("parameters_json is not valid JSON") from exc
    if not isinstance(params, dict):
        raise InvalidModelOutputError("parameters_json must encode a JSON object")
    return ProposedFix(
        action=action, parameters=params, requires_write=ACTION_REQUIRES_WRITE[action]
    )


def build_result(
    raw: dict[str, Any],
    *,
    case_id: UUID,
    evidence_pool: list[Evidence],
    missing: list[str],
    model: str,
    tokens: int,
    trace_id: str,
    degraded_cap: float,
) -> tuple[InvestigationResult, list[str]]:
    """Validate and normalise. Returns the result and human-readable adjustment notes."""
    notes: list[str] = []

    root_cause = _require(raw, "root_cause")
    if not isinstance(root_cause, str) or not root_cause.strip():
        raise InvalidModelOutputError("root_cause must be a non-empty string")
    try:
        category = ReasonCategory(_require(raw, "reason_category"))
    except ValueError as exc:
        raise InvalidModelOutputError(f"unknown reason_category: {exc}") from exc

    refs = _require(raw, "evidence_refs")
    if not isinstance(refs, list) or not all(
        isinstance(i, int) and not isinstance(i, bool) for i in refs
    ):
        raise InvalidModelOutputError("evidence_refs must be a list of integers")
    bad = [i for i in refs if not 0 <= i < len(evidence_pool)]
    if bad:
        raise InvalidModelOutputError(f"evidence_refs out of range: {bad}")
    evidence = [evidence_pool[i] for i in dict.fromkeys(refs)]

    confidence = _require(raw, "confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, int | float):
        raise InvalidModelOutputError("confidence must be a number")
    confidence = float(confidence)
    if not 0.0 <= confidence <= 1.0:
        raise InvalidModelOutputError("confidence must be within [0, 1]")

    fix = _parse_fix(_require(raw, "fix"))

    undetermined_text = root_cause.strip().lower() == UNDETERMINED_ROOT_CAUSE
    if category is ReasonCategory.UNDETERMINED:
        if not undetermined_text:
            root_cause = UNDETERMINED_ROOT_CAUSE
            notes.append("normalised root_cause to 'undetermined'")
    elif undetermined_text:
        raise InvalidModelOutputError(
            "root_cause is 'undetermined' but reason_category names a cause"
        )

    if missing and confidence > degraded_cap:
        notes.append(f"confidence capped {confidence:.2f}->{degraded_cap:.2f}: evidence missing")
        confidence = degraded_cap

    if fix is not None and (
        category is ReasonCategory.UNDETERMINED or confidence < LOW_CONFIDENCE_CEILING
    ):
        notes.append(
            f"dropped proposed fix {fix.action}: result undetermined or very low confidence"
        )
        fix = None

    try:
        result = InvestigationResult(
            schema_version="1.0",
            case_id=case_id,
            root_cause=root_cause.strip(),
            reason_category=category,
            evidence=evidence,
            proposed_fix=fix,
            confidence=confidence,
            model=model,
            tokens_used=tokens,
            reasoning_trace_id=trace_id,
        )
    except ValidationError as exc:
        raise InvalidModelOutputError("; ".join(e["msg"] for e in exc.errors())) from exc
    return result, notes


def escalation_result(
    *,
    case_id: UUID,
    evidence: list[Evidence],
    model: str,
    tokens: int,
    trace_id: str,
) -> InvestigationResult:
    """Deterministic low-confidence result used when the agent cannot conclude (-> human queue)."""
    return InvestigationResult(
        schema_version="1.0",
        case_id=case_id,
        root_cause=UNDETERMINED_ROOT_CAUSE,
        reason_category=ReasonCategory.UNDETERMINED,
        evidence=evidence,
        proposed_fix=None,
        confidence=0.0,
        model=model,
        tokens_used=tokens,
        reasoning_trace_id=trace_id,
    )
