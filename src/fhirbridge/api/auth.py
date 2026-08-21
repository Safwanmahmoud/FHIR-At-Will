"""Authentication and scopes (AGENTS.md 14).

This milestone ships scoped API keys, which the brief designates for sandbox use.
OAuth2 client-credentials / SMART Backend Services JWT is the production path and
arrives with M2, behind the same :class:`Principal` abstraction so no handler has
to care which one authenticated the caller.

Key format: ``fhirb_<prefix>_<secret>``.

* ``prefix`` is indexed, so verification hashes once against one row instead of
  once per row in the table.
* ``secret`` is never stored. Only an Argon2id PHC string is.
* Verification hashes even when no row matched, so a wrong prefix and a wrong
  secret take the same time and the endpoint does not leak which keys exist.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fhirbridge.domain.errors import ForbiddenError, UnauthenticatedError
from fhirbridge.domain.ids import IdPrefix, new_id, new_ulid
from fhirbridge.storage.models import ApiKey

logger = logging.getLogger(__name__)

KEY_NAMESPACE: Final[str] = "fhirb"
_PREFIX_LENGTH: Final[int] = 8
_SECRET_BYTES: Final[int] = 32

_HASHER: Final = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)
"""Argon2id with OWASP-recommended parameters (AGENTS.md 14)."""

_DUMMY_HASH: Final[str] = _HASHER.hash("fhirbridge-constant-work-placeholder")
"""Hashed once at import so a miss and a hit cost roughly the same at request time."""


class Scope(StrEnum):
    """The scope vocabulary (AGENTS.md 14). Values are part of the public API."""

    DOCUMENTS_WRITE = "documents:write"
    CONVERSIONS_WRITE = "conversions:write"
    FACTS_READ = "facts:read"
    PHI_READ = "phi:read"
    REVIEWS_WRITE = "reviews:write"
    REVIEWS_SUBMIT = "reviews:submit"
    DELIVERIES_WRITE = "deliveries:write"
    CREDENTIALS_WRITE = "credentials:write"
    ADMIN = "admin"


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller."""

    tenant_id: str
    actor_type: str
    actor_id: str
    scopes: frozenset[Scope]
    label: str | None = None

    def has(self, scope: Scope) -> bool:
        return Scope.ADMIN in self.scopes or scope in self.scopes

    def require(self, *scopes: Scope) -> None:
        """Raise :class:`ForbiddenError` unless every scope is held."""
        missing = sorted(str(scope) for scope in scopes if not self.has(scope))
        if missing:
            raise ForbiddenError(
                "The presented credential does not carry the scope required for this operation.",
                safe_context={"required": ",".join(missing), "actor_id": self.actor_id},
            )


@dataclass(frozen=True, slots=True)
class GeneratedApiKey:
    """A freshly minted key. The plaintext exists only inside this object."""

    key_id: str
    prefix: str
    secret: SecretStr
    key_hash: str

    @property
    def plaintext(self) -> SecretStr:
        """The single time the full key is available. It is never stored."""
        return SecretStr(f"{KEY_NAMESPACE}_{self.prefix}_{self.secret.get_secret_value()}")

    def __repr__(self) -> str:
        # Custom repr so a traceback or log line cannot print the secret.
        return f"GeneratedApiKey(key_id={self.key_id!r}, prefix={self.prefix!r})"

    __str__ = __repr__


def generate_api_key() -> GeneratedApiKey:
    """Mint a new API key and its Argon2id hash."""
    prefix = new_ulid()[:_PREFIX_LENGTH].lower()
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    return GeneratedApiKey(
        key_id=new_id(IdPrefix.API_KEY),
        prefix=prefix,
        secret=SecretStr(secret),
        key_hash=_HASHER.hash(secret),
    )


def parse_api_key(presented: str) -> tuple[str, str] | None:
    """Split ``fhirb_<prefix>_<secret>``, or None when the shape is wrong."""
    parts = presented.strip().split("_", 2)
    if len(parts) != 3:
        return None
    namespace, prefix, secret = parts
    if namespace != KEY_NAMESPACE or not prefix or not secret:
        return None
    return prefix, secret


def extract_bearer(authorization: str | None) -> str | None:
    """Pull the credential out of an ``Authorization`` header."""
    if not authorization:
        return None
    scheme, _, value = authorization.strip().partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


async def authenticate_api_key(
    session: AsyncSession, presented: str, *, now: datetime | None = None
) -> Principal:
    """Verify an API key and return its :class:`Principal`.

    Every failure raises the same :class:`UnauthenticatedError` with the same
    message. Distinguishing "no such key" from "wrong secret" from "revoked"
    would tell an attacker which prefixes are real.
    """
    moment = now or datetime.now(UTC)
    parsed = parse_api_key(presented)

    record: ApiKey | None = None
    if parsed is not None:
        prefix, secret = parsed
        result = await session.execute(select(ApiKey).where(ApiKey.prefix == prefix))
        record = result.scalar_one_or_none()
    else:
        secret = presented

    stored_hash = record.key_hash if record is not None else _DUMMY_HASH
    try:
        _HASHER.verify(stored_hash, secret)
        matched = True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        matched = False

    if record is None or not matched:
        logger.info("auth_failed", extra={"reason": "invalid_credential"})
        raise UnauthenticatedError("The presented credential is not valid.")

    if record.revoked_at is not None:
        logger.info("auth_failed", extra={"reason": "revoked", "actor_id": record.id})
        raise UnauthenticatedError("The presented credential is not valid.")

    if record.expires_at is not None and record.expires_at <= moment:
        logger.info("auth_failed", extra={"reason": "expired", "actor_id": record.id})
        raise UnauthenticatedError("The presented credential is not valid.")

    return Principal(
        tenant_id=record.tenant_id,
        actor_type="api_key",
        actor_id=record.id,
        scopes=_parse_scopes(record.scopes, actor_id=record.id),
        label=record.label,
    )


def _parse_scopes(raw: object, *, actor_id: str) -> frozenset[Scope]:
    """Parse stored scopes, dropping anything unrecognized.

    An unknown scope string is dropped rather than honoured. A scope this build
    does not understand cannot be enforced, so treating it as a grant would be
    strictly worse than treating it as absent.
    """
    if not isinstance(raw, list):
        return frozenset()
    scopes: set[Scope] = set()
    for item in raw:
        try:
            scopes.add(Scope(str(item)))
        except ValueError:
            logger.warning(
                "auth_unknown_scope_ignored", extra={"actor_id": actor_id, "scope": str(item)}
            )
    return frozenset(scopes)


def hash_secret(secret: str) -> str:
    """Hash a secret for storage. Exposed for provisioning scripts and tests."""
    return _HASHER.hash(secret)


__all__ = [
    "KEY_NAMESPACE",
    "GeneratedApiKey",
    "Principal",
    "Scope",
    "authenticate_api_key",
    "extract_bearer",
    "generate_api_key",
    "hash_secret",
    "parse_api_key",
]
