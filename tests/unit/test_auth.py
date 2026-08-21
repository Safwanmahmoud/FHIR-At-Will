"""API key hashing, verification and scope enforcement (AGENTS.md 14)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from fhirbridge.api.auth import (
    KEY_NAMESPACE,
    Principal,
    Scope,
    authenticate_api_key,
    extract_bearer,
    generate_api_key,
    parse_api_key,
)
from fhirbridge.domain.errors import ForbiddenError, UnauthenticatedError
from fhirbridge.storage.models import ApiKey


@dataclass
class _Result:
    value: Any

    def scalar_one_or_none(self) -> Any:
        return self.value


class _Session:
    """The two methods :func:`authenticate_api_key` uses, and nothing else."""

    def __init__(self, row: Any) -> None:
        self.row = row
        self.queries = 0

    async def execute(self, _statement: Any) -> _Result:
        self.queries += 1
        return _Result(self.row)


def _row(generated: Any, **overrides: Any) -> ApiKey:
    key = ApiKey(
        id=generated.key_id,
        tenant_id="ten_1",
        tenant_fk="ten_1",
        prefix=generated.prefix,
        key_hash=generated.key_hash,
        label="test",
        scopes=["facts:read"],
    )
    for name, value in overrides.items():
        setattr(key, name, value)
    return key


def test_generated_key_has_the_documented_shape() -> None:
    generated = generate_api_key()
    plaintext = generated.plaintext.get_secret_value()

    namespace, prefix, secret = plaintext.split("_", 2)
    assert namespace == KEY_NAMESPACE
    assert prefix == generated.prefix
    assert parse_api_key(plaintext) == (generated.prefix, secret)


def test_secret_is_never_stored_in_the_hash() -> None:
    generated = generate_api_key()
    assert generated.secret.get_secret_value() not in generated.key_hash
    assert generated.key_hash.startswith("$argon2id$")


def test_repr_does_not_leak_the_secret() -> None:
    """Principle 2.7: a traceback must not be able to print a credential."""
    generated = generate_api_key()
    secret = generated.secret.get_secret_value()

    assert secret not in repr(generated)
    assert secret not in str(generated)
    assert secret not in f"{generated}"
    assert generated.prefix in repr(generated)


@pytest.mark.parametrize(
    "presented",
    ["", "nonsense", "fhirb_only-two-parts", "wrongns_prefix_secret", "fhirb__secret"],
)
def test_malformed_keys_are_rejected(presented: str) -> None:
    assert parse_api_key(presented) is None


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer abc", "abc"),
        ("bearer abc", "abc"),
        ("  Bearer   abc  ", "abc"),
        ("Basic abc", None),
        ("abc", None),
        ("Bearer ", None),
        (None, None),
    ],
)
def test_bearer_extraction(header: str | None, expected: str | None) -> None:
    assert extract_bearer(header) == expected


@pytest.mark.asyncio
async def test_valid_key_authenticates() -> None:
    generated = generate_api_key()
    session = _Session(_row(generated))

    principal = await authenticate_api_key(
        session,  # type: ignore[arg-type]  # structural stub
        generated.plaintext.get_secret_value(),
    )

    assert principal.tenant_id == "ten_1"
    assert principal.actor_type == "api_key"
    assert principal.scopes == frozenset({Scope.FACTS_READ})


@pytest.mark.asyncio
async def test_wrong_secret_is_rejected() -> None:
    generated = generate_api_key()
    session = _Session(_row(generated))

    with pytest.raises(UnauthenticatedError):
        await authenticate_api_key(
            session,  # type: ignore[arg-type]
            f"{KEY_NAMESPACE}_{generated.prefix}_not-the-secret",
        )


@pytest.mark.asyncio
async def test_unknown_prefix_still_hashes() -> None:
    """A miss must do the same work as a hit, or timing discloses which keys exist."""
    session = _Session(None)

    with pytest.raises(UnauthenticatedError):
        await authenticate_api_key(session, f"{KEY_NAMESPACE}_absent_secret")  # type: ignore[arg-type]

    assert session.queries == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"revoked_at": datetime.now(UTC)},
        {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
    ],
)
async def test_revoked_and_expired_keys_are_rejected(overrides: dict[str, Any]) -> None:
    generated = generate_api_key()
    session = _Session(_row(generated, **overrides))

    with pytest.raises(UnauthenticatedError):
        await authenticate_api_key(
            session,  # type: ignore[arg-type]
            generated.plaintext.get_secret_value(),
        )


@pytest.mark.asyncio
async def test_authentication_failures_share_one_message() -> None:
    """Distinguishing the reasons would tell an attacker which prefixes are real."""
    generated = generate_api_key()
    messages = set()

    for row in (None, _row(generated, revoked_at=datetime.now(UTC))):
        session = _Session(row)
        try:
            await authenticate_api_key(
                session,  # type: ignore[arg-type]
                generated.plaintext.get_secret_value(),
            )
        except UnauthenticatedError as exc:
            messages.add(exc.detail)

    assert len(messages) == 1


@pytest.mark.asyncio
async def test_unrecognized_scopes_are_dropped_not_granted() -> None:
    """A scope this build cannot enforce must not be treated as a grant."""
    generated = generate_api_key()
    session = _Session(_row(generated, scopes=["facts:read", "everything:*"]))

    principal = await authenticate_api_key(
        session,  # type: ignore[arg-type]
        generated.plaintext.get_secret_value(),
    )

    assert principal.scopes == frozenset({Scope.FACTS_READ})


def test_require_raises_for_missing_scope() -> None:
    principal = Principal(
        tenant_id="ten_1",
        actor_type="api_key",
        actor_id="key_1",
        scopes=frozenset({Scope.FACTS_READ}),
    )

    principal.require(Scope.FACTS_READ)
    with pytest.raises(ForbiddenError) as caught:
        principal.require(Scope.DELIVERIES_WRITE)

    assert caught.value.safe_context["required"] == "deliveries:write"


def test_admin_scope_implies_the_others() -> None:
    principal = Principal(
        tenant_id="ten_1",
        actor_type="api_key",
        actor_id="key_1",
        scopes=frozenset({Scope.ADMIN}),
    )

    principal.require(Scope.DELIVERIES_WRITE, Scope.CREDENTIALS_WRITE)
    assert principal.has(Scope.PHI_READ)
