import pytest

from settleagent.domain.case import ALLOWED_TRANSITIONS, CaseStatus
from settleagent.domain.reason_codes import CATALOG, ReasonCategory, ReasonCode, info


def test_every_code_has_category_and_playbook() -> None:
    assert set(CATALOG) == set(ReasonCode)
    for code in ReasonCode:
        entry = info(code)
        assert entry.code is code
        assert entry.category is not ReasonCategory.UNDETERMINED
        assert len(entry.playbook) >= 1


def test_catalog_is_read_only() -> None:
    with pytest.raises(TypeError):
        CATALOG[ReasonCode.LACK] = info(ReasonCode.MONY)  # type: ignore[index]


def test_every_status_has_transition_entry() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(CaseStatus)


@pytest.mark.parametrize(
    "terminal", [CaseStatus.AUTO_RESOLVED, CaseStatus.ESCALATED, CaseStatus.FAILED]
)
def test_terminal_states_have_no_exits(terminal: CaseStatus) -> None:
    assert ALLOWED_TRANSITIONS[terminal] == frozenset()


def test_no_state_skips_investigation() -> None:
    assert CaseStatus.AWAITING_APPROVAL not in ALLOWED_TRANSITIONS[CaseStatus.RECEIVED]
    assert CaseStatus.AUTO_RESOLVED not in ALLOWED_TRANSITIONS[CaseStatus.RECEIVED]


def test_resolved_can_only_be_reopened_to_escalated() -> None:
    """The sole exit from RESOLVED is a reversal of the remediation."""
    assert ALLOWED_TRANSITIONS[CaseStatus.RESOLVED] == frozenset({CaseStatus.ESCALATED})


def test_only_awaiting_approval_can_become_resolved() -> None:
    sources = {s for s, targets in ALLOWED_TRANSITIONS.items() if CaseStatus.RESOLVED in targets}
    assert sources == {CaseStatus.AWAITING_APPROVAL}
