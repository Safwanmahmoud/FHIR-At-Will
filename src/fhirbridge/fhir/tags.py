"""Provenance tags stamped onto generated resources (AGENTS.md 9.1 assemble).

Downstream consumers need to be able to tell, from the resource alone, that it
was machine-derived and how much scrutiny it has had. ``docs/clinical-safety.md``
documents the semantics these tags promise.
"""

from __future__ import annotations

from typing import Final

PROVENANCE_TAG_SYSTEM: Final[str] = "https://fhirbridge.org/CodeSystem/provenance-tags"

AI_DERIVED: Final[str] = "ai-derived"
"""The resource's content was proposed by a language model, not a human."""

HUMAN_REVIEWED: Final[str] = "human-reviewed"
"""A human reviewer accepted this resource through the review plane."""

UNQUALIFIED_MODEL: Final[str] = "unqualified-model"
"""Produced with a model below MIN_QUALIFICATION_TIER, acknowledged by the caller."""

NONDETERMINISM_RISK: Final[str] = "nondeterminism-risk"
"""Reproducibility could not be guaranteed for this resource (AGENTS.md 7.10)."""

ALL_TAGS: Final[tuple[str, ...]] = (
    AI_DERIVED,
    HUMAN_REVIEWED,
    UNQUALIFIED_MODEL,
    NONDETERMINISM_RISK,
)


def tag(code: str, display: str | None = None) -> dict[str, str]:
    """Build a ``meta.tag`` Coding."""
    coding = {"system": PROVENANCE_TAG_SYSTEM, "code": code}
    if display:
        coding["display"] = display
    return coding


__all__ = [
    "AI_DERIVED",
    "ALL_TAGS",
    "HUMAN_REVIEWED",
    "NONDETERMINISM_RISK",
    "PROVENANCE_TAG_SYSTEM",
    "UNQUALIFIED_MODEL",
    "tag",
]
