import pytest

from _shared.generators import generate_dataset
from _shared.models import Dataset


@pytest.fixture(scope="session")
def dataset() -> Dataset:
    return generate_dataset(seed=42, n_scenarios=300)
