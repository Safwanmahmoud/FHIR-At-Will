"""The row-level-security guard and the role it expects (AGENTS.md 8.2).

These tests cover the classification and the DDL. Whether Postgres agrees with
any of it is asserted in ``tests/integration/test_row_level_security.py`` against
a real server, because that is the only place the answer means anything.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from fhirbridge.storage.models import TENANT_SCOPED_TABLES
from fhirbridge.storage.provisioning import (
    create_app_role_sql,
    grant_app_role_sql,
    quote_identifier,
)
from fhirbridge.storage.rls import check_rls


class FakeConnection:
    """Answers the two queries :func:`check_rls` issues, in order."""

    def __init__(self, role: str, rows: list[Any]) -> None:
        self.role = role
        self.rows = rows
        self.calls = 0

    async def execute(self, statement: object, parameters: object = None) -> Any:
        del statement, parameters
        self.calls += 1
        role, rows = self.role, self.rows

        class _Result:
            def scalar_one(self) -> str:
                return role

            def all(self) -> list[Any]:
                return rows

        return _Result()


def rows(**overrides: tuple[bool, bool, bool]) -> list[Any]:
    """One catalogue row per tenant-scoped table, all healthy unless overridden."""
    return [
        SimpleNamespace(
            table_name=table,
            active=overrides.get(table, (True, True, True))[0],
            enabled=overrides.get(table, (True, True, True))[1],
            forced=overrides.get(table, (True, True, True))[2],
        )
        for table in TENANT_SCOPED_TABLES
    ]


class TestClassification:
    async def test_a_fully_protected_schema_is_enforced(self) -> None:
        status = await check_rls(FakeConnection("fhirbridge_app", rows()))  # type: ignore[arg-type]

        assert status.enforced is True
        assert status.unprotected_tables == ()
        assert status.missing_tables == ()
        assert status.bypasses_rls is False
        assert status.detail is None

    async def test_a_role_that_bypasses_every_table_is_identified_as_such(self) -> None:
        """Policies installed and forced, yet inactive everywhere: it is the role.

        Distinguishing this from a missing migration matters because the two
        remedies are unrelated — one is a DSN change, the other is a deploy.
        """
        inactive = dict.fromkeys(TENANT_SCOPED_TABLES, (False, True, True))
        status = await check_rls(FakeConnection("postgres", rows(**inactive)))  # type: ignore[arg-type]

        assert status.enforced is False
        assert status.bypasses_rls is True
        assert set(status.unprotected_tables) == set(TENANT_SCOPED_TABLES)
        assert status.detail is not None
        assert "BYPASSRLS" in status.detail
        assert "postgres" in status.detail

    async def test_one_table_missing_its_policy_is_not_a_bypass(self) -> None:
        """A single unprotected table means the migration, not the role."""
        status = await check_rls(  # type: ignore[arg-type]
            FakeConnection("fhirbridge_app", rows(policies=(False, False, False)))
        )

        assert status.enforced is False
        assert status.bypasses_rls is False
        assert status.unprotected_tables == ("policies",)
        assert status.detail is not None
        assert "policies" in status.detail

    async def test_an_unmigrated_database_says_so(self) -> None:
        status = await check_rls(FakeConnection("fhirbridge_app", []))  # type: ignore[arg-type]

        assert status.enforced is False
        assert set(status.missing_tables) == set(TENANT_SCOPED_TABLES)
        assert status.detail is not None
        assert "alembic upgrade head" in status.detail

    async def test_it_reports_rather_than_raises(self) -> None:
        """The caller is a health probe; an exception would read as an outage."""
        status = await check_rls(  # type: ignore[arg-type]
            FakeConnection("postgres", rows(tenants=(False, True, True)))
        )

        assert status.enforced is False


class TestTheDetailIsSafeToReturn:
    @pytest.mark.parametrize(
        "role",
        ["postgres", "fhirbridge_app", "admin"],
    )
    async def test_it_names_the_role_and_the_document_but_nothing_else(self, role: str) -> None:
        inactive = dict.fromkeys(TENANT_SCOPED_TABLES, (False, True, True))
        status = await check_rls(FakeConnection(role, rows(**inactive)))  # type: ignore[arg-type]

        assert status.detail is not None
        assert role in status.detail
        assert "docs/deployment.md" in status.detail
        # The detail reaches an authenticated health response, so it must carry no
        # credential material and no clinical content (principles 2.6, 2.7).
        for forbidden in ("password", "postgresql://", "sk-", "Bearer"):
            assert forbidden not in status.detail


class TestQuoteIdentifier:
    @pytest.mark.parametrize("name", ["fhirbridge_app", "app", "a_1", "_x"])
    def test_it_accepts_ordinary_role_names(self, name: str) -> None:
        assert quote_identifier(name) == f'"{name}"'

    @pytest.mark.parametrize(
        "name",
        [
            'app"; DROP TABLE tenants; --',
            "app role",
            "App",
            "1app",
            "",
            "a" * 64,
            "app;",
            "app-role",
        ],
    )
    def test_it_refuses_anything_that_would_need_escaping(self, name: str) -> None:
        with pytest.raises(ValueError, match="valid unquoted PostgreSQL identifier"):
            quote_identifier(name)


class TestGrants:
    def test_the_role_gets_dml_and_nothing_structural(self) -> None:
        statements = grant_app_role_sql("fhirbridge_app")
        joined = "\n".join(statements)

        assert "SELECT, INSERT, UPDATE, DELETE" in joined
        # The privileges that would let a compromised API process remove the very
        # protections it is subject to.
        for forbidden in ("TRUNCATE", "REFERENCES", "TRIGGER", "CREATE", "ALL PRIVILEGES"):
            assert forbidden not in joined

    def test_it_explicitly_strips_bypass_and_superuser(self) -> None:
        joined = "\n".join(grant_app_role_sql("fhirbridge_app"))

        assert "NOBYPASSRLS NOSUPERUSER" in joined

    def test_default_privileges_cover_tables_a_later_migration_adds(self) -> None:
        """Without this, the next migration silently breaks every request."""
        joined = "\n".join(grant_app_role_sql("fhirbridge_app"))

        assert joined.count("ALTER DEFAULT PRIVILEGES") == 2

    def test_a_hostile_role_name_never_reaches_the_ddl(self) -> None:
        with pytest.raises(ValueError, match="valid unquoted PostgreSQL identifier"):
            grant_app_role_sql('x"; ALTER ROLE postgres SUPERUSER; --')


class TestRoleCreation:
    def test_the_role_is_created_without_any_dangerous_attribute(self) -> None:
        statement = create_app_role_sql("fhirbridge_app", password="s3cret")[0]

        for attribute in (
            "NOSUPERUSER",
            "NOBYPASSRLS",
            "NOCREATEDB",
            "NOCREATEROLE",
            "NOINHERIT",
        ):
            assert attribute in statement

    def test_it_is_idempotent(self) -> None:
        """Re-provisioning is routine; it must not fail the second time."""
        statement = create_app_role_sql("fhirbridge_app", password="s3cret")[0]

        assert "IF NOT EXISTS (SELECT 1 FROM pg_roles" in statement

    def test_a_quote_in_the_password_cannot_terminate_the_literal(self) -> None:
        statement = create_app_role_sql(
            "fhirbridge_app", password="a'; ALTER ROLE x SUPERUSER; --"
        )[0]

        assert "PASSWORD 'a''; ALTER ROLE x SUPERUSER; --'" in statement
        # One statement, so nothing was smuggled past the literal.
        assert statement.count("CREATE ROLE") == 1

    def test_a_hostile_role_name_never_reaches_the_ddl(self) -> None:
        with pytest.raises(ValueError, match="valid unquoted PostgreSQL identifier"):
            create_app_role_sql("x; ALTER ROLE postgres SUPERUSER", password="s3cret")
