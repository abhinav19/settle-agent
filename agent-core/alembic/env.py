"""Alembic environment. Reads DATABASE_URL from the environment (fails fast if absent)."""

import os
from logging.config import fileConfig

from sqlalchemy import create_engine

from alembic import context
from settleagent.persistence.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    try:
        return os.environ["DATABASE_URL"]
    except KeyError as exc:
        raise RuntimeError("DATABASE_URL must be set to run migrations") from exc


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
