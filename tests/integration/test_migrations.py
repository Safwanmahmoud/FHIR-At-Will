"""Migration integrity.

Two failure modes are worth catching automatically. The first is drift: models
change, nobody writes a migration, and the schema in production no longer matches
the code. The second is an unreversible migration, which turns a bad deploy into
an outage.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine

from fhirbridge.storage.base import Base
from fhirbridge.storage.rls import check_rls
from tests.integration.conftest import REPO_ROOT

pytestmark = pytest.mark.integration


def alembic_config(dsn: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    """An Alembic config pointed at ``dsn``.

    ``alembic/env.py`` reads ``DATABASE_URL`` from the environment, so the
    variable is set through ``monkeypatch`` and reverted at teardown rather than
    left behind for whatever test runs next.
    """
    config = Config(os.path.join(REPO_ROOT, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(REPO_ROOT, "alembic"))
    monkeypatch.setenv("DATABASE_URL", dsn)
    return config


def _diff(connection: Connection) -> list[object]:
    context = MigrationContext.configure(
        connection,
        opts={"compare_type": True, "compare_server_default": True},
    )
    return list(compare_metadata(context, Base.metadata))


async def test_the_models_and_the_migration_agree(owner_dsn: str, engine: AsyncEngine) -> None:
    """``alembic revision --autogenerate`` on a migrated database must find nothing.

    A non-empty diff means the ORM and the shipped migration disagree, so the
    schema a developer tests against is not the schema an operator gets.
    """
    del owner_dsn
    async with engine.connect() as connection:
        differences = await connection.run_sync(_diff)

    assert differences == [], f"models drifted from the migrations: {differences}"


def test_every_revision_is_reachable_from_head(
    owner_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A branch or a broken ``down_revision`` makes ``upgrade head`` ambiguous."""
    script = ScriptDirectory.from_config(alembic_config(owner_dsn, monkeypatch))

    heads = script.get_heads()
    assert len(heads) == 1, f"expected a single head, found {heads}"

    walked = [revision.revision for revision in script.walk_revisions()]
    assert len(walked) == len(set(walked))


async def test_downgrade_then_upgrade_restores_the_schema(
    owner_dsn: str, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A migration you cannot reverse is a deploy you cannot roll back.

    Migrations run as the owner, not as the application role: creating a policy
    or a trigger requires ownership, which is exactly the privilege the runtime
    role is denied.
    """
    config = alembic_config(owner_dsn, monkeypatch)

    async with engine.connect() as connection:
        before = await connection.run_sync(
            lambda sync: sorted(
                row[0]
                for row in sync.execute(
                    text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                        "ORDER BY tablename"
                    )
                )
            )
        )
    await engine.dispose()

    # `alembic/env.py` calls `asyncio.run`, which refuses to nest inside this
    # test's event loop, so the migration runs on a worker thread.
    await asyncio.to_thread(command.downgrade, config, "base")
    await asyncio.to_thread(command.upgrade, config, "head")

    async with engine.connect() as connection:
        after = await connection.run_sync(
            lambda sync: sorted(
                row[0]
                for row in sync.execute(
                    text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                        "ORDER BY tablename"
                    )
                )
            )
        )
        differences = await connection.run_sync(_diff)
        # The re-created tables are new objects. If ALTER DEFAULT PRIVILEGES did
        # not cover them, the application role would have lost its access and
        # every subsequent request would fail on a permission error — the kind of
        # breakage that shows up after a deploy, not during one.
        restored = await check_rls(connection)

    assert after == before
    assert differences == []
    assert restored.enforced is True, restored.detail


async def test_the_trigram_extension_is_installed(engine: AsyncEngine) -> None:
    """``pg_trgm`` backs the lexical concept search the bind stage uses in M3."""
    async with engine.connect() as connection:
        result = await connection.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
        )
        assert result.scalar_one() == 1


async def test_every_timestamp_column_is_timezone_aware(engine: AsyncEngine) -> None:
    """AGENTS.md 8.2: all timestamps ``timestamptz``, UTC.

    A naive ``timestamp`` column silently reinterprets every value according to
    whatever the session timezone happens to be, which for clinical dates is a
    correctness bug, not a formatting one.
    """
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND data_type LIKE 'timestamp%'"
            )
        )
        columns = result.all()

    assert columns
    naive = [(table, column) for table, column, kind in columns if "with time zone" not in kind]
    assert naive == [], f"naive timestamp columns: {naive}"
