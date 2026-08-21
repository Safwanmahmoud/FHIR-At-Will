"""Async engine and tenant-scoped sessions (AGENTS.md 8.2).

Every session that touches tenant data must first bind ``app.tenant_id``, which
is what the row-level security policies read. :func:`tenant_session` is the only
sanctioned way to get a session for request handling, so the binding cannot be
forgotten.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from fhirbridge.config import Settings
from fhirbridge.storage.base import set_privileged_sql, set_tenant_sql

logger = logging.getLogger(__name__)


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async engine.

    ``pool_pre_ping`` is on because a connection that silently died while idle
    would otherwise surface as a failed conversion rather than a retry.
    """
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        echo=False,
        future=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        class_=AsyncSession,
    )


@asynccontextmanager
async def tenant_session(
    factory: async_sessionmaker[AsyncSession], tenant_id: str
) -> AsyncIterator[AsyncSession]:
    """Yield a session with RLS bound to ``tenant_id``.

    The ``set_config(..., true)`` is transaction-local, so it cannot leak to the
    next borrower of a pooled connection.
    """
    async with factory() as session:
        await session.execute(set_tenant_sql(tenant_id))
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def privileged_session(
    factory: async_sessionmaker[AsyncSession], *, reason: str
) -> AsyncIterator[AsyncSession]:
    """Yield a session that can read across tenants, for one transaction.

    There are exactly two legitimate reasons: authenticating an API key, which
    has to find the key before the tenant is known, and the retention purge,
    which spans tenants by definition. ``reason`` is mandatory and logged so
    that every use is greppable in production, and a security test asserts that
    no request-handling path other than authentication reaches this function.
    """
    logger.debug("privileged_session", extra={"reason": reason})
    async with factory() as session:
        await session.execute(set_privileged_sql())
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


__all__ = [
    "create_engine",
    "create_session_factory",
    "privileged_session",
    "tenant_session",
]
