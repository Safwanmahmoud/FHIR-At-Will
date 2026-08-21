"""The hash-chained audit log (AGENTS.md 8.2).

The chain's whole purpose is that editing or removing a row is *detectable*.
These tests exercise that property directly: build a chain, mutate it the way an
attacker with row-level access would, and assert the replay names the row that
broke. They also pin the two things the hash must not depend on — dict ordering
and JSON whitespace — because a hash that varies with either would make chains
unverifiable on a different machine, which looks exactly like tampering.

The session is a stand-in. What is under test is the chaining arithmetic and the
redaction of ``details``, neither of which needs Postgres. The append-only
*trigger* is a database behaviour and is tested in tests/integration.
"""

from __future__ import annotations

from typing import Any

from pydantic import SecretStr

from fhirbridge.storage.audit import (
    GENESIS_HASH,
    AuditAction,
    canonical_payload,
    compute_hash,
    record_event,
    verify_chain,
)
from fhirbridge.storage.models import AuditEvent


class _Result:
    """The two result shapes :mod:`fhirbridge.storage.audit` reads.

    ``scalar_one_or_none`` answers the head-hash query; ``scalars`` answers the
    replay query. Both are served from the same list, so the stand-in cannot
    drift out of agreement with itself.
    """

    def __init__(self, rows: list[AuditEvent]) -> None:
        self._rows = rows

    def scalars(self) -> list[AuditEvent]:
        return self._rows

    def scalar_one_or_none(self) -> str | None:
        return self._rows[-1].hash if self._rows else None


class FakeSession:
    """An append-only in-memory stand-in for ``AsyncSession``.

    ``execute`` does not interpret the SQL — insertion order stands in for
    ``sequence``. What is under test is the chaining arithmetic, which does not
    need a query planner.
    """

    def __init__(self, events: list[AuditEvent] | None = None) -> None:
        self.events: list[AuditEvent] = events or []
        self.flushed = 0
        self.committed = 0

    def add(self, event: Any) -> None:
        self.events.append(event)

    async def flush(self) -> None:
        self.flushed += 1

    async def commit(self) -> None:  # pragma: no cover - audit must not commit
        self.committed += 1

    async def execute(self, statement: Any) -> _Result:
        del statement
        return _Result(list(self.events))


def _chain(session: FakeSession) -> list[AuditEvent]:
    return session.events


async def _append(session: FakeSession, action: str, **kwargs: Any) -> AuditEvent:
    return await record_event(session, tenant_id="ten_1", action=action, **kwargs)


class TestCanonicalPayload:
    def test_key_order_does_not_change_the_bytes(self) -> None:
        first = canonical_payload(
            tenant_id="ten_1",
            event_id="aud_1",
            action=AuditAction.AUTH_SUCCEEDED,
            outcome="success",
            actor_type="api_key",
            actor_id="key_1",
            subject_type=None,
            subject_id=None,
            details={"a": 1, "b": 2},
            created_at=None,
        )
        second = canonical_payload(
            tenant_id="ten_1",
            event_id="aud_1",
            action=AuditAction.AUTH_SUCCEEDED,
            outcome="success",
            actor_type="api_key",
            actor_id="key_1",
            subject_type=None,
            subject_id=None,
            details={"b": 2, "a": 1},
            created_at=None,
        )
        assert first == second

    def test_the_payload_has_no_incidental_whitespace(self) -> None:
        payload = canonical_payload(
            tenant_id="ten_1",
            event_id="aud_1",
            action="x",
            outcome="success",
            actor_type="system",
            actor_id=None,
            subject_type=None,
            subject_id=None,
            details={"a": 1},
            created_at=None,
        )
        assert b", " not in payload
        assert b": " not in payload

    def test_every_field_is_covered_by_the_hash(self) -> None:
        """A field outside the payload could be edited without breaking the chain."""
        base = {
            "tenant_id": "ten_1",
            "event_id": "aud_1",
            "action": "a",
            "outcome": "success",
            "actor_type": "system",
            "actor_id": "act_1",
            "subject_type": "conversion",
            "subject_id": "cnv_1",
            "details": {"k": "v"},
            "created_at": None,
        }
        baseline = canonical_payload(**base)  # type: ignore[arg-type]
        for field, altered in (
            ("tenant_id", "ten_2"),
            ("event_id", "aud_2"),
            ("action", "b"),
            ("outcome", "failure"),
            ("actor_type", "user"),
            ("actor_id", "act_2"),
            ("subject_type", "document"),
            ("subject_id", "doc_1"),
            ("details", {"k": "w"}),
        ):
            mutated = canonical_payload(**{**base, field: altered})  # type: ignore[arg-type]
            assert mutated != baseline, f"{field} is not covered by the hash"


class TestComputeHash:
    def test_the_hash_is_a_sha256_hex_digest(self) -> None:
        digest = compute_hash(b"payload", GENESIS_HASH)
        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")

    def test_the_previous_hash_changes_the_result(self) -> None:
        assert compute_hash(b"payload", GENESIS_HASH) != compute_hash(b"payload", "a" * 64)

    def test_the_payload_and_the_previous_hash_cannot_be_confused(self) -> None:
        """A separator prevents ``payload || prev`` from colliding with a shifted split.

        Without the delimiter, ``("ab", "c")`` and ``("a", "bc")`` would hash to
        the same value, which lets an attacker move bytes across the boundary.
        """
        assert compute_hash(b"ab", "c" * 64) != compute_hash(b"abc", "c" * 63)


