"""Provisioning the least-privileged role the application must connect as.

Row-level security is inert for a superuser, for any role holding ``BYPASSRLS``,
and — without ``FORCE ROW LEVEL SECURITY`` — for the owner of the table. The
default role in the official Postgres image is a superuser with ``BYPASSRLS``, so
the most convenient ``DATABASE_URL`` an operator can write is also the one that
silently switches off tenant isolation. :mod:`fhirbridge.storage.rls` detects
that at readiness; this module is how you fix it.

Two roles, deliberately:

* the **migration role** owns the schema and runs Alembic, because installing
  policies and triggers requires ownership;
* the **application role** owns nothing, holds no ``BYPASSRLS``, and has only
  ``SELECT``/``INSERT``/``UPDATE``/``DELETE``. It cannot drop a table, alter a
  policy, or disable the append-only trigger — so a compromised API process
  cannot rewrite the audit chain it is being audited by.

The grants live here rather than in a ``.sql`` file so that the table list is
derived from the model metadata and cannot drift out of date when a migration
adds a table.
"""

from __future__ import annotations

import re
from typing import Final

_IDENTIFIER: Final = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def quote_identifier(name: str) -> str:
    """Validate and quote a role or schema name.

    Role names arrive from configuration, not from a request, but they are still
    interpolated into DDL that cannot be parameterized. An allowlist is cheaper
    than reasoning about whether that path is reachable.
    """
    if not _IDENTIFIER.match(name):
        raise ValueError(
            f"{name!r} is not a valid unquoted PostgreSQL identifier: use lowercase "
            "letters, digits and underscores, starting with a letter or underscore."
        )
    return f'"{name}"'


def grant_app_role_sql(role: str, *, schema: str = "public") -> list[str]:
    """DDL granting ``role`` exactly what the application needs, and no more.

    Run after every ``alembic upgrade``: ``ALTER DEFAULT PRIVILEGES`` covers
    tables created later by the same migration role, but not tables that already
    exist, and a half-granted schema fails at request time rather than at deploy
    time.
    """
    identifier = quote_identifier(role)
    namespace = quote_identifier(schema)
    return [
        f"GRANT USAGE ON SCHEMA {namespace} TO {identifier}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {namespace} TO {identifier}",
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {namespace} TO {identifier}",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {namespace} "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {identifier}",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {namespace} "
        f"GRANT USAGE, SELECT ON SEQUENCES TO {identifier}",
        # Explicit, even though it is the default for a freshly created role.
        # Someone will eventually grant this role to a human for debugging, and
        # the whole point of it is that it cannot bypass a policy.
        f"ALTER ROLE {identifier} NOBYPASSRLS NOSUPERUSER",
    ]


def create_app_role_sql(role: str, *, password: str) -> list[str]:
    """DDL creating the application login role if it does not already exist.

    The password is interpolated as a quoted literal because ``CREATE ROLE``
    takes no parameters. It is doubled rather than escaped, which is what
    PostgreSQL's string literal syntax requires, and callers pass a generated
    secret — never one echoed from a request.
    """
    identifier = quote_identifier(role)
    literal = password.replace("'", "''")
    # S608: CREATE ROLE accepts no bind parameters, so both values must be
    # interpolated. `role` is allowlisted by quote_identifier above, and
    # `password` is doubled per PostgreSQL's string-literal rules and only ever
    # comes from an operator's generated secret, never from a request.
    statement = f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                CREATE ROLE {identifier} LOGIN PASSWORD '{literal}'
                    NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
            END IF;
        END
        $$
    """  # noqa: S608
    return [statement]


__all__ = ["create_app_role_sql", "grant_app_role_sql", "quote_identifier"]
