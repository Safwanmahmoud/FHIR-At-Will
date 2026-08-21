"""Declarative base and shared column mixins (AGENTS.md 8.2)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, MetaData, String, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
"""Deterministic constraint names so Alembic autogenerate produces stable diffs."""

TENANT_GUC = "app.tenant_id"
"""The session variable Postgres row-level security policies read."""

PRIVILEGED_GUC = "app.privileged"
"""Transaction-local escape hatch for genuinely cross-tenant work.

Authenticating an API key has to find the key *before* the tenant is known, and
the retention purge spans tenants by definition. Rather than exempting those
tables from RLS — which would remove the protection permanently — the policies
carry one explicit, greppable escape that only
:func:`fhirbridge.storage.session.privileged_session` sets, for the duration of
a single transaction, with a stated reason.
"""

RETENTION_PURGE_GUC = "app.retention_purge"
"""Transaction-local flag permitting deletion from append-only tables."""


class Base(DeclarativeBase):
    """Shared declarative base."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:
        primary_key = getattr(self, "id", None)
        return f"{type(self).__name__}(id={primary_key!r})"


def utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    """``created_at`` / ``updated_at``, always timestamptz in UTC."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CreatedAtMixin:
    """``created_at`` only, for append-only tables that are never updated."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )


class TenantMixin:
    """``tenant_id`` on every table (AGENTS.md 8.2).

    The column is a plain string rather than a foreign key on the tenants table
    itself, so that RLS policies can be expressed uniformly across every table
    including ``tenants``.
    """

    tenant_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)


def rls_policy_sql(table: str) -> list[str]:
    """DDL enabling row-level security for ``table``.

    ``FORCE ROW LEVEL SECURITY`` matters: without it the table owner (which is
    usually the migration role, and often the application role too) bypasses
    every policy, and the isolation guarantee is silently absent in exactly the
    deployments that are least likely to notice.
    """
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"""
        CREATE POLICY {table}_tenant_isolation ON {table}
            USING (
                tenant_id = current_setting('{TENANT_GUC}', true)
                OR current_setting('{PRIVILEGED_GUC}', true) = 'on'
            )
            WITH CHECK (
                tenant_id = current_setting('{TENANT_GUC}', true)
                OR current_setting('{PRIVILEGED_GUC}', true) = 'on'
            )
        """,
    ]


def append_only_sql(table: str, *, allow_purge: bool = True) -> list[str]:
    """DDL making ``table`` append-only via a trigger (AGENTS.md 8.2).

    Application code is not trusted to enforce immutability: a single missing
    guard in a future handler would silently permit tampering with a bundle or
    an audit record. The database refuses.

    Retention purges are the one legitimate deletion path. They set
    ``app.retention_purge = 'on'`` for the duration of the transaction, which is
    audited by the purge job itself.
    """
    purge_escape = (
        "IF TG_OP = 'DELETE' AND current_setting('app.retention_purge', true) = 'on' THEN\n"
        "            RETURN OLD;\n"
        "        END IF;\n        "
        if allow_purge
        else ""
    )
    return [
        f"""
        CREATE OR REPLACE FUNCTION {table}_forbid_mutation() RETURNS trigger AS $$
        BEGIN
            {purge_escape}RAISE EXCEPTION
                '{table} is append-only; % is not permitted', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
        """,
        f"""
        CREATE TRIGGER {table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION {table}_forbid_mutation()
        """,
    ]


def set_tenant_sql(tenant_id: str) -> Any:
    """A statement binding the RLS tenant for the current transaction.

    ``set_config`` with a bind parameter is used instead of ``SET LOCAL``
    because the latter cannot be parameterized, which would mean interpolating a
    tenant id into SQL text.
    """
    return text(f"SELECT set_config('{TENANT_GUC}', :tenant_id, true)").bindparams(
        tenant_id=tenant_id
    )


def set_privileged_sql() -> Any:
    """A statement enabling the cross-tenant escape for this transaction only."""
    return text(f"SELECT set_config('{PRIVILEGED_GUC}', 'on', true)")


def set_retention_purge_sql() -> Any:
    """A statement permitting deletes from append-only tables in this transaction."""
    return text(f"SELECT set_config('{RETENTION_PURGE_GUC}', 'on', true)")


__all__ = [
    "NAMING_CONVENTION",
    "PRIVILEGED_GUC",
    "RETENTION_PURGE_GUC",
    "TENANT_GUC",
    "Base",
    "CreatedAtMixin",
    "TenantMixin",
    "TimestampMixin",
    "append_only_sql",
    "rls_policy_sql",
    "set_privileged_sql",
    "set_retention_purge_sql",
    "set_tenant_sql",
    "utcnow",
]
