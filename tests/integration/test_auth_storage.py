"""Authentication against a real database.

The unit tests cover the key format and the hashing. What needs a real Postgres
is the interaction between authentication and row-level security: a key lookup
happens *before* the tenant is known, so it necessarily runs with the privileged
escape — and this file pins that the escape is required, that it is all it grants,
and that no plaintext secret is stored.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fhirbridge.api.auth import (
    Scope,
    authenticate_api_key,
    generate_api_key,
)
from fhirbridge.domain.errors import UnauthenticatedError
from fhirbridge.storage.models import ApiKey
from fhirbridge.storage.session import privileged_session, tenant_session

pytestmark = pytest.mark.integration

Factory = async_sessionmaker[AsyncSession]

SCOPES = [str(Scope.CONVERSIONS_WRITE), str(Scope.FACTS_READ)]


async def issue_key(
    factory: Factory, tenant_id: str, *, scopes: list[str] | None = None, **columns: object
) -> tuple[str, str]:
    """Provision a key and return ``(key_id, plaintext)``."""
    generated = generate_api_key()
    async with tenant_session(factory, tenant_id) as session:
        session.add(
            ApiKey(
                id=generated.key_id,
                tenant_id=tenant_id,
                tenant_fk=tenant_id,
                prefix=generated.prefix,
                key_hash=generated.key_hash,
                label="integration",
                scopes=SCOPES if scopes is None else scopes,
                **columns,  # type: ignore[arg-type]
            )
        )
    return generated.key_id, generated.plaintext.get_secret_value()


class TestAuthentication:
    async def test_a_valid_key_authenticates_to_its_own_tenant(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        first, _ = tenants
        key_id, plaintext = await issue_key(session_factory, first)

        async with privileged_session(session_factory, reason="test") as session:
            principal = await authenticate_api_key(session, plaintext)

        assert principal.tenant_id == first
        assert principal.actor_id == key_id
        assert principal.scopes == {Scope.CONVERSIONS_WRITE, Scope.FACTS_READ}

    async def test_lookup_requires_the_privileged_escape(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        """The tenant is unknown until the key is found, so RLS cannot help here.

        This asserts the constraint honestly rather than papering over it: without
        the escape the row is invisible and authentication fails, which is why
        ``privileged_session`` exists and why its use is logged with a reason.
        """
        first, _ = tenants
        _, plaintext = await issue_key(session_factory, first)

        async with session_factory() as session:
            with pytest.raises(UnauthenticatedError):
                await authenticate_api_key(session, plaintext)

    async def test_a_revoked_key_is_refused(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        from fhirbridge.storage.base import utcnow

        first, _ = tenants
        _, plaintext = await issue_key(session_factory, first, revoked_at=utcnow())

        async with privileged_session(session_factory, reason="test") as session:
            with pytest.raises(UnauthenticatedError):
                await authenticate_api_key(session, plaintext)

    async def test_an_expired_key_is_refused(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        from datetime import UTC, datetime

        first, _ = tenants
        _, plaintext = await issue_key(
            session_factory, first, expires_at=datetime(2020, 1, 1, tzinfo=UTC)
        )

        async with privileged_session(session_factory, reason="test") as session:
            with pytest.raises(UnauthenticatedError):
                await authenticate_api_key(session, plaintext)

    async def test_an_unknown_key_is_refused_the_same_way(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        """The message must not distinguish "no such key" from "wrong secret"."""
        first, _ = tenants
        _, plaintext = await issue_key(session_factory, first)
        tampered = plaintext[:-4] + "zzzz"

        async with privileged_session(session_factory, reason="test") as session:
            with pytest.raises(UnauthenticatedError) as wrong_secret:
                await authenticate_api_key(session, tampered)
            with pytest.raises(UnauthenticatedError) as no_such_key:
                await authenticate_api_key(session, "fhirb_deadbeef_nothing")

        assert str(wrong_secret.value) == str(no_such_key.value)

    async def test_an_unrecognized_stored_scope_is_dropped_not_honoured(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        first, _ = tenants
        _, plaintext = await issue_key(
            session_factory, first, scopes=["conversions:write", "future:superpower"]
        )

        async with privileged_session(session_factory, reason="test") as session:
            principal = await authenticate_api_key(session, plaintext)

        assert principal.scopes == {Scope.CONVERSIONS_WRITE}


class TestStoredKeyMaterial:
    async def test_no_column_holds_the_plaintext(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        """Principle 2.7, checked against the actual bytes on disk."""
        first, _ = tenants
        _, plaintext = await issue_key(session_factory, first)
        secret = plaintext.rsplit("_", 1)[-1]

        async with tenant_session(session_factory, first) as session:
            row = await session.execute(text("SELECT to_jsonb(api_keys) FROM api_keys"))
            serialized = str(row.scalar_one())

        assert secret not in serialized
        assert plaintext not in serialized

    async def test_the_stored_hash_is_argon2id(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        first, _ = tenants
        await issue_key(session_factory, first)

        async with tenant_session(session_factory, first) as session:
            stored = await session.execute(text("SELECT key_hash FROM api_keys"))
            key_hash = stored.scalar_one()

        assert key_hash.startswith("$argon2id$")

    async def test_two_keys_cannot_share_a_prefix(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        """The prefix index is what makes verification single-row; a collision
        would make ``scalar_one_or_none`` raise instead of authenticating."""
        from sqlalchemy.exc import IntegrityError

        first, second = tenants
        generated = generate_api_key()
        async with tenant_session(session_factory, first) as session:
            session.add(
                ApiKey(
                    id=generated.key_id,
                    tenant_id=first,
                    tenant_fk=first,
                    prefix=generated.prefix,
                    key_hash=generated.key_hash,
                    scopes=SCOPES,
                )
            )

        from fhirbridge.domain.ids import IdPrefix, new_id

        with pytest.raises(IntegrityError):
            async with tenant_session(session_factory, second) as session:
                session.add(
                    ApiKey(
                        id=new_id(IdPrefix.API_KEY),
                        tenant_id=second,
                        tenant_fk=second,
                        prefix=generated.prefix,
                        key_hash=generated.key_hash,
                        scopes=SCOPES,
                    )
                )
                await session.flush()

    async def test_a_key_is_invisible_to_another_tenant(
        self, session_factory: Factory, tenants: tuple[str, str]
    ) -> None:
        first, second = tenants
        await issue_key(session_factory, first)

        async with tenant_session(session_factory, second) as session:
            count = await session.execute(text("SELECT count(*) FROM api_keys"))
            assert count.scalar_one() == 0
