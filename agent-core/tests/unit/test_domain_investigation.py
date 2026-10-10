import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from settleagent.domain.investigation import UNDETERMINED_ROOT_CAUSE, InvestigationResult
from settleagent.domain.reason_codes import ReasonCategory
from tests.factories import make_result


def test_valid_high_confidence_result() -> None:
    r = make_result()
    assert r.confidence == 0.95
    assert r.proposed_fix is not None


def test_determined_cause_requires_evidence() -> None:
    with pytest.raises(ValidationError, match="Evidence"):
        make_result(evidence=[])


def test_low_confidence_forbids_fix() -> None:
    with pytest.raises(ValidationError, match="forbids a proposed_fix"):
        make_result(confidence=0.1)


def test_low_confidence_without_fix_is_ok() -> None:
    r = make_result(confidence=0.1, proposed_fix=None)
    assert r.proposed_fix is None


def test_confidence_boundary_at_ceiling_allows_fix() -> None:
    assert make_result(confidence=0.2).proposed_fix is not None


def test_undetermined_requires_matching_text_and_category() -> None:
    with pytest.raises(ValidationError, match="must match"):
        make_result(
            root_cause=UNDETERMINED_ROOT_CAUSE,
            reason_category=ReasonCategory.STATIC_DATA,
            proposed_fix=None,
        )
    with pytest.raises(ValidationError, match="must match"):
        make_result(
            root_cause="something specific",
            reason_category=ReasonCategory.UNDETERMINED,
            proposed_fix=None,
        )


def test_undetermined_ok_without_evidence_and_fix() -> None:
    r = make_result(
        root_cause=UNDETERMINED_ROOT_CAUSE,
        reason_category=ReasonCategory.UNDETERMINED,
        evidence=[],
        proposed_fix=None,
        confidence=0.05,
    )
    assert r.reason_category is ReasonCategory.UNDETERMINED


def test_undetermined_cannot_carry_fix() -> None:
    with pytest.raises(ValidationError):
        make_result(
            root_cause=UNDETERMINED_ROOT_CAUSE,
            reason_category=ReasonCategory.UNDETERMINED,
            evidence=[],
            confidence=0.9,
        )


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_confidence_range(confidence: float) -> None:
    with pytest.raises(ValidationError):
        make_result(confidence=confidence, proposed_fix=None)


def test_negative_tokens_rejected() -> None:
    with pytest.raises(ValidationError):
        make_result(tokens_used=-1)


def test_roundtrip_json() -> None:
    r = make_result()
    assert InvestigationResult.model_validate_json(r.model_dump_json()) == r


@given(confidence=st.floats(min_value=0.0, max_value=1.0), has_fix=st.booleans())
def test_invariant_never_violated(confidence: float, has_fix: bool) -> None:
    """Any accepted result satisfies the confidence<->fix invariant."""
    try:
        r = make_result(
            confidence=confidence, proposed_fix=make_result().proposed_fix if has_fix else None
        )
    except ValidationError:
        assert has_fix and confidence < 0.2
        return
    assert not (r.confidence < 0.2 and r.proposed_fix is not None)
