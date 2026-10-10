import pytest
from hypothesis import given
from hypothesis import strategies as st

from settleagent.domain.case import Decision
from settleagent.domain.investigation import FixAction, ProposedFix
from settleagent.domain.reason_codes import ReasonCategory
from settleagent.gate.policy import Policy, default_policy
from settleagent.gate.router import route, route_with_reason
from tests.factories import make_fix, make_result

POLICY = default_policy(0.9)


def test_high_confidence_with_eligible_fix_auto_resolves() -> None:
    assert route(make_result(confidence=0.95), POLICY) is Decision.AUTO_RESOLVE


def test_exactly_at_threshold_auto_resolves() -> None:
    assert route(make_result(confidence=0.9), POLICY) is Decision.AUTO_RESOLVE


def test_just_below_threshold_goes_to_human() -> None:
    d, why = route_with_reason(make_result(confidence=0.8999), POLICY)
    assert d is Decision.HUMAN_REVIEW and "below threshold" in why


def test_missing_fix_goes_to_human() -> None:
    d, why = route_with_reason(make_result(confidence=0.99, proposed_fix=None), POLICY)
    assert d is Decision.HUMAN_REVIEW and why == "no proposed fix"


def test_ineligible_action_goes_to_human() -> None:
    policy = Policy(
        version="t",
        auto_threshold=0.5,
        auto_eligible_actions=frozenset({FixAction.NO_ACTION}),
    )
    assert route(make_result(confidence=0.99), policy) is Decision.HUMAN_REVIEW
    ok = make_result(
        confidence=0.99,
        proposed_fix=ProposedFix(action=FixAction.NO_ACTION, parameters={}, requires_write=False),
    )
    assert route(ok, policy) is Decision.AUTO_RESOLVE


def test_undetermined_goes_to_human_regardless_of_threshold() -> None:
    r = make_result(
        root_cause="undetermined",
        reason_category=ReasonCategory.UNDETERMINED,
        evidence=[],
        proposed_fix=None,
        confidence=1.0,
    )
    assert route(r, default_policy(0.0)) is Decision.HUMAN_REVIEW


def test_threshold_zero_still_requires_a_fix() -> None:
    assert route(make_result(confidence=0.3, proposed_fix=None), default_policy(0.0)) is (
        Decision.HUMAN_REVIEW
    )


def test_threshold_one_requires_perfect_confidence() -> None:
    assert route(make_result(confidence=0.999), default_policy(1.0)) is Decision.HUMAN_REVIEW
    assert route(make_result(confidence=1.0), default_policy(1.0)) is Decision.AUTO_RESOLVE


def test_write_fix_can_be_auto_decided_but_stays_a_write() -> None:
    """The gate only routes; the approval gate downstream still applies to writes."""
    r = make_result(confidence=0.99, proposed_fix=make_fix(requires_write=True))
    assert route(r, POLICY) is Decision.AUTO_RESOLVE
    assert r.proposed_fix is not None and r.proposed_fix.requires_write


def test_policy_rejects_bad_threshold() -> None:
    with pytest.raises(ValueError):
        Policy(version="t", auto_threshold=1.2, auto_eligible_actions=frozenset())


@given(
    confidence=st.floats(min_value=0.0, max_value=1.0),
    threshold=st.floats(min_value=0.0, max_value=1.0),
    has_fix=st.booleans(),
)
def test_output_is_always_a_valid_decision_and_never_auto_below_threshold(
    confidence: float, threshold: float, has_fix: bool
) -> None:
    fix = make_fix() if has_fix and confidence >= 0.2 else None
    result = make_result(confidence=confidence, proposed_fix=fix)
    decision = route(result, default_policy(threshold))
    assert decision in set(Decision)
    if decision is Decision.AUTO_RESOLVE:
        assert result.confidence >= threshold and result.proposed_fix is not None


def test_write_action_cannot_be_declared_read_only() -> None:
    with pytest.raises(ValueError, match="requires_write must be True"):
        ProposedFix(action=FixAction.AMEND_SSI, parameters={}, requires_write=False)
