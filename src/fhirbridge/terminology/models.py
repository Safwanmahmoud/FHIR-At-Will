"""Value types for terminology operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class BindingStrength(StrEnum):
    """FHIR binding strengths, ordered by how hard they bite."""

    REQUIRED = "required"
    EXTENSIBLE = "extensible"
    PREFERRED = "preferred"
    EXAMPLE = "example"

    @property
    def is_blocking(self) -> bool:
        """Only ``required`` bindings block (AGENTS.md 10, L3)."""
        return self is BindingStrength.REQUIRED


@dataclass(frozen=True, slots=True)
class Coding:
    """A ``system``/``code`` pair, optionally versioned."""

    system: str | None
    code: str | None
    display: str | None = None
    version: str | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.system or "", self.code or "", self.version or "")

    def __str__(self) -> str:
        return f"{self.system}|{self.code}"


@dataclass(frozen=True, slots=True)
class ValidateCodeResult:
    """Outcome of ``$validate-code``.

    ``result`` is authoritative. When the terminology server cannot answer, the
    client raises rather than returning ``result=False``: "unknown" and "invalid"
    must never be conflated (principle 2.4).
    """

    result: bool
    coding: Coding
    value_set: str | None = None
    display: str | None = None
    message: str | None = None
    code_system_version: str | None = None
    issues: tuple[str, ...] = ()

    @property
    def in_value_set(self) -> bool | None:
        """Membership answer, or None when the check was not scoped to a ValueSet."""
        return None if self.value_set is None else self.result


@dataclass(frozen=True, slots=True)
class LookupResult:
    """Outcome of ``$lookup``."""

    coding: Coding
    name: str | None = None
    display: str | None = None
    code_system_version: str | None = None
    designations: tuple[str, ...] = ()
    properties: dict[str, Any] = field(default_factory=dict)
    inactive: bool | None = None


@dataclass(frozen=True, slots=True)
class ExpansionResult:
    """Outcome of ``$expand``."""

    value_set: str
    contains: tuple[Coding, ...]
    total: int | None = None
    offset: int | None = None
    incomplete: bool = False


class SubsumptionOutcome(StrEnum):
    EQUIVALENT = "equivalent"
    SUBSUMES = "subsumes"
    SUBSUMED_BY = "subsumed-by"
    NOT_SUBSUMED = "not-subsumed"


@dataclass(frozen=True, slots=True)
class SubsumesResult:
    """Outcome of ``$subsumes``, used for subsumption-tolerant code scoring."""

    outcome: SubsumptionOutcome
    left: Coding
    right: Coding


@dataclass(frozen=True, slots=True)
class TranslateMatch:
    equivalence: str
    concept: Coding
    source: str | None = None


@dataclass(frozen=True, slots=True)
class TranslateResult:
    """Outcome of ``$translate``."""

    result: bool
    matches: tuple[TranslateMatch, ...] = ()
    message: str | None = None


@dataclass(frozen=True, slots=True)
class CodeSystemVersion:
    """A ``system`` and the version the terminology server currently serves."""

    system: str
    version: str | None


@dataclass(frozen=True, slots=True)
class TerminologyHealth:
    reachable: bool
    software: str | None = None
    fhir_version: str | None = None
    code_systems: tuple[CodeSystemVersion, ...] = ()
    detail: str | None = None
    latency_ms: int | None = None

    @property
    def ready(self) -> bool:
        return self.reachable


__all__ = [
    "BindingStrength",
    "CodeSystemVersion",
    "Coding",
    "ExpansionResult",
    "LookupResult",
    "SubsumesResult",
    "SubsumptionOutcome",
    "TerminologyHealth",
    "TranslateMatch",
    "TranslateResult",
    "ValidateCodeResult",
]
