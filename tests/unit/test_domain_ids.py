"""Public identifiers (AGENTS.md 8, principle 2.6).

Ids appear in URLs, logs and metric labels, so the properties asserted here are
security-relevant, not cosmetic: they must be opaque, prefixed, and sortable by
creation time so they can be used as cursor keys without a second column.
"""

from __future__ import annotations

import time

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fhirbridge.domain.ids import (
    IdPrefix,
    is_valid_id,
    new_id,
    new_ulid,
    parse_id,
    timestamp_ms,
)

_MAX_ULID_MS = (1 << 48) - 1


def test_an_id_is_its_prefix_and_a_ulid() -> None:
    value = new_id(IdPrefix.CONVERSION)

    prefix, ulid = parse_id(value)
    assert prefix == "cnv"
    assert len(ulid) == 26


def test_every_prefix_produces_a_parseable_id() -> None:
    """The regex bounds the prefix length, so a new long prefix must not break it."""
    for prefix in IdPrefix:
        value = new_id(prefix)
        assert is_valid_id(value, prefix), value


def test_ids_are_unique() -> None:
    values = {new_id(IdPrefix.FACT) for _ in range(2000)}

    assert len(values) == 2000


def test_prefix_checking_rejects_the_wrong_kind_of_id() -> None:
    """A caller passing a document id where a conversion id belongs is a bug we
    want to catch at the boundary, not a cross-tenant lookup."""
    document = new_id(IdPrefix.DOCUMENT)

    assert is_valid_id(document, IdPrefix.DOCUMENT)
    assert not is_valid_id(document, IdPrefix.CONVERSION)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "cnv",
        "cnv_",
        "_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "cnv_01ARZ3NDEKTSV4RRFFQ69G5FA",  # 25 chars
        "cnv_01ARZ3NDEKTSV4RRFFQ69G5FAVV",  # 27 chars
        "CNV_01ARZ3NDEKTSV4RRFFQ69G5FAV",  # upper-case prefix
        "cnv_01arz3ndektsv4rrffq69g5fav",  # lower-case ULID
        "conversion_01ARZ3NDEKTSV4RRFFQ69G5FAV",  # prefix too long
        "cnv_01ARZ3NDEKTSV4RRFFQ69G5FAU",  # U is excluded from Crockford base32
    ],
)
def test_malformed_identifiers_are_rejected(value: str) -> None:
    assert not is_valid_id(value)
    with pytest.raises(ValueError):
        parse_id(value)


def test_the_encoding_excludes_the_confusable_letters() -> None:
    """Crockford base32 drops I, L, O and U so a transcribed id cannot be
    misread. A support ticket quoting an id has to be actionable."""
    alphabet = {char for _ in range(200) for char in new_ulid()}

    assert not alphabet & set("ILOU")


class TestSortability:
    def test_ids_from_later_timestamps_sort_later(self) -> None:
        earlier = new_id(IdPrefix.CONVERSION, now_ms=1_700_000_000_000)
        later = new_id(IdPrefix.CONVERSION, now_ms=1_700_000_001_000)

        assert earlier < later

    def test_ids_generated_in_sequence_are_non_decreasing(self) -> None:
        values = [new_ulid(now_ms=1_700_000_000_000 + index) for index in range(500)]

        assert values == sorted(values)

    def test_the_current_time_is_recoverable_from_a_fresh_id(self) -> None:
        before = int(time.time() * 1000)
        value = new_id(IdPrefix.AUDIT)
        after = int(time.time() * 1000)

        assert before <= timestamp_ms(value) <= after


class TestTimestampRoundTrip:
    @given(st.integers(min_value=0, max_value=_MAX_ULID_MS))
    def test_the_timestamp_survives_the_round_trip(self, now_ms: int) -> None:
        assert timestamp_ms(new_ulid(now_ms=now_ms)) == now_ms

    @given(st.integers(min_value=0, max_value=_MAX_ULID_MS), st.sampled_from(list(IdPrefix)))
    def test_the_prefix_does_not_disturb_the_timestamp(self, now_ms: int, prefix: IdPrefix) -> None:
        assert timestamp_ms(new_id(prefix, now_ms=now_ms)) == now_ms

    def test_a_bare_ulid_is_accepted(self) -> None:
        assert timestamp_ms(new_ulid(now_ms=42)) == 42

    def test_lower_case_is_decoded(self) -> None:
        ulid = new_ulid(now_ms=1_700_000_000_000)

        assert timestamp_ms(ulid.lower()) == 1_700_000_000_000

    @pytest.mark.parametrize("value", ["short", "cnv_short", "U" * 26, "i" * 26, "!" * 26])
    def test_a_non_ulid_raises(self, value: str) -> None:
        with pytest.raises(ValueError):
            timestamp_ms(value)


@given(st.integers(min_value=0, max_value=_MAX_ULID_MS))
def test_every_generated_id_is_valid_by_construction(now_ms: int) -> None:
    value = new_id(IdPrefix.CONVERSION, now_ms=now_ms)

    assert is_valid_id(value, IdPrefix.CONVERSION)


def test_prefix_values_are_stable() -> None:
    """These strings are part of the public API. Changing one breaks every
    client that stored an id, so pin the ones already in the documentation."""
    assert IdPrefix.CONVERSION == "cnv"
    assert IdPrefix.DOCUMENT == "doc"
    assert IdPrefix.CREDENTIAL == "cred"
    assert IdPrefix.QUALIFICATION == "qual"
    assert IdPrefix.FACT == "fact"
    assert IdPrefix.LLM_CALL == "call"
