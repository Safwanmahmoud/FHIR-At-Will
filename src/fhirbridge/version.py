"""Version pins that are stamped onto every artifact.

Principle 2.8 requires every artifact to record the full version set. These
constants are the single source of truth for the non-runtime half of that set;
runtime-discovered versions (validator build, terminology CodeSystem versions)
are read from the sidecars at request time.
"""

from __future__ import annotations

from typing import Final

from fhirbridge import __version__

CODE_VERSION: Final[str] = __version__
"""Version of this service's code, stamped into every validation report."""

PROMPT_SET_VERSION: Final[str] = "v2.1.0"
"""Version of the hash-pinned prompt template set, stamped into conversion reports."""

AGENT_TOOLSET_VERSION: Final[str] = "v1.0.0"
"""Version of the craft agent's deterministic tool set (``POST /v1/craft``).

Like the prompt set, the tools are a pinned artifact: a verdict produced by the
agent names the toolset that produced it, so a change to a tool's behaviour must
move this version.
"""

FACT_SCHEMA_VERSION: Final[str] = "v1"
"""Version of the canonical `Fact` schema (AGENTS.md 9.2), unused until M3."""

VALIDATION_REPORT_SCHEMA_VERSION: Final[str] = "v1"
"""Version of the validation report envelope returned by ``POST /v1/validate``."""

SUPPORTED_FHIR_VERSIONS: Final[tuple[str, ...]] = ("4.0.1",)
"""FHIR versions this build accepts. R4 only; see docs/adr/0004."""

TYPED_MODEL_FHIR_VERSION: Final[str] = "4.3.0"
"""FHIR version of the ``fhir.resources`` typed models used for L1.

``fhir.resources`` 8.x ships R4B (4.3.0) and R5, but not R4 (4.0.1). L1 uses the
R4B models constrained to the R4 resource-type allowlist; L2 (the HAPI validator
sidecar, run with ``-version 4.0.1``) is the authoritative R4 conformance check.
See docs/adr/0004 and OPEN_QUESTIONS.md#Q1.
"""

__all__ = [
    "AGENT_TOOLSET_VERSION",
    "CODE_VERSION",
    "FACT_SCHEMA_VERSION",
    "PROMPT_SET_VERSION",
    "SUPPORTED_FHIR_VERSIONS",
    "TYPED_MODEL_FHIR_VERSION",
    "VALIDATION_REPORT_SCHEMA_VERSION",
]
