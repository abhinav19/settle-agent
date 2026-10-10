"""Deterministic confidence gate. Pure functions: no LLM, no I/O, no clock."""

from settleagent.domain.case import Decision
from settleagent.domain.investigation import InvestigationResult
from settleagent.domain.reason_codes import ReasonCategory
from settleagent.gate.policy import Policy


def route_with_reason(result: InvestigationResult, policy: Policy) -> tuple[Decision, str]:
    """Decide and say why. ``AUTO_RESOLVE`` requires every condition; anything else is human."""
    fix = result.proposed_fix
    if result.reason_category is ReasonCategory.UNDETERMINED:
        return Decision.HUMAN_REVIEW, "root cause undetermined"
    if fix is None:
        return Decision.HUMAN_REVIEW, "no proposed fix"
    if result.confidence < policy.auto_threshold:
        return (
            Decision.HUMAN_REVIEW,
            f"confidence {result.confidence:.3f} below threshold {policy.auto_threshold:.3f}",
        )
    if fix.action not in policy.auto_eligible_actions:
        return Decision.HUMAN_REVIEW, f"action {fix.action} is not auto-eligible"
    return Decision.AUTO_RESOLVE, "confidence and fix eligible for auto-resolution"


def route(result: InvestigationResult, policy: Policy) -> Decision:
    """The gate: ``confidence >= threshold`` and an auto-eligible fix -> AUTO_RESOLVE."""
    return route_with_reason(result, policy)[0]
