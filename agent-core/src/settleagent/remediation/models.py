"""Remediation value types and errors."""

from datetime import datetime
from uuid import UUID

from settleagent.domain.base import DomainModel
from settleagent.domain.case import Approval, Case
from settleagent.domain.investigation import FixAction


class FixNotExecutableError(ValueError):
    """The proposed fix cannot be safely derived from the case's own evidence."""


class CaseStateError(RuntimeError):
    """The case is not in a state that allows the requested operation."""


class SettlementSystemError(RuntimeError):
    """The (synthetic) settlement system could not apply the action.

    ``transient`` errors may be retried by calling the operation again; permanent ones escalate.
    """

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


class ExecutableFix(DomainModel):
    """A write action fully derived and verified from evidence — safe to hand to the system.

    ``parameters`` are what is applied; ``inverse`` is what undoes it.
    """

    case_id: UUID
    trade_id: str
    action: FixAction
    parameters: dict[str, str]
    inverse: dict[str, str]


class ExecutionRecord(DomainModel):
    """One applied (and possibly reversed) action in the settlement system's ledger."""

    idempotency_key: str
    case_id: UUID
    action: FixAction
    parameters: dict[str, str]
    inverse: dict[str, str]
    executed_at: datetime
    reversed_at: datetime | None = None
    created: bool = True
    """False when an idempotent replay found the action already applied."""


class RemediationOutcome(DomainModel):
    """Result of an approve / reject / reverse call."""

    case: Case
    approval: Approval
    changed: bool
    """False when the call was an idempotent replay (nothing new happened)."""
