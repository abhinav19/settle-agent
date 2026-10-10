"""Routing policy: versioned, config-driven thresholds for the confidence gate."""

from typing import Annotated

from pydantic import Field

from settleagent.domain.base import DomainModel
from settleagent.domain.investigation import FixAction


class Policy(DomainModel):
    """What the gate needs to decide. Chosen from the eval precision/recall curve."""

    version: Annotated[str, Field(min_length=1)]
    auto_threshold: Annotated[float, Field(ge=0.0, le=1.0)]
    auto_eligible_actions: frozenset[FixAction]


DEFAULT_POLICY_VERSION = "1"


def default_policy(auto_threshold: float) -> Policy:
    """Every action is auto-eligible; writes are still approval-gated downstream."""
    return Policy(
        version=DEFAULT_POLICY_VERSION,
        auto_threshold=auto_threshold,
        auto_eligible_actions=frozenset(FixAction),
    )
