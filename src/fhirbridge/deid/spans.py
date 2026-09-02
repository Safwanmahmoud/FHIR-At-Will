"""Detected identifier spans and deterministic overlap resolution."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class IdentifierClass(StrEnum):
    NAME = "name"
    DATE = "date"
    AGE = "age"
    PHONE = "phone"
    EMAIL = "email"
    ADDRESS = "address"
    LOCATION = "location"
    ZIP = "zip"
    SSN = "ssn"
    MRN = "mrn"
    ACCOUNT = "account"
    LICENSE = "license"
    DEVICE = "device"
    URL = "url"
    IP = "ip"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class Span:
    start: int
    end: int
    identifier_class: IdentifierClass
    detector: str

    def __post_init__(self) -> None:
        if self.start < 0 or self.end <= self.start:
            raise ValueError("a span must have non-negative, increasing offsets")

    @property
    def length(self) -> int:
        return self.end - self.start


def resolve_overlaps(spans: Iterable[Span]) -> list[Span]:
    """Choose longest spans first, then return the non-overlapping set in text order."""
    selected: list[Span] = []
    for candidate in sorted(
        spans,
        key=lambda item: (-item.length, item.start, item.end, str(item.identifier_class)),
    ):
        if any(candidate.start < item.end and item.start < candidate.end for item in selected):
            continue
        selected.append(candidate)
    return sorted(selected, key=lambda item: item.start)


__all__ = ["IdentifierClass", "Span", "resolve_overlaps"]
