"""Kafka message contract: ``SettlementFailureEvent`` v1.0."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator

from settleagent.domain.base import DomainModel
from settleagent.domain.reason_codes import ReasonCode

_ISIN_LEN = 12


def isin_checksum_ok(isin: str) -> bool:
    """Validate an ISIN's Luhn check digit (letters expand to two digits)."""
    digits = "".join(str(int(c, 36)) for c in isin)
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            n -= 9 if n > 9 else 0
        total += n
    return total % 10 == 0


class SettlementFailureEvent(DomainModel):
    """A settlement-failure status message (modeled on semt.018 / MT548)."""

    schema_version: Literal["1.0"]
    event_id: UUID
    emitted_at: AwareDatetime
    trade_id: Annotated[str, Field(min_length=1, max_length=64)]
    isin: Annotated[str, Field(pattern=r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")]
    settlement_date: date
    counterparty_bic: Annotated[str, Field(pattern=r"^[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?$")]
    reason_code: ReasonCode
    quantity: Annotated[Decimal, Field(gt=0)]
    cash_amount: Annotated[Decimal, Field(ge=0)]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    raw_message: Annotated[str, Field(min_length=1)]

    @field_validator("isin")
    @classmethod
    def _isin_checksum(cls, v: str) -> str:
        if not isin_checksum_ok(v):
            raise ValueError("invalid ISIN check digit")
        return v
