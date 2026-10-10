"""The versioned 300-case dataset: generation, integrity checks and loading."""

import hashlib
import json
from collections import Counter
from pathlib import Path

from _shared.models import Dataset as ReferenceData
from _shared.store import DATASET_FILE, default_data_dir

from evalharness.models import EvalCase, Manifest

DATASET_VERSION = "1"
"""Bump when labels, the mix, or the reference data change in a way that moves scores."""
DEFAULT_DIR = Path(__file__).resolve().parents[1] / "dataset" / f"v{DATASET_VERSION}"
CASES_FILE = "cases.jsonl"
MANIFEST_FILE = "manifest.json"


class DatasetIntegrityError(RuntimeError):
    """The cases on disk do not match their manifest or the reference data."""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reference_data_hash(reference_dir: Path | None = None) -> str:
    return sha256_file((reference_dir or default_data_dir()) / DATASET_FILE)


def cases_from_reference(reference: ReferenceData) -> list[EvalCase]:
    return [
        EvalCase(
            case_id=s.scenario_id,
            difficulty=s.difficulty,
            event=dict(s.event),
            expected_category=s.expected_category,
            secondary_category=s.secondary_category,
            expected_fix_action=s.expected_fix_action,
            expected_requires_write=s.expected_requires_write,
            auto_resolvable=s.auto_resolvable,
            note=s.note,
        )
        for s in reference.scenarios
    ]


def _serialise(cases: list[EvalCase]) -> bytes:
    lines = (json.dumps(c.model_dump(mode="json"), sort_keys=True) for c in cases)
    return ("\n".join(lines) + "\n").encode()


def write_dataset(
    reference: ReferenceData, out_dir: Path = DEFAULT_DIR, reference_dir: Path | None = None
) -> Manifest:
    """Write cases.jsonl + manifest.json deterministically (byte-identical on regeneration)."""
    cases = cases_from_reference(reference)
    payload = _serialise(cases)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / CASES_FILE).write_bytes(payload)
    manifest = Manifest(
        dataset_version=DATASET_VERSION,
        seed=reference.seed,
        n_cases=len(cases),
        mix=dict(Counter(c.difficulty for c in cases)),
        reason_codes=sorted({c.reason_code for c in cases}),
        reference_data_sha256=reference_data_hash(reference_dir),
        cases_sha256=hashlib.sha256(payload).hexdigest(),
    )
    (out_dir / MANIFEST_FILE).write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=1, sort_keys=True) + "\n"
    )
    return manifest


def load_dataset(
    directory: Path = DEFAULT_DIR, reference_dir: Path | None = None
) -> tuple[Manifest, list[EvalCase]]:
    """Load and VERIFY: file hash, case count, mix, and that the reference data is unchanged."""
    cases_path, manifest_path = directory / CASES_FILE, directory / MANIFEST_FILE
    if not cases_path.exists() or not manifest_path.exists():
        raise DatasetIntegrityError(f"{directory} has no dataset; run `make eval-dataset`")
    manifest = Manifest.model_validate_json(manifest_path.read_text())
    raw = cases_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest.cases_sha256:
        raise DatasetIntegrityError("cases.jsonl was modified after the manifest was written")
    cases = [EvalCase.model_validate_json(line) for line in raw.decode().splitlines() if line]
    if len(cases) != manifest.n_cases or dict(Counter(c.difficulty for c in cases)) != manifest.mix:
        raise DatasetIntegrityError("case count or difficulty mix disagrees with the manifest")
    if len({c.case_id for c in cases}) != len(cases):
        raise DatasetIntegrityError("duplicate case ids")
    if reference_data_hash(reference_dir) != manifest.reference_data_sha256:
        raise DatasetIntegrityError(
            "the synthetic reference data changed since the labels were generated; "
            "regenerate the dataset (and bump DATASET_VERSION if scores will move)"
        )
    return manifest, cases
