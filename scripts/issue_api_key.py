"""Issue a new API key for an existing tenant.

Lost API keys cannot be recovered because only an Argon2id hash is stored. Run
this operator command with the schema-owner database URL to mint a replacement:

    DATABASE_URL=postgresql+asyncpg://owner:...@host/db \
        python scripts/issue_api_key.py --tenant-slug general-hospital

The plaintext key is printed once, after the database transaction commits. The
command does not revoke existing keys; rotation should be verified before an old
credential is revoked.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from fhirbridge.api.auth import GeneratedApiKey, Scope, generate_api_key
from fhirbridge.storage.models import ApiKey, Tenant
from fhirbridge.storage.session import create_session_factory, privileged_session


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    tenant = parser.add_mutually_exclusive_group(required=True)
    tenant.add_argument("--tenant-slug", help="Slug of the tenant receiving the key.")
    tenant.add_argument("--tenant-id", help="ID of the tenant receiving the key.")
    tenant.add_argument(
        "--only-tenant",
        action="store_true",
        help="Issue to the tenant only when the database contains exactly one.",
    )
    parser.add_argument("--key-label", default="smoke-test", help="Operator-visible key label.")
    parser.add_argument(
        "--scope",
        action="append",
        choices=[str(scope) for scope in Scope],
        default=[],
        help=(
            "Scope to grant; repeat for multiple scopes. Validation needs authentication "
            "but no scopes, so the default is an unscoped key."
        ),
    )
    return parser.parse_args(argv)


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit(
            "DATABASE_URL is not set. Point it at the schema owner, not the "
            "least-privileged application role."
        )
    return url


async def issue_key(
    owner_url: str,
    *,
    tenant_slug: str | None,
    tenant_id: str | None,
    only_tenant: bool,
    label: str,
    scopes: list[str],
) -> tuple[Tenant, GeneratedApiKey]:
    """Persist and return a newly generated key for exactly one existing tenant."""
    engine = create_async_engine(owner_url)
    generated = generate_api_key()
    try:
        factory = create_session_factory(engine)
        async with privileged_session(factory, reason="issue-api-key") as session:
            existing: Tenant | None
            if only_tenant:
                tenants = list(await session.scalars(select(Tenant).limit(2)))
                if len(tenants) != 1:
                    raise SystemExit(
                        f"Expected exactly one tenant, found {len(tenants)}. "
                        "Use --tenant-slug or --tenant-id."
                    )
                existing = tenants[0]
            else:
                predicate = (
                    Tenant.slug == tenant_slug
                    if tenant_slug is not None
                    else Tenant.id == tenant_id
                )
                existing = await session.scalar(select(Tenant).where(predicate))
            if existing is None:
                selector = (
                    f"slug {tenant_slug!r}"
                    if tenant_slug is not None
                    else f"id {tenant_id!r}"
                )
                raise SystemExit(f"No tenant exists with {selector}. No API key was issued.")
            session.add(
                ApiKey(
                    id=generated.key_id,
                    tenant_id=existing.id,
                    tenant_fk=existing.id,
                    prefix=generated.prefix,
                    key_hash=generated.key_hash,
                    label=label,
                    scopes=scopes,
                )
            )
        return existing, generated
    finally:
        await engine.dispose()


def report(tenant: Tenant, generated: GeneratedApiKey, scopes: list[str]) -> None:
    print()
    print("API key issued.")
    print()
    print(f"  tenant_id    {tenant.id}")
    print(f"  tenant_slug  {tenant.slug}")
    print(f"  key_id       {generated.key_id}")
    print(f"  api_key      {generated.plaintext.get_secret_value()}")
    print(f"  scopes       {', '.join(scopes) if scopes else '(none)'}")
    print()
    print("Copy the API key now. It cannot be recovered from the stored Argon2id hash.")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    tenant, generated = asyncio.run(
        issue_key(
            database_url(),
            tenant_slug=args.tenant_slug,
            tenant_id=args.tenant_id,
            only_tenant=args.only_tenant,
            label=args.key_label,
            scopes=args.scope,
        )
    )
    report(tenant, generated, args.scope)
    return 0


if __name__ == "__main__":
    sys.exit(main())
