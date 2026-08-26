"""The :class:`TerminologyClient` protocol (AGENTS.md 4).

Everything in the codebase depends on this protocol, never on a concrete server.
Ontoserver, Snowstorm, HAPI JPA and tx.fhir.org differ in authentication,
supported operations and error shapes; those differences belong in the adapter.

Implementations MUST fail closed: when the server cannot answer, raise
:class:`~fhirbridge.domain.errors.TerminologyUnavailableError`. Returning
``result=False`` for an unanswerable question would let unvalidated codes
through, which violates principle 2.3.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from fhirbridge.terminology.models import (
    ExpansionResult,
    LookupResult,
    SubsumesResult,
    TerminologyHealth,
    TranslateResult,
    ValidateCodeResult,
)

_SEARCH_VALUE_SETS = {
    # FHIR's generic ``?fhir_vs`` implicit ValueSet is not implemented by every
    # server. LOINC publishes this canonical whole-code-system ValueSet.
    "http://loinc.org": "http://loinc.org/vs",
}


def search_value_set_for_system(system: str) -> str:
    """Return a searchable ValueSet canonical for an entire CodeSystem."""
    return _SEARCH_VALUE_SETS.get(system, f"{system}?fhir_vs")


@runtime_checkable
class TerminologyClient(Protocol):
    """A FHIR terminology server, reduced to the five operations we need."""

    async def validate_code(
        self,
        *,
        system: str | None,
        code: str,
        display: str | None = None,
        version: str | None = None,
        value_set: str | None = None,
    ) -> ValidateCodeResult:
        """Confirm a code exists, and belongs to ``value_set`` when given.

        This is the gate in principle 2.3: no ``Coding`` may be emitted unless
        this returns ``result=True``.
        """
        ...

    async def lookup(self, *, system: str, code: str, version: str | None = None) -> LookupResult:
        """Fetch the server's display and properties for a code."""
        ...

    async def expand(
        self,
        *,
        value_set: str,
        filter_text: str | None = None,
        count: int | None = None,
        offset: int | None = None,
    ) -> ExpansionResult:
        """Expand a ValueSet, optionally text-filtered, for candidate retrieval."""
        ...

    async def subsumes(
        self,
        *,
        system: str,
        code_a: str,
        code_b: str,
        version: str | None = None,
    ) -> SubsumesResult:
        """Test the subsumption relationship between two codes in one system."""
        ...

    async def translate(
        self,
        *,
        system: str,
        code: str,
        target_system: str | None = None,
        concept_map: str | None = None,
    ) -> TranslateResult:
        """Translate a code via a ConceptMap."""
        ...

    async def health(self, *, code_systems: Sequence[str] = ()) -> TerminologyHealth:
        """Probe reachability and report served CodeSystem versions."""
        ...

    async def aclose(self) -> None:
        """Release transport resources. Implementations own a connection pool."""
        ...


__all__ = ["TerminologyClient", "search_value_set_for_system"]
