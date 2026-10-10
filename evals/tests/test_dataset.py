"""The 300-case dataset: integrity, coverage, and label consistency."""

import json
import shutil
from collections import Counter
from pathlib import Path

import pytest
from _shared.store import DATASET_FILE, default_data_dir
from _shared.store import load_dataset as load_reference
from settleagent.domain.investigation import ACTION_REQUIRES_WRITE, FixAction
from settleagent.domain.reason_codes import CATALOG, ReasonCode

from evalharness.dataset import (
    CASES_FILE,
    DEFAULT_DIR,
    MANIFEST_FILE,
    DatasetIntegrityError,
    load_dataset,
    write_dataset,
)

MANIFEST, CASES = load_dataset()


def test_the_committed_dataset_verifies() -> None:
    assert MANIFEST.n_cases == 300 == len(CASES) and MANIFEST.dataset_version == "1"
    assert MANIFEST.mix == {"clear": 180, "ambiguous": 75, "underdetermined": 45}


def test_regenerating_reproduces_the_committed_files_byte_for_byte(tmp_path: Path) -> None:
    write_dataset(load_reference(), tmp_path)
    for name in (CASES_FILE, MANIFEST_FILE):
        assert (tmp_path / name).read_bytes() == (DEFAULT_DIR / name).read_bytes(), name


def test_every_reason_code_appears_in_every_difficulty_tier() -> None:
    for tier in ("clear", "ambiguous", "underdetermined"):
        codes = {c.reason_code for c in CASES if c.difficulty == tier}
        assert codes == {rc.value for rc in ReasonCode}, tier


def test_labels_are_internally_consistent() -> None:
    for c in CASES:
        clear = c.difficulty == "clear"
        assert c.auto_resolvable == clear, c.case_id
        assert (c.expected_fix_action is not None) == clear, c.case_id
        assert (c.expected_requires_write is not None) == clear, c.case_id
        assert (c.expected_category == "UNDETERMINED") == (c.difficulty == "underdetermined")
        if clear:
            assert c.expected_category == CATALOG[ReasonCode(c.reason_code)].category.value
            assert (
                c.expected_requires_write
                == ACTION_REQUIRES_WRITE[FixAction(c.expected_fix_action or "")]
            )
        if c.difficulty == "ambiguous":
            assert c.secondary_category and c.secondary_category != c.expected_category


def test_case_ids_are_unique_and_events_are_valid_contracts() -> None:
    from settleagent.domain.events import SettlementFailureEvent

    assert len({c.case_id for c in CASES}) == 300
    assert len({c.event["event_id"] for c in CASES}) == 300
    for c in CASES:
        SettlementFailureEvent.model_validate_json(json.dumps(c.event))


def test_the_mix_is_balanced_enough_to_measure_each_code() -> None:
    per_code = Counter(c.reason_code for c in CASES if c.auto_resolvable)
    assert min(per_code.values()) >= 15  # no code is a rounding error in the headline numbers


# ------------------------------------------------------------- tamper detection
@pytest.fixture
def copy_of_dataset(tmp_path: Path) -> Path:
    shutil.copytree(DEFAULT_DIR, tmp_path / "v1")
    return tmp_path / "v1"


def test_editing_a_label_after_the_fact_is_detected(copy_of_dataset: Path) -> None:
    path = copy_of_dataset / CASES_FILE
    path.write_text(
        path.read_text().replace('"auto_resolvable": true', '"auto_resolvable": false', 1)
    )
    with pytest.raises(DatasetIntegrityError, match="modified after the manifest"):
        load_dataset(copy_of_dataset)


def test_a_manifest_that_disagrees_with_the_cases_is_detected(copy_of_dataset: Path) -> None:
    path = copy_of_dataset / MANIFEST_FILE
    manifest = json.loads(path.read_text())
    manifest["mix"]["clear"] = 179
    path.write_text(json.dumps(manifest))
    with pytest.raises(DatasetIntegrityError, match="mix"):
        load_dataset(copy_of_dataset)


def test_changed_reference_data_invalidates_the_labels(tmp_path: Path) -> None:
    ref = tmp_path / "ref"
    ref.mkdir()
    shutil.copy(default_data_dir() / DATASET_FILE, ref / DATASET_FILE)
    assert load_dataset(reference_dir=ref)  # same bytes: fine
    (ref / DATASET_FILE).write_text((ref / DATASET_FILE).read_text() + " ")
    with pytest.raises(DatasetIntegrityError, match="reference data changed"):
        load_dataset(reference_dir=ref)


def test_a_missing_dataset_says_how_to_create_it(tmp_path: Path) -> None:
    with pytest.raises(DatasetIntegrityError, match="make eval-dataset"):
        load_dataset(tmp_path)
