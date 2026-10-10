"""Per-message intake: validate -> (DLQ | persist case). Returns what happened."""

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from settleagent.consumer.dlq import DlqPublisher
from settleagent.consumer.messages import IncomingMessage, PoisonMessageError, parse_event
from settleagent.domain.case import Case
from settleagent.domain.events import SettlementFailureEvent

log = logging.getLogger(__name__)


class IntakeKind(StrEnum):
    CREATED = "created"
    DUPLICATE = "duplicate"
    POISON = "poison"


@dataclass(frozen=True)
class IntakeResult:
    kind: IntakeKind
    case_id: UUID | None = None


class Intake(Protocol):
    async def intake(self, event: SettlementFailureEvent) -> tuple[Case, bool]: ...


class EventIntake:
    def __init__(self, processor: Intake, dlq: DlqPublisher) -> None:
        self._processor = processor
        self._dlq = dlq

    async def handle(self, msg: IncomingMessage) -> IntakeResult:
        """Poison -> DLQ (published before returning, so the caller may then commit).

        Any other exception propagates: it is treated as transient by the consumer.
        """
        try:
            event = parse_event(msg.value)
        except PoisonMessageError as exc:
            log.warning("poison message %s[%d]@%d: %s", msg.topic, msg.partition, msg.offset, exc)
            await self._dlq.publish(msg, str(exc))
            return IntakeResult(IntakeKind.POISON)
        case, created = await self._processor.intake(event)
        return IntakeResult(IntakeKind.CREATED if created else IntakeKind.DUPLICATE, case.id)
