"""Verifying that row-level security is actually in force (AGENTS.md 8.2).

Installing RLS policies is not the same as having them apply. Postgres skips
every policy for a role that is a superuser or that carries ``BYPASSRLS``, and
skips them for the table's owner unless ``FORCE ROW LEVEL SECURITY`` is also set.
So a deployment can pass every migration, install every policy, and still serve
every tenant's rows to every tenant — which is the failure AGENTS.md 8.2 calls
"the single most damaging bug class in multi-tenant health software".

It is not a hypothetical. The default role in the official Postgres image is a
superuser with ``BYPASSRLS``, and connecting the application as that role is the
path of least resistance in every quickstart, compose file and managed-database
setup wizard. The protection would be absent precisely where nobody looks.

Postgres will answer the question directly. ``row_security_active(regclass)``
reports whether policies apply to *this* role on *that* table, folding
superuser, ``BYPASSRLS``, ownership and ``FORCE`` into one boolean. That is
strictly better than inspecting ``pg_roles`` ourselves and re-deriving rules the
server already knows.

The result feeds ``/readyz``: with ``REQUIRE_RLS_ENFORCEMENT`` on (the default),
a process that cannot isolate tenants reports itself unready rather than taking
traffic.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from fhirbridge.storage.models import TENANT_SCOPED_TABLES

logger = logging.getLogger(__name__)

_QUERY = text(
    """
    SELECT c.relname AS table_name,
           row_security_active(c.oid) AS active,
           c.relrowsecurity AS enabled,
           c.relforcerowsecurity AS forced
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relname = ANY(:tables)
      AND n.nspname = current_schema()
      AND c.relkind = 'r'
    """
)


@dataclass(frozen=True, slots=True)
class RlsStatus:
    """Whether tenant isolation is enforced for the connected role."""

    role: str
    enforced: bool
    unprotected_tables: tuple[str, ...] = ()
    missing_tables: tuple[str, ...] = ()
    bypasses_rls: bool = False

    @property
    def detail(self) -> str | None:
        """A PHI-free, secret-free explanation, safe for a health response.

        Names the role because that is the one piece of information an operator
        needs to fix this, and it is not a secret — it is already in the DSN they
        configured.
        """
        if self.enforced:
            return None
        if self.missing_tables:
            return (
                f"{len(self.missing_tables)} tenant-scoped table(s) are absent from "
                "the connected database. Run `alembic upgrade head`."
            )
        if self.bypasses_rls:
            return (
                f"Role {self.role!r} bypasses row-level security (it is a superuser "
                "or holds BYPASSRLS), so tenant isolation is not enforced for any "
                "table. Connect the application as a dedicated, non-superuser role. "
                "See docs/deployment.md#database-role."
            )
        listed = ", ".join(self.unprotected_tables[:5])
        return (
            f"Row-level security is not active for role {self.role!r} on: {listed}. "
            "Tenant isolation is not enforced. See docs/deployment.md#database-role."
        )


async def check_rls(
    connection: AsyncSession | AsyncConnection,
    *,
    tables: Sequence[str] = TENANT_SCOPED_TABLES,
) -> RlsStatus:
    """Ask Postgres whether its policies apply to the current role.

    Never raises for a policy problem — an unreachable database is the caller's
    concern, but "RLS is off" is a finding to report, not an exception to swallow
    somewhere up the stack.
    """
    role = str((await connection.execute(text("SELECT current_user"))).scalar_one())
    rows = (await connection.execute(_QUERY, {"tables": list(tables)})).all()

    found = {str(row.table_name): bool(row.active) for row in rows}
    unprotected = tuple(name for name in tables if name in found and not found[name])
    missing = tuple(name for name in tables if name not in found)

    # If policies are installed and forced yet still inactive everywhere, the
    # role itself is the reason. Distinguishing this is worth it: the fix is
    # "change the DSN", not "re-run the migration".
    installed = {str(row.table_name) for row in rows if bool(row.enabled) and bool(row.forced)}
    bypasses = bool(installed) and installed.issubset(set(unprotected))

    status = RlsStatus(
        role=role,
        enforced=not unprotected and not missing,
        unprotected_tables=unprotected,
        missing_tables=missing,
        bypasses_rls=bypasses,
    )
    if not status.enforced:
        logger.error(
            "rls_not_enforced",
            extra={
                "role": role,
                "unprotected_table_count": len(unprotected),
                "missing_table_count": len(missing),
                "bypasses_rls": bypasses,
            },
        )
    return status


__all__ = ["RlsStatus", "check_rls"]
