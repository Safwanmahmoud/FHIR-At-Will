"""Alembic environment.

The database URL comes from ``DATABASE_URL`` only. Alembic's own
``sqlalchemy.url`` is left empty so a stale ini value can never send a migration
at the wrong database.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from fhirbridge.storage.base import Base
from fhirbridge.storage.models import (  # noqa: F401 - imported so metadata is populated
    ApiKey,
    AuditEvent,
    IdempotencyKey,
    Policy,
    Tenant,
    User,
)

config = context.config

if config.config_file_name is not None:
    # `disable_existing_loggers=False` is not a nicety. The default is True, and
    # it silently switches off every logger already configured — which, whenever
    # Alembic is driven in-process (a migrate-then-serve entrypoint, a test
    # session), means the application's own logging goes quiet and nobody
    # notices until the incident when there are no logs.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit(
            "DATABASE_URL is not set. Alembic reads it from the environment so that "
            "migrations always target the same database as the application."
        )
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    engine = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
