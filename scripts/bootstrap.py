"""Provision a database for first use: application role, tenant, API key.

Run this once after ``alembic upgrade head``, as the role that owns the schema:

    DATABASE_URL=postgresql+asyncpg://owner:...@host/db \\
        uv run python scripts/bootstrap.py --tenant-name "General Hospital"

It does three things that the application deliberately cannot do for itself:

* **Creates the least-privileged application role** and grants it. The API must
  not connect as a superuser or the schema owner, because Postgres ignores every
  row-level security policy for those (see :mod:`fhirbridge.storage.rls`), and
  ``/readyz`` refuses to serve when it detects that.
* **Creates a tenant.** There is no self-service signup; a tenant is an isolation
  boundary an operator establishes.
* **Mints one API key** and prints it once. Only its Argon2id hash is stored, so
  a lost key is re-issued, never recovered.

The generated secrets are printed to stdout and never written to a file or a log.
Capture them in your secret manager and clear your scrollback.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import secrets
import sys
from typing import Final

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from fhirbridge.api.auth import Scope, generate_api_key
from fhirbridge.domain.ids import IdPrefix, new_id
from fhirbridge.storage.models import ApiKey, Tenant
from fhirbridge.storage.provisioning import create_app_role_sql, grant_app_role_sql
from fhirbridge.storage.rls import check_rls
from fhirbridge.storage.session import create_session_factory, privileged_session

DEFAULT_ROLE: Final[str] = "fhirbridge_app"
DEFAULT_SCOPES: Final[tuple[Scope, ...]] = (
    Scope.DOCUMENTS_WRITE,
    Scope.CONVERSIONS_WRITE,
    Scope.FACTS_READ,
    Scope.REVIEWS_WRITE,
)
"""A working set for a first key. Deliberately excludes ``phi:read``,
``reviews:submit``, ``credentials:write`` and ``admin`` — those are grants an
operator should make on purpose, per principal, not inherit from a quickstart."""

_SLUG_ALLOWED: Final = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    return _SLUG_ALLOWED.sub("-", name.strip().lower()).strip("-")[:48] or "tenant"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-name", default=None, help="Display name for the tenant.")
    parser.add_argument("--tenant-slug", default=None, help="Defaults to a slug of the name.")
    parser.add_argument("--key-label", default="bootstrap", help="Label for the API key.")
    parser.add_argument(
        "--app-role",
        default=DEFAULT_ROLE,
        help=f"Login role the API will use. Default: {DEFAULT_ROLE}.",
    )
    parser.add_argument(
        "--app-password",
        default=None,
        help="Password for the application role. Generated if omitted.",
    )
    parser.add_argument(
        "--skip-role",
        action="store_true",
        help="Do not create or grant the application role (it already exists).",
    )
    parser.add_argument(
        "--role-only",
        action="store_true",
        help=(
            "Provision the application role and its grants, then stop. This half is "
            "idempotent, so it is safe to run on every deploy; minting a tenant and "
            "an API key is not, and would hand out a new credential each time."
        ),
    )
    parser.add_argument(
        "--only-if-absent",
        action="store_true",
        help=(
            "Do nothing beyond the role if a tenant with this slug already exists. "
            "This makes the whole script safe to run on every deploy, which is what "
            "a platform's pre-deploy hook needs: without it, each deploy would mint "
            "another tenant and another API key."
        ),
    )
    args = parser.parse_args(argv)
    if args.role_only and args.skip_role:
        parser.error("--role-only and --skip-role together would do nothing")
    if not args.role_only and not args.tenant_name:
        parser.error("--tenant-name is required unless --role-only is given")
    return args


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit(
            "DATABASE_URL is not set. Point it at the schema owner: this script "
            "creates a role and grants privileges, which the application role "
            "cannot do."
        )
    return url


async def provision(args: argparse.Namespace) -> int:
    owner_url = database_url()
    engine = create_async_engine(owner_url)
    password = args.app_password or secrets.token_urlsafe(32)

    try:
        if not args.skip_role:
            statements = [
                *create_app_role_sql(args.app_role, password=password),
                *grant_app_role_sql(args.app_role),
            ]
            async with engine.begin() as connection:
                for statement in statements:
                    await connection.execute(text(statement))

        if args.role_only:
            print(f"Role {args.app_role!r} provisioned and granted.")
            return 0

        slug = args.tenant_slug or slugify(args.tenant_name)
        factory = create_session_factory(engine)

        if args.only_if_absent:
            async with privileged_session(factory, reason="bootstrap-probe") as session:
                existing = await session.scalar(select(Tenant.id).where(Tenant.slug == slug))
            if existing is not None:
                print(
                    f"Tenant {slug!r} already exists ({existing}); leaving it alone. "
                    "Its API key was shown only when it was created - issue a new key "
                    "rather than looking for the old one."
                )
                return 0

        tenant_id = new_id(IdPrefix.TENANT)
        generated = generate_api_key()

        # Privileged because this transaction creates the very tenant that the
        # RLS policies would otherwise scope it to. It is the same escape
        # authentication uses, and it is logged with a reason.
        async with privileged_session(factory, reason="bootstrap") as session:
            session.add(
                Tenant(
                    id=tenant_id,
                    tenant_id=tenant_id,
                    name=args.tenant_name,
                    slug=slug,
                )
            )
            await session.flush()
            session.add(
                ApiKey(
                    id=generated.key_id,
                    tenant_id=tenant_id,
                    tenant_fk=tenant_id,
                    prefix=generated.prefix,
                    key_hash=generated.key_hash,
                    label=args.key_label,
                    scopes=[str(scope) for scope in DEFAULT_SCOPES],
                )
            )

        async with engine.connect() as connection:
            owner_rls = await check_rls(connection)
    finally:
        await engine.dispose()

    _report(args, tenant_id, generated.plaintext.get_secret_value(), password, owner_rls.role)
    return 0


def _report(
    args: argparse.Namespace,
    tenant_id: str,
    api_key: str,
    role_password: str,
    owner_role: str,
) -> None:
    """Print the secrets once, with the warning they need.

    print() rather than logging: these values must not pass through the logging
    pipeline at all, not even to be redacted by it.
    """
    lines = [
        "",
        "Provisioned.",
        "",
        f"  tenant_id   {tenant_id}",
        f"  api_key     {api_key}",
        "",
        "The API key is shown once. Only its Argon2id hash is stored, so this",
        "cannot be recovered - re-issue instead. Scopes granted:",
        f"  {', '.join(str(scope) for scope in DEFAULT_SCOPES)}",
        "",
    ]
    if not args.skip_role:
        lines += [
            "Point the API at the least-privileged role, not at the owner:",
            "",
            f"  DATABASE_URL=postgresql+asyncpg://{args.app_role}:"
            f"{role_password}@<host>:<port>/<database>",
            "",
            f"You migrated as {owner_role!r}. Running the API as that role would",
            "disable every row-level security policy, and /readyz will refuse to",
            "serve if you do. See docs/deployment.md#database-role.",
            "",
        ]
    print("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(provision(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
