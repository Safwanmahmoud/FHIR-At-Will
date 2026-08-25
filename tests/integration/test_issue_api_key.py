"""Operator key re-issuance against a real Postgres database."""

from __future__ import annotations

from scripts.issue_api_key import issue_key
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fhirbridge.api.auth import authenticate_api_key
from fhirbridge.storage.models import ApiKey, Tenant
from fhirbridge.storage.session import privileged_session, tenant_session


async def test_issues_an_authenticating_key_for_an_existing_tenant(
    owner_dsn: str,
    session_factory: async_sessionmaker[AsyncSession],
    tenants: tuple[str, str],
) -> None:
    tenant_id, _ = tenants
    async with tenant_session(session_factory, tenant_id) as session:
        tenant_slug = await session.scalar(select(Tenant.slug))
    assert tenant_slug is not None

    tenant, generated = await issue_key(
        owner_dsn,
        tenant_slug=tenant_slug,
        tenant_id=None,
        only_tenant=False,
        label="smoke-test",
        scopes=[],
    )
    plaintext = generated.plaintext.get_secret_value()

    async with privileged_session(session_factory, reason="test") as session:
        principal = await authenticate_api_key(session, plaintext)
        stored = await session.scalar(select(ApiKey).where(ApiKey.id == generated.key_id))

    assert tenant.id == tenant_id
    assert principal.tenant_id == tenant_id
    assert principal.scopes == set()
    assert stored is not None
    assert stored.label == "smoke-test"
    assert plaintext not in stored.key_hash
