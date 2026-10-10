"""Dataset persistence: deterministic JSON on disk, loaded read-only into indexed lookups."""

import json
import os
from pathlib import Path

from _shared.models import Dataset

DATA_DIR_ENV = "SETTLE_DATA_DIR"
DATASET_FILE = "dataset.json"


def default_data_dir() -> Path:
    env = os.environ.get(DATA_DIR_ENV)
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "data" / "synthetic"


def save_dataset(dataset: Dataset, directory: Path) -> Path:
    """Write the dataset with stable key order so regeneration is byte-identical."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / DATASET_FILE
    payload = dataset.model_dump(mode="json")
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_dataset(directory: Path | None = None) -> Dataset:
    path = (directory or default_data_dir()) / DATASET_FILE
    if not path.exists():
        raise FileNotFoundError(f"{path} not found; run `make seed`")
    return Dataset.model_validate_json(path.read_text(encoding="utf-8"))