class TestRecordEvent:
    async def test_the_first_event_chains_from_genesis(self) -> None:
        session = FakeSession()
        event = await _append(session, AuditAction.AUTH_SUCCEEDED)
        assert event.prev_hash == GENESIS_HASH
        assert session.flushed == 1

    async def test_each_event_chains_from_the_previous_one(self) -> None:
        session = FakeSession()
        first = await _append(session, AuditAction.AUTH_SUCCEEDED)
        second = await _append(session, AuditAction.VALIDATION_REQUESTED)
        assert second.prev_hash == first.hash
        assert second.hash != first.hash

    async def test_ids_are_distinct_per_event(self) -> None:
        session = FakeSession()
        ids = {(await _append(session, "a")).id for _ in range(5)}
        assert len(ids) == 5

    async def test_details_are_redacted_before_hashing(self) -> None:
        """Redaction happens on the way in, so the stored bytes never held the secret."""
        session = FakeSession()
        event = await _append(
            session,
            AuditAction.CREDENTIAL_CREATED,
            details={"api_key": "sk-abcdefghijklmnopqrstuvwxyz", "provider": "openai"},
        )
        assert "sk-abcdefghijklmnopqrstuvwxyz" not in str(event.details)
        assert event.details["provider"] == "openai"

    async def test_a_secretstr_in_details_does_not_survive(self) -> None:
        session = FakeSession()
        event = await _append(
            session,
            AuditAction.CREDENTIAL_ROTATED,
            details={"token": SecretStr("sk-live-do-not-log")},
        )
        assert "do-not-log" not in str(event.details)

    async def test_the_caller_transaction_is_not_committed(self) -> None:
        """An audit row and the action it describes must land together or not at all."""
        session = FakeSession()
        await _append(session, AuditAction.VALIDATION_COMPLETED)
        assert session.committed == 0


class TestVerifyChain:
    async def test_an_empty_chain_is_valid(self) -> None:
        result = await verify_chain(FakeSession(), tenant_id="ten_1")
        assert result.valid is True
        assert result.checked == 0

    async def test_an_untouched_chain_replays(self) -> None:
        session = FakeSession()
        for index in range(6):
            await _append(session, "a", subject_id=f"cnv_{index}")
        result = await verify_chain(session, tenant_id="ten_1")
        assert result.valid is True
        assert result.checked == 6

    async def test_editing_a_field_is_detected(self) -> None:
        session = FakeSession()
        for index in range(4):
            await _append(session, "a", subject_id=f"cnv_{index}")
        target = _chain(session)[2]
        target.outcome = "failure"

        result = await verify_chain(session, tenant_id="ten_1")
        assert result.valid is False
        assert result.first_broken_id == target.id
        assert result.detail is not None
        assert "recomputed hash" in result.detail

    async def test_editing_details_is_detected(self) -> None:
        session = FakeSession()
        await _append(session, "a", details={"count": 1})
        _chain(session)[0].details = {"count": 2}

        result = await verify_chain(session, tenant_id="ten_1")
        assert result.valid is False

    async def test_removing_a_row_is_detected(self) -> None:
        session = FakeSession()
        for index in range(5):
            await _append(session, "a", subject_id=f"cnv_{index}")
        removed = _chain(session).pop(2)
        orphan = _chain(session)[2]

        result = await verify_chain(session, tenant_id="ten_1")
        assert result.valid is False
        assert result.first_broken_id == orphan.id
        assert result.first_broken_id != removed.id
        assert result.detail is not None
        assert "prev_hash" in result.detail

    async def test_reordering_rows_is_detected(self) -> None:
        session = FakeSession()
        for index in range(4):
            await _append(session, "a", subject_id=f"cnv_{index}")
        chain = _chain(session)
        chain[1], chain[2] = chain[2], chain[1]

        result = await verify_chain(session, tenant_id="ten_1")
        assert result.valid is False

    async def test_a_replaced_hash_alone_does_not_repair_the_chain(self) -> None:
        """Recomputing one row's hash breaks the *next* row's ``prev_hash``.

        This is the property that forces an attacker to rewrite the entire tail,
        not just the row they wanted to change.
        """
        session = FakeSession()
        for index in range(4):
            await _append(session, "a", subject_id=f"cnv_{index}")
        target = _chain(session)[1]
        target.outcome = "failure"
        target.hash = compute_hash(
            canonical_payload(
                tenant_id=target.tenant_id,
                event_id=target.id,
                action=target.action,
                outcome=target.outcome,
                actor_type=target.actor_type,
                actor_id=target.actor_id,
                subject_type=target.subject_type,
                subject_id=target.subject_id,
                details=target.details,
                created_at=None,
            ),
            target.prev_hash or GENESIS_HASH,
        )

        result = await verify_chain(session, tenant_id="ten_1")
        assert result.valid is False
        assert result.first_broken_id == _chain(session)[2].id

    async def test_a_null_prev_hash_reads_as_genesis(self) -> None:
        """Older rows may predate the column; treat NULL as the chain start."""
        session = FakeSession()
        first = await _append(session, "a")
        first.prev_hash = None

        result = await verify_chain(session, tenant_id="ten_1")
        assert result.valid is True
