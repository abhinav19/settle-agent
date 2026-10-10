"""The Kafka message contract, as a committed JSON Schema both languages are tested against.

`contracts/settlement_failure_event.schema.json` is GENERATED from the Pydantic model (the source
of truth). This test fails if the model changes without the schema being regenerated, so the Java
producer's schema validation can never silently go stale. Regenerate: UPDATE_SNAPSHOTS=1 pytest.
"""

import json
import os
from pathlib import Path

import pytest

from settleagent.domain.events import SettlementFailureEvent
from settleagent.domain.reason_codes import ReasonCode

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
SCHEMA = CONTRACTS / "settlement_failure_event.schema.json"


def current_schema() -> dict[str, object]:
    return SettlementFailureEvent.model_json_schema(mode="validation")


def test_committed_schema_matches_the_pydantic_model() -> None:
    schema = current_schema()
    if os.environ.get("UPDATE_SNAPSHOTS") or not SCHEMA.exists():
        CONTRACTS.mkdir(exist_ok=True)
        SCHEMA.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert json.loads(SCHEMA.read_text(encoding="utf-8")) == schema, (
        "SettlementFailureEvent changed: regenerate contracts/ with UPDATE_SNAPSHOTS=1, "
        "update the Java record, and bump schema_version if the change is breaking"
    )


def test_schema_pins_the_contract_details_the_producer_relies_on() -> None:
    schema = current_schema()
    props = schema["properties"]
    assert isinstance(props, dict)
    assert set(props) == {
        "schema_version",
        "event_id",
        "emitted_at",
        "trade_id",
        "isin",
        "settlement_date",
        "counterparty_bic",
        "reason_code",
        "quantity",
        "cash_amount",
        "currency",
        "raw_message",
    }
    assert (
        schema["additionalProperties"] is False
    )  # reject-on-extra: the producer may not add fields
    assert set(schema["required"]) == set(props)  # type: ignore[call-overload]
    assert props["schema_version"] == {"const": "1.0", "title": "Schema Version", "type": "string"}


def test_the_reason_code_list_is_the_single_vocabulary() -> None:
    """The Java ReasonCodeCatalog must offer exactly these codes (checked in JUnit as well)."""
    expected = sorted(rc.value for rc in ReasonCode)
    assert expected == ["CALD", "CMIS", "DENO", "LACK", "LATE", "MONY", "NMAT", "SSIM"]
    defs = current_schema()["$defs"]
    assert isinstance(defs, dict)
    assert sorted(defs["ReasonCode"]["enum"]) == expected


@pytest.mark.parametrize("field", ["event_id", "emitted_at", "settlement_date"])
def test_wire_formats_are_declared(field: str) -> None:
    props = current_schema()["properties"]
    assert isinstance(props, dict)
    assert props[field]["format"] in {"uuid", "date-time", "date"}
