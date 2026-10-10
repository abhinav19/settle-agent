import json
from typing import Any

import pytest
from pydantic import ValidationError

from settleagent.domain.events import SettlementFailureEvent, isin_checksum_ok
from tests.factories import event_payload


def parse(**overrides: Any) -> SettlementFailureEvent:
    return SettlementFailureEvent.model_validate_json(json.dumps(event_payload(**overrides)))


def test_valid_event_parses() -> None:
    ev = parse()
    assert ev.reason_code == "SSIM"
    assert str(ev.quantity) == "1000"


def test_event_is_frozen() -> None:
    ev = parse()
    with pytest.raises(ValidationError):
        ev.trade_id = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema_version": "2.0"},
        {"event_id": "not-a-uuid"},
        {"emitted_at": "2026-10-10T12:00:00"},  # naive datetime rejected
        {"isin": "US0378331006"},  # bad check digit
        {"isin": "us0378331005"},  # lowercase
        {"counterparty_bic": "BAD"},
        {"reason_code": "XXXX"},
        {"quantity": "0"},
        {"quantity": "-5"},
        {"cash_amount": "-1"},
        {"currency": "usd"},
        {"trade_id": ""},
        {"raw_message": ""},
    ],
)
def test_invalid_payload_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse(**overrides)


def test_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        parse(unexpected="x")


def test_missing_field_rejected() -> None:
    payload = event_payload()
    del payload["trade_id"]
    with pytest.raises(ValidationError):
        SettlementFailureEvent.model_validate_json(json.dumps(payload))


def test_no_silent_coercion_from_python_types() -> None:
    with pytest.raises(ValidationError):
        SettlementFailureEvent.model_validate(event_payload(quantity=1000))


def test_isin_checksum() -> None:
    assert isin_checksum_ok("US0378331005")
    assert isin_checksum_ok("GB0002634946")
    assert not isin_checksum_ok("US0378331004")
