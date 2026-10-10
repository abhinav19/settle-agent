"""Cross-project contract: generated synthetic events must satisfy the Kafka event schema."""

import json
from pathlib import Path
from typing import Any, cast

import pytest

from settleagent.domain.events import SettlementFailureEvent
from settleagent.domain.reason_codes import CATALOG, ReasonCategory

DATASET = Path(__file__).resolve().parents[3] / "data" / "synthetic" / "dataset.json"

pytestmark = pytest.mark.skipif(not DATASET.exists(), reason="run `make seed` first")


def _scenarios() -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], json.loads(DATASET.read_text())["scenarios"])


def test_every_scenario_event_parses_as_settlement_failure_event() -> None:
    scenarios = _scenarios()
    assert len(scenarios) == 300
    for s in scenarios:
        SettlementFailureEvent.model_validate_json(json.dumps(s["event"]))


def test_ground_truth_categories_match_the_reason_code_catalog() -> None:
    for s in _scenarios():
        if s["difficulty"] == "clear":
            code = SettlementFailureEvent.model_validate_json(json.dumps(s["event"])).reason_code
            assert s["expected_category"] == CATALOG[code].category.value
        if s["difficulty"] == "underdetermined":
            assert s["expected_category"] == ReasonCategory.UNDETERMINED.value
