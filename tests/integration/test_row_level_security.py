"""Row-level security (AGENTS.md 8.2, 16.6).

Tenant isolation is enforced twice: at the query layer, and by Postgres. This
file tests the second one, because it is the one that survives a bug in the
first. A handler that forgets `.where(tenant_id == ...)` must return nothing
rather than another hospital's charts.

The tests deliberately issue raw statements with *no* tenant predicate. If
isolation came from the ORM rather than the database, every one of them would
return rows.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from fhirbridge.domain.ids import IdPrefix, new_id
from fhirbridge.storage.base import TENANT_GUC, set_privileged_sql
from fhirbridge.storage.models import TENANT_SCOPED_TABLES, Policy
from fhirbridge.storage.rls import check_rls
from fhirbridge.storage.session import privileged_session, tenant_session

pytestmark = pytest.mark.integration

Factory = async_sessionmaker[AsyncSession]


async def add_policy(factory: Factory, tenant_id: str, name: str) -> str:
    identifier = new_id(IdPrefix.POLICY)
    async with tenant_session(factory, tenant_id) as session:
        session.add(
            Policy(
                id=identifier,
                tenant_id=tenant_id,
                tenant_fk=tenant_id,
                name=name,
                mode="standard",
                definition={},
                version=1,
            )
        )
    return identifier


async def all_policy_names(session: AsyncSession) -> set[str]:
    """Every policy the session can see, with no tenant predicate at all."""
    result = await session.execute(select(Policy.name))
    return set(result.scalars())


class TestReadIsolation:
    async def test_a_tenant_sees_only_its_own_rows(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        first, second = tenants
        await add_policy(session_factory, first, "first-policy")
        await add_policy(session_factory, second, "second-policy")

        async with tenant_session(session_factory, first) as session:
            assert await all_policy_names(session) == {"first-policy"}

        async with tenant_session(session_factory, second) as session:
            assert await all_policy_names(session) == {"second-policy"}

    async def test_a_direct_lookup_by_id_across_tenants_finds_nothing(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        """404, not 403: the response must not disclose that the row exists."""
        first, second = tenants
        identifier = await add_policy(session_factory, first, "first-policy")

        async with tenant_session(session_factory, second) as session:
            found = await session.get(Policy, identifier)

        assert found is None

    async def test_a_session_with_no_tenant_bound_sees_nothing(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        """Failing open here would make a forgotten binding invisible."""
        first, _ = tenants
        await add_policy(session_factory, first, "first-policy")

        async with session_factory() as session:
            assert await all_policy_names(session) == set()

    async def test_an_unknown_tenant_sees_nothing(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        first, _ = tenants
        await add_policy(session_factory, first, "first-policy")

        async with tenant_session(session_factory, new_id(IdPrefix.TENANT)) as session:
            assert await all_policy_names(session) == set()

    async def test_count_and_aggregate_queries_are_also_filtered(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        """A leak through ``count(*)`` is still a leak: it discloses volume."""
        first, second = tenants
        await add_policy(session_factory, first, "a")
        await add_policy(session_factory, first, "b")
        await add_policy(session_factory, second, "c")

        async with tenant_session(session_factory, second) as session:
            total = await session.execute(text("SELECT count(*) FROM policies"))
            assert total.scalar_one() == 1


class TestWriteIsolation:
    async def test_writing_a_row_for_another_tenant_is_refused(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        """The ``WITH CHECK`` half of the policy. Without it, one tenant could
        plant rows in another's account even though it could not read them."""
        first, second = tenants

        with pytest.raises(DBAPIError):
            async with tenant_session(session_factory, first) as session:
                session.add(
                    Policy(
                        id=new_id(IdPrefix.POLICY),
                        tenant_id=second,
                        tenant_fk=second,
                        name="planted",
                        mode="standard",
                        definition={},
                        version=1,
                    )
                )
                await session.flush()

    async def test_updating_another_tenants_row_affects_nothing(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        first, second = tenants
        await add_policy(session_factory, first, "first-policy")

        async with tenant_session(session_factory, second) as session:
            result = await session.execute(
                text("UPDATE policies SET name = 'tampered' WHERE name = 'first-policy'")
            )
            assert result.rowcount == 0

        async with tenant_session(session_factory, first) as session:
            assert await all_policy_names(session) == {"first-policy"}

    async def test_deleting_another_tenants_row_affects_nothing(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        first, second = tenants
        await add_policy(session_factory, first, "first-policy")

        async with tenant_session(session_factory, second) as session:
            result = await session.execute(text("DELETE FROM policies"))
            assert result.rowcount == 0

        async with tenant_session(session_factory, first) as session:
            assert await all_policy_names(session) == {"first-policy"}


class TestEnforcementIsRealAndNotAssumed:
    """Whether the policies apply at all, which depends on the connected role.

    Every assertion in the rest of this file is vacuous unless RLS is actually in
    force for the role the application logs in as. Postgres ignores policies
    entirely for a superuser or a ``BYPASSRLS`` role, and for the table owner
    unless ``FORCE`` is set — so "the migration installed a policy" and "tenant
    data is isolated" are two different claims.
    """

    async def test_rls_is_enabled_and_forced_on_every_tenant_scoped_table(
        self, session_factory: Factory
    ) -> None:
        async with session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = ANY(:names)"
                ).bindparams(names=list(TENANT_SCOPED_TABLES))
            )
            rows = {name: (enabled, forced) for name, enabled, forced in result.all()}

        assert set(rows) == set(TENANT_SCOPED_TABLES)
        for table, (enabled, forced) in rows.items():
            assert enabled, f"{table} does not have RLS enabled"
            assert forced, f"{table} does not have RLS forced"

    async def test_postgres_confirms_the_policies_apply_to_the_application_role(
        self, session_factory: Factory
    ) -> None:
        """``row_security_active`` is the server's own answer, not our inference.

        It folds superuser, ``BYPASSRLS``, ownership and ``FORCE`` into one
        boolean per table, which is why the readiness probe asks it rather than
        re-deriving the rules from ``pg_roles``.
        """
        async with session_factory() as session:
            status = await check_rls(session)

        assert status.enforced is True, status.detail
        assert status.unprotected_tables == ()
        assert status.missing_tables == ()
        assert status.role != "postgres"

    async def test_the_application_role_holds_no_bypass_and_owns_nothing(
        self, session_factory: Factory
    ) -> None:
        """The privileges that would make the policies decorative."""
        async with session_factory() as session:
            attributes = await session.execute(
                text(
                    "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            )
            assert attributes.one() == (False, False, False, False)

            owned = await session.execute(
                text(
                    "SELECT count(*) FROM pg_tables "
                    "WHERE schemaname = 'public' AND tableowner = current_user"
                )
            )
            assert owned.scalar_one() == 0

    async def test_the_application_role_cannot_drop_the_protections(
        self, session_factory: Factory
    ) -> None:
        """Defence in depth against a compromised API process.

        If the application role could disable the policy or the append-only
        trigger, an attacker with code execution in the API would not need to
        defeat either — they would just turn them off.
        """
        for statement in (
            "ALTER TABLE policies DISABLE ROW LEVEL SECURITY",
            "ALTER TABLE policies NO FORCE ROW LEVEL SECURITY",
            "DROP POLICY policies_tenant_isolation ON policies",
            "ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only",
            "DROP TABLE idempotency_keys",
        ):
            with pytest.raises(DBAPIError):
                async with session_factory() as session:
                    await session.execute(text(statement))

    async def test_the_guard_notices_a_role_that_bypasses_rls(self, owner_dsn: str) -> None:
        """The failure this whole arrangement exists to prevent, demonstrated.

        The container's default role is a superuser with ``BYPASSRLS`` — the same
        role a quickstart ``DATABASE_URL`` points at. Connected as it, the
        policies are inert and every tenant sees every row. ``check_rls`` has to
        report that, because it is what ``/readyz`` refuses to serve on.
        """
        engine = create_async_engine(owner_dsn)
        try:
            async with engine.connect() as connection:
                status = await check_rls(connection)
        finally:
            await engine.dispose()

        assert status.enforced is False
        assert status.bypasses_rls is True
        assert set(status.unprotected_tables) == set(TENANT_SCOPED_TABLES)
        assert status.detail is not None
        assert "BYPASSRLS" in status.detail

    async def test_a_bypassing_role_really_does_see_other_tenants_rows(
        self, session_factory: Factory, owner_dsn: str, tenants: tuple[str, str]
    ) -> None:
        """Proof that the previous test is reporting a real exposure, not a flag.

        Without this, ``check_rls`` could be measuring something incidental. Here
        the superuser reads a row it has no tenant binding for at all.
        """
        first, _ = tenants
        await add_policy(session_factory, first, "first-policy")

        engine = create_async_engine(owner_dsn)
        try:
            async with engine.connect() as connection:
                visible = await connection.execute(select(Policy.name))
                assert set(visible.scalars()) == {"first-policy"}
        finally:
            await engine.dispose()


class TestBindingLifetime:
    async def test_the_binding_does_not_outlive_its_transaction(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        """Pooled connections are reused. A leaked GUC is a cross-tenant read."""
        first, second = tenants
        await add_policy(session_factory, first, "first-policy")

        async with tenant_session(session_factory, first) as session:
            assert await all_policy_names(session) == {"first-policy"}

        # A fresh session over the same pool, with nothing bound.
        async with session_factory() as session:
            assert await all_policy_names(session) == set()
            bound = await session.execute(text(f"SELECT current_setting('{TENANT_GUC}', true)"))
            assert not bound.scalar_one()
        del second

    async def test_a_rolled_back_transaction_does_not_persist_rows(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        first, _ = tenants

        with pytest.raises(RuntimeError):
            async with tenant_session(session_factory, first) as session:
                session.add(
                    Policy(
                        id=new_id(IdPrefix.POLICY),
                        tenant_id=first,
                        tenant_fk=first,
                        name="doomed",
                        mode="standard",
                        definition={},
                        version=1,
                    )
                )
                await session.flush()
                raise RuntimeError("handler failed after the write")

        async with tenant_session(session_factory, first) as session:
            assert await all_policy_names(session) == set()


class TestPrivilegedEscape:
    async def test_it_can_read_across_tenants(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        """Authenticating an API key has to find the key before the tenant is known."""
        first, second = tenants
        await add_policy(session_factory, first, "first-policy")
        await add_policy(session_factory, second, "second-policy")

        async with privileged_session(session_factory, reason="test") as session:
            assert await all_policy_names(session) == {"first-policy", "second-policy"}

    async def test_it_does_not_outlive_its_transaction(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        first, second = tenants
        await add_policy(session_factory, first, "first-policy")
        await add_policy(session_factory, second, "second-policy")

        async with privileged_session(session_factory, reason="test") as session:
            assert len(await all_policy_names(session)) == 2

        async with session_factory() as session:
            assert await all_policy_names(session) == set()

    async def test_it_must_be_set_explicitly(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        """Any value other than the exact escape string must not open the door."""
        first, _ = tenants
        await add_policy(session_factory, first, "first-policy")

        for value in ("off", "true", "1", "ON", ""):
            async with session_factory() as session:
                await session.execute(
                    text("SELECT set_config('app.privileged', :value, true)").bindparams(
                        value=value
                    )
                )
                assert await all_policy_names(session) == set(), f"{value!r} opened the escape"

        async with session_factory() as session:
            await session.execute(set_privileged_sql())
            assert await all_policy_names(session) == {"first-policy"}
