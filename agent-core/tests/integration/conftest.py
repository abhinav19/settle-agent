"""Postgres via testcontainers; schema built by running the real Alembic migrations."""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from testcontainers.community.postgres import PostgresContainer

from alembic import command
from settleagent.persistence.db import make_engine, make_session_factory

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    with PostgresContainer("postgres:16", driver="psycopg") as pg:
        url = pg.get_connection_url()
        previous = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = url
        try:
            cfg = Config(str(ROOT / "alembic.ini"))
            cfg.set_main_option("script_location", str(ROOT / "alembic"))
            command.upgrade(cfg, "head")
            yield url
        finally:
            if previous is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous


@pytest.fixture
async def session(database_url: str) -> AsyncIterator[AsyncSession]:
    engine = make_engine(database_url)
    factory = make_session_factory(engine)
    async with factory() as s:
        yield s
        await s.rollback()
    await engine.dispose()


@pytest.fixture
async def sessions(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(database_url)
    yield make_session_factory(engine)
    await engine.dispose()


@pytest.fixture(scope="session")
def kafka_bootstrap() -> Iterator[str]:
    from testcontainers.community.kafka import KafkaContainer

    with KafkaContainer().with_kraft() as kafka:
        yield kafka.get_bootstrap_server()


@pytest.fixture
async def isolated_sessions(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A brand-new, fully migrated database inside the test Postgres.

    For tests that must not see (or recover!) cases left behind by other tests, such as load
    tests: the consumer re-dispatches every incomplete case in its database on startup.
    """
    import uuid

    import psycopg
    from sqlalchemy.engine import make_url

    name = f"iso_{uuid.uuid4().hex[:10]}"
    admin = make_url(database_url)
    with psycopg.connect(
        admin.set(drivername="postgresql").render_as_string(hide_password=False), autocommit=True
    ) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = admin.set(database=name).render_as_string(hide_password=False)

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "alembic"))
        command.upgrade(cfg, "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous

    engine = make_engine(url)
    yield make_session_factory(engine)
    await engine.dispose()
    with psycopg.connect(
        admin.set(drivername="postgresql").render_as_string(hide_password=False), autocommit=True
    ) as conn:
        conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
