"""The consumer's half of the cross-language contract: parse what the JAVA producer emits.

`contracts/java-events.golden.jsonl` is written by the Java test-suite (EventContractTest). Here the
real strict Pydantic model — the one the Kafka consumer uses — must accept every line, agree with
Java on the exact wire representation, and find the referenced trades in the MCP reference data.
"""

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from _shared.store import load_dataset as load_reference
from holiday_calendar.service import HolidayCalendar
from pydantic import ValidationError
from security_master.service import SecurityMaster
from ssi_store.service import SsiStore
from trade_store.service import TradeStore

from settleagent.consumer.messages import PoisonMessageError, parse_event
from settleagent.domain.events import SettlementFailureEvent
from settleagent.domain.reason_codes import ReasonCode

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
GOLDEN = CONTRACTS / "java-events.golden.jsonl"
SCHEMA = json.loads((CONTRACTS / "settlement_failure_event.schema.json").read_text())
LINES = GOLDEN.read_text(encoding="utf-8").splitlines()
REFERENCE = load_reference()
FIXTURE_IDS = {s.event["event_id"] for s in REFERENCE.scenarios}


def test_the_golden_file_has_replayed_and_streamed_events() -> None:
    assert len(LINES) == 50
    ids = [json.loads(line)["event_id"] for line in LINES]
    assert len(set(ids)) == 50
    assert all(i in FIXTURE_IDS for i in ids[:25])  # replay keeps the fixtures' ids
    assert not any(i in FIXTURE_IDS for i in ids[25:])  # stream mints new ones


@pytest.mark.parametrize("index", range(50))
def test_every_java_event_is_accepted_by_the_real_consumer_parser(index: int) -> None:
    """parse_event is exactly what the Kafka consumer calls: no coercion, no extras allowed."""
    event = parse_event(LINES[index].encode())
    assert isinstance(event, SettlementFailureEvent) and event.schema_version == "1.0"


def test_java_and_python_agree_on_the_exact_wire_representation() -> None:
    """Parse with Pydantic and re-serialise: it must equal Java's JSON. Catches format drift in
    decimals, timestamps, UUIDs and dates that mere parsing would silently tolerate."""
    for line in LINES:
        python_view = json.loads(SettlementFailureEvent.model_validate_json(line).model_dump_json())
        assert python_view == json.loads(line)


def test_java_output_also_satisfies_the_published_json_schema() -> None:
    validator = jsonschema.Draft202012Validator(SCHEMA, format_checker=jsonschema.FormatChecker())
    for line in LINES:
        assert list(validator.iter_errors(json.loads(line))) == []


def test_the_schema_is_not_vacuous() -> None:
    validator = jsonschema.Draft202012Validator(SCHEMA, format_checker=jsonschema.FormatChecker())
    good: dict[str, Any] = json.loads(LINES[0])
    for mutation in (
        {"surprise": 1},
        {"schema_version": "2.0"},
        {"reason_code": "NOPE"},
        {"event_id": "not-a-uuid-at-all-but-36-characters"},
        {"isin": "us0378331005"},
        {"quantity": -5},
    ):
        assert list(validator.iter_errors({**good, **mutation})), mutation


def test_the_consumer_rejects_a_corrupted_java_event() -> None:
    good = json.loads(LINES[0])
    for field, bad in (("isin", "US0378331006"), ("quantity", "0"), ("currency", "usd")):
        with pytest.raises(PoisonMessageError):
            parse_event(json.dumps({**good, field: bad}).encode())
    with pytest.raises(PoisonMessageError, match="(?i)extra"):
        parse_event(json.dumps({**good, "surprise": 1}).encode())
    with pytest.raises(ValidationError):  # and naive/zone-less timestamps never slip through
        SettlementFailureEvent.model_validate_json(
            json.dumps({**good, "emitted_at": "2026-10-10T12:00:00"})
        )


def test_the_reason_codes_java_emits_are_the_codes_python_knows() -> None:
    emitted = {json.loads(line)["reason_code"] for line in LINES}
    assert emitted <= {rc.value for rc in ReasonCode} and len(emitted) >= 5


def test_java_events_reference_data_the_mcp_servers_can_resolve() -> None:
    """The point of sampling fixtures: every trade, instrument and SSI the producer mentions
    exists in the synthetic reference data, so the agent's tool calls find something."""
    trades, instruments = TradeStore(REFERENCE), SecurityMaster(REFERENCE)
    ssi, _ = SsiStore(REFERENCE), HolidayCalendar(REFERENCE)
    absent = {s.event["trade_id"] for s in REFERENCE.scenarios if "absent" in s.note}
    for line in LINES:
        event = parse_event(line.encode())
        instruments.get_instrument(event.isin)
        ssi.get_ssi(event.counterparty_bic, event.currency)
        if event.trade_id not in absent:  # a few fixtures deliberately have no trade record
            assert trades.get_trade(event.trade_id).isin == event.isin
