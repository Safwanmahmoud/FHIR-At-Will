"""The append-only trigger and the hash chain over a real database.

AGENTS.md 8.2 requires immutability to be enforced by the database, not by
application discipline: a single missing guard in a future handler would
otherwise let someone edit an audit record or a bundle. So the tests here try to
tamper using plain SQL — no ORM, no application code — and assert Postgres
refuses.

The retention purge is the one legitimate deletion path, and it is tested too:
an escape hatch nobody exercises is an escape hatch that has silently broken.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fhirbridge.storage.audit import (
    GENESIS_HASH,
    AuditAction,
    record_event,
    verify_chain,
)
from fhirbridge.storage.base import set_retention_purge_sql
from fhirbridge.storage.models import APPEND_ONLY_TABLES
from fhirbridge.storage.session import tenant_session

pytestmark = pytest.mark.integration

Factory = async_sessionmaker[AsyncSession]


async def append(factory: Factory, tenant_id: str, **kwargs: object) -> str:
    async with tenant_session(factory, tenant_id) as session:
        event = await record_event(
            session,
            tenant_id=tenant_id,
            action=kwargs.pop("action", AuditAction.VALIDATION_REQUESTED),  # type: ignore[arg-type]
            **kwargs,  # type: ignore[arg-type]
        )
        return event.id


class TestTheTriggerRefusesMutation:
    async def test_an_update_is_refused(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        first, _ = tenants
        await append(session_factory, first)

        with pytest.raises(DBAPIError) as caught:
            async with tenant_session(session_factory, first) as session:
                await session.execute(text("UPDATE audit_events SET outcome = 'failure'"))

        assert "append-only" in str(caught.value)

    async def test_a_delete_is_refused(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        first, _ = tenants
        await append(session_factory, first)

        with pytest.raises(DBAPIError) as caught:
            async with tenant_session(session_factory, first) as session:
                await session.execute(text("DELETE FROM audit_events"))

        assert "append-only" in str(caught.value)

    async def test_the_privileged_escape_does_not_permit_mutation(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        """RLS and immutability are separate controls; one must not weaken the other."""
        first, _ = tenants
        await append(session_factory, first)

        with pytest.raises(DBAPIError):
            async with session_factory() as session:
                await session.execute(text("SELECT set_config('app.privileged', 'on', true)"))
                await session.execute(text("UPDATE audit_events SET outcome = 'failure'"))
                await session.commit()

    async def test_an_insert_is_still_permitted(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        first, _ = tenants

        identifier = await append(session_factory, first)

        async with tenant_session(session_factory, first) as session:
            count = await session.execute(text("SELECT count(*) FROM audit_events"))
            assert count.scalar_one() == 1
        assert identifier

    async def test_a_failed_mutation_leaves_the_row_intact(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        first, _ = tenants
        await append(session_factory, first, outcome="success")

        with pytest.raises(DBAPIError):
            async with tenant_session(session_factory, first) as session:
                await session.execute(text("UPDATE audit_events SET outcome = 'denied'"))

        async with tenant_session(session_factory, first) as session:
            outcome = await session.execute(text("SELECT outcome FROM audit_events"))
            assert outcome.scalar_one() == "success"

    async def test_every_declared_append_only_table_has_its_trigger(
        self, session_factory: Factory
    ) -> None:
        async with session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                    "WHERE NOT t.tgisinternal AND t.tgname LIKE '%_append_only'"
                )
            )
            protected = set(result.scalars())

        assert set(APPEND_ONLY_TABLES) <= protected


class TestRetentionPurge:
    async def test_the_purge_escape_permits_deletion(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        first, _ = tenants
        await append(session_factory, first)

        async with tenant_session(session_factory, first) as session:
            await session.execute(set_retention_purge_sql())
            result = await session.execute(text("DELETE FROM audit_events"))
            assert result.rowcount == 1

    async def test_the_purge_escape_does_not_permit_an_update(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        """Retention deletes rows. It has no business editing them."""
        first, _ = tenants
        await append(session_factory, first)

        with pytest.raises(DBAPIError):
            async with tenant_session(session_factory, first) as session:
                await session.execute(set_retention_purge_sql())
                await session.execute(text("UPDATE audit_events SET outcome = 'failure'"))

    async def test_the_purge_escape_does_not_outlive_its_transaction(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        first, _ = tenants
        await append(session_factory, first)

        async with tenant_session(session_factory, first) as session:
            await session.execute(set_retention_purge_sql())

        with pytest.raises(DBAPIError):
            async with tenant_session(session_factory, first) as session:
                await session.execute(text("DELETE FROM audit_events"))

    async def test_the_purge_is_still_bound_by_tenant_isolation(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        first, second = tenants
        await append(session_factory, first)

        async with tenant_session(session_factory, second) as session:
            await session.execute(set_retention_purge_sql())
            result = await session.execute(text("DELETE FROM audit_events"))
            assert result.rowcount == 0


class TestTheChainOverARealDatabase:
    async def test_a_tenants_chain_starts_at_genesis_and_links(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        first, _ = tenants

        async with tenant_session(session_factory, first) as session:
            one = await record_event(session, tenant_id=first, action="a")
            two = await record_event(session, tenant_id=first, action="b")

        assert one.prev_hash == GENESIS_HASH
        assert two.prev_hash == one.hash

    async def test_the_chain_links_across_separate_transactions(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        """The head hash is read from the database, not carried in memory."""
        first, _ = tenants

        async with tenant_session(session_factory, first) as session:
            one = await record_event(session, tenant_id=first, action="a")
            first_hash = one.hash

        async with tenant_session(session_factory, first) as session:
            two = await record_event(session, tenant_id=first, action="b")

        assert two.prev_hash == first_hash

    async def test_verification_replays_a_real_chain(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        first, _ = tenants
        for index in range(5):
            await append(session_factory, first, subject_id=f"cnv_{index}")

        async with tenant_session(session_factory, first) as session:
            result = await verify_chain(session, tenant_id=first)

        assert result.valid is True
        assert result.checked == 5

    async def test_each_tenant_has_its_own_chain(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        """A shared chain would let one tenant's write order disclose another's activity."""
        first, second = tenants
        await append(session_factory, first)
        await append(session_factory, second)

        async with tenant_session(session_factory, second) as session:
            events = await session.execute(text("SELECT prev_hash FROM audit_events"))
            assert events.scalars().all() == [GENESIS_HASH]

    async def test_the_sequence_is_monotonic_across_tenants(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        """``sequence`` is what makes replay order well-defined, so it is assigned
        by the database rather than by whichever worker happened to write."""
        first, second = tenants
        await append(session_factory, first)
        await append(session_factory, second)
        await append(session_factory, first)

        async with session_factory() as session:
            await session.execute(text("SELECT set_config('app.privileged', 'on', true)"))
            result = await session.execute(text("SELECT sequence FROM audit_events ORDER BY id"))
            sequences = sorted(result.scalars())

        assert len(set(sequences)) == 3
        assert sequences == sorted(sequences)

    async def test_details_are_stored_redacted(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        """Principle 2.7: a key must not reach Postgres even inside an audit record."""
        first, _ = tenants
        await append(
            session_factory,
            first,
            action=AuditAction.CREDENTIAL_CREATED,
            details={"api_key": "sk-abcdefghijklmnopqrstuvwxyz012345", "provider": "openai"},
        )

        async with tenant_session(session_factory, first) as session:
            stored = await session.execute(text("SELECT details::text FROM audit_events"))
            payload = stored.scalar_one()

        assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in payload
        assert "openai" in payload
