"""The hash-chained audit log (AGENTS.md 8.2).

Each row's hash covers its own canonical content *and* the previous row's hash,
so removing or editing a row breaks every hash after it. :func:`verify_chain`
replays a chain and reports the first break.

What this does and does not give you: it makes tampering *evident* to anyone who
replays the chain. It does not make tampering impossible — an attacker with
write access to the whole table could recompute every subsequent hash. Detecting
that requires anchoring the head hash somewhere the attacker does not control
(an append-only object store, a WORM bucket, a signed periodic export). That is
a deployment responsibility and is documented in docs/security.md.

``details`` must never contain PHI or secrets (principles 2.6, 2.7). The writer
redacts values defensively, but the real control is not putting them there.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fhirbridge.domain.ids import IdPrefix, new_id
from fhirbridge.observability.context import get_trace_id
from fhirbridge.observability.redaction import redact_object
from fhirbridge.storage.models import AuditEvent

logger = logging.getLogger(__name__)

GENESIS_HASH: Final[str] = "0" * 64
"""The ``prev_hash`` stand-in for the first event in a tenant's chain."""


class AuditAction:
    """Audit action vocabulary. Extend as endpoints are added."""

    AUTH_SUCCEEDED = "auth.succeeded"
    AUTH_FAILED = "auth.failed"
    AUTH_SCOPE_DENIED = "auth.scope_denied"
    VALIDATION_REQUESTED = "validation.requested"
    VALIDATION_COMPLETED = "validation.completed"
    VALIDATION_FAILED_CLOSED = "validation.failed_closed"
    TERMINOLOGY_QUERIED = "terminology.queried"
    CREDENTIAL_CREATED = "credential.created"
    CREDENTIAL_DELETED = "credential.deleted"
    CREDENTIAL_ROTATED = "credential.rotated"
    PHI_EGRESS_ACKNOWLEDGED = "phi_egress.acknowledged"
    RETENTION_PURGED = "retention.purged"


@dataclass(frozen=True, slots=True)
class ChainVerification:
    """Result of replaying an audit chain."""

    valid: bool
    checked: int
    first_broken_id: str | None = None
    detail: str | None = None


def canonical_payload(
    *,
    tenant_id: str,
    event_id: str,
    action: str,
    outcome: str,
    actor_type: str,
    actor_id: str | None,
    subject_type: str | None,
    subject_id: str | None,
    details: dict[str, Any],
    created_at: datetime | None,
) -> bytes:
    """Serialize an event deterministically for hashing.

    Sorted keys and a fixed separator set mean the hash does not depend on dict
    ordering or JSON whitespace, so a chain verified on one machine verifies on
    another.
    """
    document = {
        "tenant_id": tenant_id,
        "id": event_id,
        "action": action,
        "outcome": outcome,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "details": details,
        "created_at": created_at.isoformat() if created_at else None,
    }
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")


def compute_hash(payload: bytes, prev_hash: str) -> str:
    digest = hashlib.sha256()
    digest.update(payload)
    digest.update(b"\x00")
    digest.update(prev_hash.encode("ascii"))
    return digest.hexdigest()


async def _head_hash(session: AsyncSession, tenant_id: str) -> str:
    statement = (
        select(AuditEvent.hash)
        .where(AuditEvent.tenant_id == tenant_id)
        .order_by(AuditEvent.sequence.desc())
        .limit(1)
    )
    result = await session.execute(statement)
    return result.scalar_one_or_none() or GENESIS_HASH


async def record_event(
    session: AsyncSession,
    *,
    tenant_id: str,
    action: str,
    outcome: str = "success",
    actor_type: str = "system",
    actor_id: str | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    details: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> AuditEvent:
    """Append one event to the tenant's chain.

    The caller's transaction is not committed here: an audit record and the
    action it describes must land together or not at all.
    """
    safe_details = redact_object(details or {})
    assert isinstance(safe_details, dict)

    event_id = new_id(IdPrefix.AUDIT)
    prev_hash = await _head_hash(session, tenant_id)
    payload = canonical_payload(
        tenant_id=tenant_id,
        event_id=event_id,
        action=action,
        outcome=outcome,
        actor_type=actor_type,
        actor_id=actor_id,
        subject_type=subject_type,
        subject_id=subject_id,
        details=safe_details,
        created_at=None,
    )
    event = AuditEvent(
        id=event_id,
        tenant_id=tenant_id,
        action=action,
        outcome=outcome,
        actor_type=actor_type,
        actor_id=actor_id,
        subject_type=subject_type,
        subject_id=subject_id,
        trace_id=trace_id or get_trace_id(),
        details=safe_details,
        prev_hash=prev_hash,
        hash=compute_hash(payload, prev_hash),
    )
    session.add(event)
    await session.flush()
    return event


async def verify_chain(
    session: AsyncSession, *, tenant_id: str, limit: int | None = None
) -> ChainVerification:
    """Replay a tenant's chain and report the first break."""
    statement = (
        select(AuditEvent)
        .where(AuditEvent.tenant_id == tenant_id)
        .order_by(AuditEvent.sequence.asc())
    )
    if limit is not None:
        statement = statement.limit(limit)

    expected_prev = GENESIS_HASH
    checked = 0
    for event in (await session.execute(statement)).scalars():
        checked += 1
        if (event.prev_hash or GENESIS_HASH) != expected_prev:
            return ChainVerification(
                valid=False,
                checked=checked,
                first_broken_id=event.id,
                detail="prev_hash does not match the preceding event's hash",
            )
        payload = canonical_payload(
            tenant_id=event.tenant_id,
            event_id=event.id,
            action=event.action,
            outcome=event.outcome,
            actor_type=event.actor_type,
            actor_id=event.actor_id,
            subject_type=event.subject_type,
            subject_id=event.subject_id,
            details=event.details,
            created_at=None,
        )
        if compute_hash(payload, expected_prev) != event.hash:
            return ChainVerification(
                valid=False,
                checked=checked,
                first_broken_id=event.id,
                detail="recomputed hash does not match the stored hash",
            )
        expected_prev = event.hash

    return ChainVerification(valid=True, checked=checked)


__all__ = [
    "GENESIS_HASH",
    "AuditAction",
    "ChainVerification",
    "canonical_payload",
    "compute_hash",
    "record_event",
    "verify_chain",
]
