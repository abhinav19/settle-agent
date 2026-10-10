"""The real readiness probes against live Postgres and Kafka (and against dead endpoints)."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from settleagent.api.readiness import database_check, kafka_check, run_checks
from settleagent.persistence.db import make_engine, make_session_factory

pytestmark = pytest.mark.integration


async def test_database_probe_up(sessions: async_sessionmaker[AsyncSession]) -> None:
    result = (await run_checks({"database": database_check(sessions)}))["database"]
    assert result.ok and result.latency_ms >= 0


async def test_database_probe_down() -> None:
    engine = make_engine("postgresql+psycopg://x:y@127.0.0.1:1/none?connect_timeout=1")
    result = (await run_checks({"database": database_check(make_session_factory(engine))}, 4))[
        "database"
    ]
    await engine.dispose()
    assert not result.ok and result.detail


async def test_kafka_probe_up(kafka_bootstrap: str) -> None:
    result = (await run_checks({"kafka": kafka_check(kafka_bootstrap)}, timeout_s=10))["kafka"]
    assert result.ok and "broker" in result.detail


async def test_kafka_probe_down() -> None:
    result = (await run_checks({"kafka": kafka_check("127.0.0.1:1", timeout_s=1)}, timeout_s=5))[
        "kafka"
    ]
    assert not result.ok
