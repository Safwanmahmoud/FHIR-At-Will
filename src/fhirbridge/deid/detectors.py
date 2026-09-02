"""Deterministic, pluggable identifier detectors."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import cache, lru_cache
from pathlib import Path
from typing import Any, Final, Protocol

import yaml

from fhirbridge.deid.policy import DeidProfile
from fhirbridge.deid.spans import IdentifierClass, Span

_PACKAGE: Final = Path(__file__).parent
_RULES_PATH: Final = _PACKAGE / "rules" / "identifiers.yaml"
_DATA_PATH: Final = _PACKAGE / "data"
_WORD_RE: Final[re.Pattern[str]] = re.compile(r"\b[A-Z][A-Za-z'-]{1,}\b")


class Detector(Protocol):
    name: str
    version: str

    def detect(self, text: str) -> Sequence[Span]: ...


@dataclass(frozen=True, slots=True)
class DeclaredIdentifier:
    identifier_class: IdentifierClass
    value: str


@dataclass(slots=True)
class DeclaredIdentifierDetector:
    identifiers: Sequence[DeclaredIdentifier]
    name: str = "declared"
    version: str = "1"

    def detect(self, text: str) -> Sequence[Span]:
        spans: list[Span] = []
        for declared in self.identifiers:
            for variant in _variants(declared):
                pattern = re.compile(rf"(?<!\w){re.escape(variant)}(?:'s)?(?!\w)", re.IGNORECASE)
                spans.extend(
                    Span(
                        match.start(),
                        match.end(),
                        declared.identifier_class,
                        self.name,
                    )
                    for match in pattern.finditer(text)
                )
        return spans


@dataclass(frozen=True, slots=True)
class PatternRule:
    id: str
    identifier_class: IdentifierClass
    pattern: re.Pattern[str]


@lru_cache(maxsize=1)
def load_pattern_rules() -> tuple[str, tuple[PatternRule, ...]]:
    raw: Any = yaml.safe_load(_RULES_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("version"), int):
        raise ValueError("de-identification rule pack requires an integer version")
    items = raw.get("patterns")
    if not isinstance(items, list):
        raise ValueError("de-identification rule pack requires a patterns list")
    rules: list[PatternRule] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("each de-identification pattern must be an object")
        flags = re.IGNORECASE if "ignorecase" in item.get("flags", []) else 0
        rules.append(
            PatternRule(
                id=str(item["id"]),
                identifier_class=IdentifierClass(str(item["class"])),
                pattern=re.compile(str(item["regex"]), flags),
            )
        )
    return str(raw["version"]), tuple(rules)


@cache
def load_word_set(filename: str) -> frozenset[str]:
    lines = (_DATA_PATH / filename).read_text(encoding="utf-8").splitlines()
    return frozenset(
        line.strip().casefold()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    )


@dataclass(slots=True)
class PatternDetector:
    profile: DeidProfile
    rules: Sequence[PatternRule] = field(default_factory=lambda: load_pattern_rules()[1])
    name: str = "pattern"
    version: str = field(default_factory=lambda: load_pattern_rules()[0])

    def detect(self, text: str) -> Sequence[Span]:
        allowed = None
        if self.profile is DeidProfile.HIPAA_LIMITED_DATA_SET:
            allowed = {
                IdentifierClass.EMAIL,
                IdentifierClass.URL,
                IdentifierClass.IP,
                IdentifierClass.SSN,
                IdentifierClass.PHONE,
                IdentifierClass.MRN,
                IdentifierClass.ACCOUNT,
                IdentifierClass.LICENSE,
                IdentifierClass.DEVICE,
            }
        return [
            Span(match.start(), match.end(), rule.identifier_class, self.name)
            for rule in self.rules
            if allowed is None or rule.identifier_class in allowed
            for match in rule.pattern.finditer(text)
        ]


@dataclass(slots=True)
class GazetteerDetector:
    terms: frozenset[str] = field(default_factory=lambda: load_word_set("locations.txt"))
    name: str = "gazetteer"
    version: str = "1"

    def detect(self, text: str) -> Sequence[Span]:
        spans: list[Span] = []
        for term in sorted(self.terms, key=len, reverse=True):
            pattern = re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)
            spans.extend(
                Span(match.start(), match.end(), IdentifierClass.LOCATION, self.name)
                for match in pattern.finditer(text)
            )
        return spans


@dataclass(slots=True)
class UnknownProperNounDetector:
    allowlist: frozenset[str] = field(default_factory=lambda: load_word_set("allowlist.txt"))
    protected_phrases: frozenset[str] = field(
        default_factory=lambda: load_word_set("clinical_eponyms.txt")
    )
    name: str = "unknown_proper_noun"
    version: str = "1"

    def detect(self, text: str) -> Sequence[Span]:
        protected = _phrase_ranges(text, self.protected_phrases)
        spans: list[Span] = []
        for match in _WORD_RE.finditer(text):
            token = match.group(0).casefold()
            if token in self.allowlist or _inside(match.start(), match.end(), protected):
                continue
            spans.append(Span(match.start(), match.end(), IdentifierClass.NAME, self.name))
        return spans


def build_detectors(
    profile: DeidProfile,
    declared: Sequence[DeclaredIdentifier] = (),
) -> tuple[Detector, ...]:
    detectors: list[Detector] = [
        DeclaredIdentifierDetector(declared),
        PatternDetector(profile),
    ]
    if profile is DeidProfile.HIPAA_SAFE_HARBOR:
        detectors.extend((GazetteerDetector(), UnknownProperNounDetector()))
    return tuple(detectors)


def validate_assets() -> None:
    load_pattern_rules()
    for filename in ("locations.txt", "allowlist.txt", "clinical_eponyms.txt"):
        if not load_word_set(filename):
            raise ValueError(f"de-identification data file is empty: {filename}")


def _variants(declared: DeclaredIdentifier) -> frozenset[str]:
    value = " ".join(declared.value.split())
    variants = {value}
    if declared.identifier_class is IdentifierClass.NAME:
        parts = value.replace(",", " ").split()
        if len(parts) >= 2:
            variants.add(" ".join(reversed(parts)))
            variants.add(f"{parts[0][0]}. {parts[-1]}")
            variants.add(f"{parts[0][0]} {parts[-1]}")
    return frozenset(item for item in variants if item)


def _phrase_ranges(text: str, phrases: Iterable[str]) -> list[tuple[int, int]]:
    return [
        (match.start(), match.end())
        for phrase in phrases
        for match in re.finditer(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, re.IGNORECASE)
    ]


def _inside(start: int, end: int, ranges: Iterable[tuple[int, int]]) -> bool:
    return any(left <= start and end <= right for left, right in ranges)


__all__ = [
    "DeclaredIdentifier",
    "DeclaredIdentifierDetector",
    "Detector",
    "GazetteerDetector",
    "PatternDetector",
    "UnknownProperNounDetector",
    "build_detectors",
    "load_pattern_rules",
    "validate_assets",
]
