"""Wire-level message handling: parse and validate; reject (never coerce) malformed input."""

from dataclasses import dataclass

from pydantic import ValidationError

from settleagent.domain.events import SettlementFailureEvent


@dataclass(frozen=True)
class IncomingMessage:
    """A Kafka record, decoupled from the client library."""

    topic: str
    partition: int
    offset: int
    key: bytes | None
    value: bytes | None


class PoisonMessageError(ValueError):
    """The message can never be processed (invalid JSON or schema). Goes to the DLQ."""


def parse_event(value: bytes | None) -> SettlementFailureEvent:
    """Validate a body as ``SettlementFailureEvent`` v1.0 or raise ``PoisonMessageError``."""
    if not value:
        raise PoisonMessageError("empty message body")
    try:
        return SettlementFailureEvent.model_validate_json(value)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()
        )
        raise PoisonMessageError(f"schema validation failed: {details}"[:2000]) from exc
