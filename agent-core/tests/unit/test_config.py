import pytest
from pydantic import ValidationError

from settleagent.config import Settings


def _env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    base = {
        "KAFKA_BOOTSTRAP_SERVERS": "localhost:9092",
        "DATABASE_URL": "postgresql+psycopg://u:p@localhost/db",
        "API_KEY": "0123456789abcdef",
    }
    base.update({k.upper(): v for k, v in overrides.items()})
    for k, v in base.items():
        monkeypatch.setenv(k, v)


def test_loads_valid_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    s = Settings(_env_file=None)
    assert s.max_steps == 12
    assert s.auto_threshold == 0.9


def test_fails_fast_on_missing_required(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("KAFKA_BOOTSTRAP_SERVERS", "DATABASE_URL", "API_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_rejects_out_of_range_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, auto_threshold="1.5")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
