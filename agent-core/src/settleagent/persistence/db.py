"""Async engine and session factory. Callers own the transaction (unit of work)."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def make_engine(database_url: str) -> AsyncEngine:
    """Create the async engine (``postgresql+psycopg://`` URL)."""
    return create_async_engine(database_url, pool_pre_ping=True)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory; ``expire_on_commit=False`` so rows stay readable after commit."""
    return async_sessionmaker(engine, expire_on_commit=False)
