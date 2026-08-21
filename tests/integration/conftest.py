"""Integration fixtures: a real Postgres 16, migrated by Alembic.

Why a real database rather than SQLite: the two properties these tests exist to
prove — row-level security and the append-only trigger — do not exist outside
Postgres. Asserting them against a substitute would be worse than not asserting
them, because the suite would go green while the protection was absent.

**The connection role is part of what is under test.** Postgres ignores every RLS
policy for a superuser or a ``BYPASSRLS`` role, and the default role in the
official image is both. Running these tests as that role would make every
isolation assertion vacuous, so the fixtures migrate as the owner and then
reconnect as the same least-privileged application role that
:mod:`fhirbridge.storage.provisioning` provisions in production. That is also why
the isolation tests are worth having: they exercise the documented deployment
shape, not a privileged shortcut.

The container is session-scoped (starting Postgres is the expensive part) and
tables are truncated between tests. Every test is skipped, not failed, when no
Docker daemon is reachable, so `pytest` still works on a laptop without Docker;
CI runs this job explicitly.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import AsyncIterator, Iterator, Sequence

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from fhirbridge.config import Environment, Settings
from fhirbridge.domain.ids import IdPrefix, new_id
from fhirbridge.storage.models import Tenant
from fhirbridge.storage.provisioning import create_app_role_sql, grant_app_role_sql
from fhirbridge.storage.session import create_engine, create_session_factory

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

POSTGRES_IMAGE = "postgres:16-alpine"

APP_ROLE = "fhirbridge_app"
APP_PASSWORD = "integration-only-not-a-real-secret"

TABLES = (
    "idempotency_keys",
    "audit_events",
    "policies",
    "api_keys",
    "users",
    "tenants",
)


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    """Start Postgres 16 and yield an asyncpg DSN, or skip if Docker is absent."""
    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # pragma: no cover - dev extra not installed
        pytest.skip("testcontainers is not installed")

    try:
        container = PostgresContainer(POSTGRES_IMAGE, driver="asyncpg")
        container.start()
    except Exception as exc:  # pragma: no cover - no Docker on this machine
        pytest.skip(f"could not start {POSTGRES_IMAGE}: {type(exc).__name__}")

    try:
        yield str(container.get_connection_url())
    finally:
        container.stop()


@pytest.fixture(scope="session")
def owner_dsn(postgres_dsn: str) -> str:
    """The container's own superuser DSN, migrated to head.

    The migration is the thing under test as much as the schema is: RLS policies
    and the append-only trigger are installed by it, so a test that created
    tables from ``Base.metadata`` would prove nothing about what production runs.
    """
    config = Config(os.path.join(REPO_ROOT, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(REPO_ROOT, "alembic"))
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = postgres_dsn
    try:
        command.upgrade(config, "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
    return postgres_dsn


def run_as_owner(dsn: str, statements: Sequence[str]) -> None:
    """Execute owner-privileged SQL from a synchronous fixture.

    In a worker thread rather than via ``asyncio.run``: a session-scoped sync
    fixture can be resolved while pytest-asyncio's per-test loop is already
    running, and ``asyncio.run`` refuses to nest.
    """

    async def execute() -> None:
        engine = create_async_engine(dsn, poolclass=None)
        try:
            async with engine.begin() as connection:
                for statement in statements:
                    await connection.execute(text(statement))
        finally:
            await engine.dispose()

    raised: list[BaseException] = []

    def target() -> None:
        try:
            asyncio.run(execute())
        except BaseException as exc:
            raised.append(exc)

    thread = threading.Thread(target=target, name="fhirbridge-owner-ddl")
    thread.start()
    thread.join()
    if raised:
        raise raised[0]


@pytest.fixture(scope="session")
def app_role_dsn(owner_dsn: str) -> str:
    """Provision the application role and return a DSN that logs in as it.

    Uses the shipped provisioning DDL rather than hand-written grants, so a gap
    between what operators are told to run and what the application needs shows
    up here as a permission error, instead of in someone's cluster as a silently
    unprotected table.
    """
    run_as_owner(
        owner_dsn,
        [
            *create_app_role_sql(APP_ROLE, password=APP_PASSWORD),
            *grant_app_role_sql(APP_ROLE),
        ],
    )
    _, _, tail = owner_dsn.partition("@")
    return f"postgresql+asyncpg://{APP_ROLE}:{APP_PASSWORD}@{tail}"


@pytest.fixture(scope="session")
def db_settings(app_role_dsn: str) -> Settings:
    return Settings.model_validate(
        {
            "FHIRBRIDGE_ENV": Environment.DEVELOPMENT,
            "DATABASE_URL": app_role_dsn,
            "REDIS_URL": "redis://localhost:6379/0",
            "VALIDATOR_URL": "http://validator.invalid",
            "TERMINOLOGY_URL": "http://terminology.invalid",
            "LLM_EGRESS_ALLOWLIST": "",
        }
    )


@pytest.fixture
async def engine(db_settings: Settings) -> AsyncIterator[AsyncEngine]:
    created = create_engine(db_settings)
    yield created
    await created.dispose()


@pytest.fixture
async def session_factory(
    engine: AsyncEngine, owner_dsn: str
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A session factory over a clean database.

    Cleanup connects as the owner because ``TRUNCATE`` requires ownership, which
    the application role deliberately lacks — it is the privilege that would let
    a compromised API process erase the audit chain. Truncation also bypasses the
    per-row append-only trigger, so resetting between tests never needs the
    retention-purge escape.
    """
    owner = create_async_engine(owner_dsn)
    try:
        async with owner.begin() as connection:
            await connection.execute(text(f"TRUNCATE TABLE {', '.join(TABLES)} CASCADE"))
    finally:
        await owner.dispose()
    yield create_session_factory(engine)


@pytest.fixture
async def tenants(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[str, str]:
    """Two tenants, so isolation has something to isolate."""
    first = new_id(IdPrefix.TENANT)
    second = new_id(IdPrefix.TENANT)
    async with session_factory() as session:
        # Seeding runs with the privileged escape because it spans both tenants;
        # this is the same path the operator's bootstrap script uses.
        await session.execute(text("SELECT set_config('app.privileged', 'on', true)"))
        for index, identifier in enumerate((first, second)):
            session.add(
                Tenant(
                    id=identifier,
                    tenant_id=identifier,
                    name=f"Tenant {index}",
                    slug=f"tenant-{index}-{identifier[-8:].lower()}",
                )
            )
        await session.commit()
    return first, second
