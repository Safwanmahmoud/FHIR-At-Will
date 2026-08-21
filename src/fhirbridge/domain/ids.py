"""Prefixed, sortable public identifiers.

Every externally visible id is ``<prefix>_<ULID>``: ``cnv_01J9Z...``. Three
properties matter:

* **Prefixed.** A support ticket quoting ``cred_01J...`` is unambiguous, and a
  caller cannot accidentally pass a document id where a conversion id belongs.
* **Sortable.** ULIDs sort lexicographically by creation time, which makes them
  usable as cursor pagination keys without a separate sort column.
* **Opaque and PHI-free.** Ids appear in URLs, logs and metrics, where
  principle 2.6 forbids anything patient-identifying. They encode a timestamp
  and randomness, nothing else.

ULID is implemented here rather than pulled in as a dependency: it is 30 lines,
and the encoding is part of our API contract.
"""

from __future__ import annotations

import re
import secrets
import time
from enum import StrEnum
from typing import Final

_CROCKFORD: Final[str] = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE: Final[dict[str, int]] = {char: index for index, char in enumerate(_CROCKFORD)}
_ULID_LENGTH: Final[int] = 26
_TIMESTAMP_BITS: Final[int] = 48
_RANDOM_BITS: Final[int] = 80


class IdPrefix(StrEnum):
    """The prefix vocabulary. Values are part of the public API."""

    TENANT = "ten"
    USER = "usr"
    API_KEY = "key"
    CREDENTIAL = "cred"
    MODEL_PROFILE = "mdl"
    QUALIFICATION = "qual"
    DOCUMENT = "doc"
    CONVERSION = "cnv"
    STAGE = "stg"
    FACT = "fact"
    BUNDLE = "bdl"
    VALIDATION_REPORT = "vr"
    REVIEW = "rev"
    DECISION = "dec"
    PROVENANCE = "prv"
    AUDIT = "aud"
    DELIVERY = "dlv"
    TARGET = "tgt"
    POLICY = "pol"
    GOLDSET = "gs"
    EVALUATION = "eval"
    LLM_CALL = "call"
    WEBHOOK = "wh"
    BATCH = "batch"
    REQUEST = "req"
    JOB = "job"


def new_ulid(*, now_ms: int | None = None) -> str:
    """Generate a 26-character Crockford base32 ULID."""
    timestamp = now_ms if now_ms is not None else int(time.time() * 1000)
    value = (timestamp << _RANDOM_BITS) | secrets.randbits(_RANDOM_BITS)
    digits = []
    for _ in range(_ULID_LENGTH):
        digits.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(digits))


def new_id(prefix: IdPrefix, *, now_ms: int | None = None) -> str:
    """Generate a prefixed public identifier."""
    return f"{prefix}_{new_ulid(now_ms=now_ms)}"


_ID_PATTERN: Final = re.compile(rf"^(?P<prefix>[a-z]{{2,6}})_(?P<ulid>[{_CROCKFORD}]{{26}})$")


def parse_id(value: str) -> tuple[str, str]:
    """Split ``prefix_ulid``, raising :class:`ValueError` if malformed."""
    match = _ID_PATTERN.match(value)
    if match is None:
        raise ValueError("identifier must look like 'prefix_ULID'")
    return match["prefix"], match["ulid"]


def is_valid_id(value: str, prefix: IdPrefix | None = None) -> bool:
    """Validate an identifier's shape, and optionally its prefix."""
    try:
        found, _ = parse_id(value)
    except ValueError:
        return False
    return prefix is None or found == str(prefix)


def timestamp_ms(value: str) -> int:
    """Recover the creation timestamp encoded in an id or bare ULID."""
    ulid = value.split("_", 1)[1] if "_" in value else value
    if len(ulid) != _ULID_LENGTH:
        raise ValueError("not a ULID")
    decoded = 0
    for char in ulid:
        digit = _DECODE.get(char.upper())
        if digit is None:
            raise ValueError(f"invalid ULID character {char!r}")
        decoded = (decoded << 5) | digit
    return decoded >> _RANDOM_BITS


__all__ = [
    "IdPrefix",
    "is_valid_id",
    "new_id",
    "new_ulid",
    "parse_id",
    "timestamp_ms",
]
