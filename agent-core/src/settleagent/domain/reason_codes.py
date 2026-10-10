"""Reason codes: the single source of truth for code -> category -> playbook.

The seed set is a representative subset modeled on ISO 20022 ``semt.018`` /
SWIFT MT548 failing/pending reasons. It MUST be reconciled against the official
ISO 20022 external code lists (``FailingReason1Code`` / ``PendingReason*Code``)
before any use beyond this synthetic reference implementation.
"""

from enum import StrEnum
from types import MappingProxyType
from typing import Final

from settleagent.domain.base import DomainModel


class ReasonCode(StrEnum):
    """Failure reason carried on the settlement status message."""

    LACK = "LACK"  # insufficient securities
    MONY = "MONY"  # insufficient cash
    NMAT = "NMAT"  # unmatched / mismatch
    CMIS = "CMIS"  # counterparty instruction missing
    SSIM = "SSIM"  # SSI mismatch
    DENO = "DENO"  # denomination / quantity issue
    LATE = "LATE"  # late instruction
    CALD = "CALD"  # settlement date on market holiday


class ReasonCategory(StrEnum):
    """Deterministic grouping of reason codes (and the agent's conclusion)."""

    INVENTORY = "INVENTORY"
    FUNDING = "FUNDING"
    MATCHING = "MATCHING"
    COUNTERPARTY = "COUNTERPARTY"
    STATIC_DATA = "STATIC_DATA"
    QUANTITY = "QUANTITY"
    TIMING = "TIMING"
    CALENDAR = "CALENDAR"
    UNDETERMINED = "UNDETERMINED"


class ToolName(StrEnum):
    """The four read-only MCP tool servers."""

    SECURITY_MASTER = "security-master"
    SSI_STORE = "ssi-store"
    HOLIDAY_CALENDAR = "holiday-calendar"
    TRADE_STORE = "trade-store"


class ReasonCodeInfo(DomainModel):
    """Catalog entry: meaning, category and the investigation playbook."""

    code: ReasonCode
    meaning: str
    category: ReasonCategory
    typical_cause: str
    playbook: tuple[ToolName, ...]


_ENTRIES = (
    ReasonCodeInfo(
        code=ReasonCode.LACK,
        meaning="Insufficient securities",
        category=ReasonCategory.INVENTORY,
        typical_cause="Short inventory or failed upstream receive",
        playbook=(ToolName.TRADE_STORE, ToolName.SECURITY_MASTER),
    ),
    ReasonCodeInfo(
        code=ReasonCode.MONY,
        meaning="Insufficient cash",
        category=ReasonCategory.FUNDING,
        typical_cause="Funding shortfall",
        playbook=(ToolName.TRADE_STORE,),
    ),
    ReasonCodeInfo(
        code=ReasonCode.NMAT,
        meaning="Unmatched / economic detail mismatch",
        category=ReasonCategory.MATCHING,
        typical_cause="Economic detail mismatch vs counterparty",
        playbook=(ToolName.TRADE_STORE, ToolName.SECURITY_MASTER),
    ),
    ReasonCodeInfo(
        code=ReasonCode.CMIS,
        meaning="Counterparty instruction missing",
        category=ReasonCategory.COUNTERPARTY,
        typical_cause="Counterparty has not instructed",
        playbook=(ToolName.TRADE_STORE,),
    ),
    ReasonCodeInfo(
        code=ReasonCode.SSIM,
        meaning="Standing settlement instruction mismatch",
        category=ReasonCategory.STATIC_DATA,
        typical_cause="Wrong or stale standing settlement instruction",
        playbook=(ToolName.SSI_STORE, ToolName.TRADE_STORE),
    ),
    ReasonCodeInfo(
        code=ReasonCode.DENO,
        meaning="Denomination / quantity issue",
        category=ReasonCategory.QUANTITY,
        typical_cause="Lot-size or denomination break",
        playbook=(ToolName.SECURITY_MASTER, ToolName.TRADE_STORE),
    ),
    ReasonCodeInfo(
        code=ReasonCode.LATE,
        meaning="Late instruction",
        category=ReasonCategory.TIMING,
        typical_cause="Instructed after cut-off",
        playbook=(ToolName.TRADE_STORE, ToolName.HOLIDAY_CALENDAR),
    ),
    ReasonCodeInfo(
        code=ReasonCode.CALD,
        meaning="Settlement date on market holiday",
        category=ReasonCategory.CALENDAR,
        typical_cause="Holiday / calendar error",
        playbook=(ToolName.HOLIDAY_CALENDAR, ToolName.TRADE_STORE),
    ),
)

CATALOG: Final = MappingProxyType({e.code: e for e in _ENTRIES})
"""Read-only mapping of every ReasonCode to its catalog entry."""


def info(code: ReasonCode) -> ReasonCodeInfo:
    """Return the catalog entry for a code."""
    return CATALOG[code]
