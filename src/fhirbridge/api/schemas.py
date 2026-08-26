"""Request and response schemas for the M0/M1 endpoints.

Every field carries a description: these models are the OpenAPI document, which
is the contract the generated SDKs and the reviewer UI are built from.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fhirbridge.validation.models import IssueSeverity, ValidationLayer, ValidationReport

FhirResource = dict[str, Any]


class ValidateRequest(BaseModel):
    """Body of ``POST /v1/validate``.

    A bare FHIR resource (anything with a top-level ``resourceType``) is also
    accepted and is rewritten into this envelope with every option defaulted, so
    an existing FHIR client can post to this endpoint without learning a wrapper
    schema.
    """

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _accept_bare_resource(cls, value: Any) -> Any:
        if isinstance(value, dict) and "resourceType" in value and "resource" not in value:
            return {"resource": value}
        return value

    resource: FhirResource = Field(
        description=(
            "The FHIR R4 resource or Bundle to validate. Sent in the body, never in a "
            "query parameter, because it may contain PHI."
        )
    )
    profiles: list[str] = Field(
        default_factory=list,
        description=(
            "Profile canonical URLs to validate against, in addition to any declared in "
            "meta.profile. A profile the validator cannot resolve fails the request "
            "rather than passing silently."
        ),
        examples=[["http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient"]],
    )
    layers: list[ValidationLayer] | None = Field(
        default=None,
        description=(
            "Restrict the cascade to these layers. Omit to run every layer that can run. "
            "Layers you exclude are reported as skipped, never as passed."
        ),
    )
    severity_overrides: dict[str, IssueSeverity] = Field(
        default_factory=dict,
        description="Per-rule severity overrides for L5 plausibility, keyed by rule id.",
    )
    max_terminology_checks: Annotated[int, Field(ge=1, le=2000)] = Field(
        default=500,
        description=(
            "Cap on distinct $validate-code calls for L3. Reaching the cap is reported "
            "as a coverage note on the layer, not as a pass."
        ),
    )


class ConvertRequest(BaseModel):
    """Body of ``POST /v1/convert``.

    The clinical narrative is sent in the body, never a query parameter, because
    it is PHI (principle 2.6). The generated bundle is validated before it is
    returned, so the response is a report as much as a conversion.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(
        min_length=1,
        description="The clinical narrative to convert into FHIR. Sent in the body as PHI.",
    )
    profiles: list[str] = Field(
        default_factory=list,
        description=(
            "US Core (or other) profile canonical URLs to target and validate the "
            "generated bundle against."
        ),
        examples=[["http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient"]],
    )
    layers: list[ValidationLayer] | None = Field(
        default=None,
        description=(
            "Restrict the post-generation cascade to these layers. Omit to run every "
            "layer that can run. L6 fidelity and L7 coverage require source spans and "
            "remain not-applicable until M3."
        ),
    )
    max_terminology_checks: Annotated[int, Field(ge=1, le=2000)] = Field(
        default=500,
        description="Cap on distinct $validate-code calls for the L3 layer.",
    )


class CraftRequest(BaseModel):
    """Body of ``POST /v1/craft``.

    Same shape as :class:`ConvertRequest`: a clinical narrative (PHI, in the body)
    and optional profile targets plus post-assembly cascade controls. The
    difference is entirely server-side — the narrative is turned into FHIR by a
    tool-driven agent whose every edit is validated before it is kept.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(
        min_length=1,
        description="The clinical narrative to convert into FHIR. Sent in the body as PHI.",
    )
    profiles: list[str] = Field(
        default_factory=list,
        description="Profile canonical URLs to target and validate the assembled bundle against.",
        examples=[["http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient"]],
    )
    layers: list[ValidationLayer] | None = Field(
        default=None,
        description=(
            "Restrict the post-assembly cascade to these layers. Omit to run every "
            "layer that can run."
        ),
    )
    max_terminology_checks: Annotated[int, Field(ge=1, le=2000)] = Field(
        default=500,
        description="Cap on distinct $validate-code calls for the L3 layer.",
    )


class CraftToolCall(BaseModel):
    """One step in the agent's trace: which tool ran and whether it committed."""

    model_config = ConfigDict(extra="forbid")

    iteration: int
    tool: str | None = None
    ok: bool | None = None
    finish: bool | None = None
    error: str | None = Field(
        default=None, description="Why a tool refused the edit, when it did. May quote values."
    )
    event: str | None = Field(
        default=None, description="A loop event with no tool, e.g. 'budget_exhausted'."
    )


class LlmCallInfo(BaseModel):
    """Provenance for one model call. No prompt or completion content (principle 2.6)."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str = Field(description="The model id the provider reported answering with.")
    usage: dict[str, int] = Field(
        default_factory=dict, description="Token counts reported by the provider, when available."
    )
    cost_usd: float | None = Field(
        default=None, description="Provider-reported cost of this call, when pricing is known."
    )
    latency_ms: int = 0
    qualification_tier: str = Field(description="This build's qualification tier for the model.")


class ConvertResponse(BaseModel):
    """Body of ``POST /v1/convert``."""

    model_config = ConfigDict(extra="forbid")

    conversion_id: str = Field(description="Opaque id for this conversion; also on the report.")
    bundle: FhirResource = Field(description="The generated FHIR R4 Bundle.")
    report: ValidationReport = Field(
        description="The L1-L5 validation of the generated bundle. Read status before trusting it."
    )
    llm: LlmCallInfo


class CraftResponse(BaseModel):
    """Body of ``POST /v1/craft``."""

    model_config = ConfigDict(extra="forbid")

    conversion_id: str = Field(description="Opaque id for this conversion; also on the report.")
    bundle: FhirResource = Field(
        description="The FHIR R4 Bundle the agent assembled through validated tool edits."
    )
    report: ValidationReport = Field(
        description="The L1-L5 validation of the assembled bundle. Read status before trusting it."
    )
    llm: LlmCallInfo = Field(
        description="Aggregate provenance across every model call the agent made."
    )
    trace: list[CraftToolCall] = Field(
        default_factory=list,
        description="Ordered record of each tool the agent ran and whether it was accepted.",
    )
    iterations: int = Field(description="Tool-calling turns the agent took.")
    stop_reason: str = Field(
        description="Why the loop ended: finished, max_iterations, budget_exhausted, or "
        "no_tool_calls."
    )
    toolset_version: str = Field(description="Version of the deterministic tool set that ran.")


class LlmProbeResponse(BaseModel):
    """Body of ``POST /v1/llm/probe`` — a connectivity and credential check."""

    model_config = ConfigDict(extra="forbid")

    ok: bool = Field(description="True when the provider answered the probe.")
    provider: str
    model: str
    qualification_tier: str
    latency_ms: int
    cost_usd: float | None = None
    sample: str | None = Field(
        default=None, description="A short, non-PHI excerpt of the probe completion."
    )


class ValidateCodeRequest(BaseModel):
    """Body of ``POST /v1/terminology/validate-code``."""

    model_config = ConfigDict(extra="forbid")

    system: str | None = Field(
        default=None,
        description="CodeSystem canonical URL. Required unless value_set is given.",
        examples=["http://snomed.info/sct"],
    )
    code: str = Field(description="The code to validate.", examples=["49436004"])
    display: str | None = Field(
        default=None, description="Display to check against the server's designations."
    )
    version: str | None = Field(default=None, description="CodeSystem version.")
    value_set: str | None = Field(
        default=None,
        description=(
            "ValueSet canonical URL. When supplied, membership is checked as well as "
            "code existence."
        ),
    )


class ValidateCodeResponse(BaseModel):
    """Result of one ``$validate-code`` call, as answered by the terminology server."""

    model_config = ConfigDict(extra="forbid")

    result: bool = Field(description="True only if the terminology server confirmed the code.")
    system: str | None = None
    code: str
    display: str | None = Field(
        default=None, description="The server's preferred display for the code."
    )
    value_set: str | None = None
    code_system_version: str | None = Field(
        default=None, description="The CodeSystem version the server answered from."
    )
    message: str | None = None
    issues: list[str] = Field(default_factory=list)


class TerminologySearchRequest(BaseModel):
    """Body of ``POST /v1/terminology/search``."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, description="Text to search for.")
    system: str | None = Field(
        default=None,
        description="CodeSystem canonical URL. Required unless value_set is given.",
    )
    value_set: str | None = Field(
        default=None,
        description="ValueSet canonical URL to search instead of an entire CodeSystem.",
    )
    count: Annotated[int, Field(ge=1, le=100)] = Field(
        default=10,
        description="Maximum number of candidates to return.",
    )


class TerminologySearchCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system: str | None = None
    code: str
    display: str | None = None


class TerminologySearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[TerminologySearchCandidate] = Field(default_factory=list)


class TerminologyMapRequest(BaseModel):
    """Body of ``POST /v1/terminology/map`` (a ``$translate`` passthrough)."""

    model_config = ConfigDict(extra="forbid")

    system: str = Field(description="Source CodeSystem canonical URL.")
    code: str = Field(description="Source code.")
    target_system: str | None = Field(default=None, description="Target CodeSystem canonical URL.")
    concept_map: str | None = Field(default=None, description="ConceptMap canonical URL to use.")


class TerminologyMapMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    equivalence: str = Field(description="FHIR concept-map-equivalence / relationship code.")
    system: str | None = None
    code: str | None = None
    display: str | None = None
    version: str | None = None
    source: str | None = Field(default=None, description="The ConceptMap that produced the match.")


class TerminologyMapResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: bool
    matches: list[TerminologyMapMatch] = Field(default_factory=list)
    message: str | None = None


class VersionResponse(BaseModel):
    """Body of ``GET /version`` — the pins required by principle 2.8."""

    model_config = ConfigDict(extra="forbid")

    service: str
    version: str = Field(description="fhirbridge code version.")
    fhir_version: str
    typed_model_fhir_version: str = Field(
        description="FHIR version of the typed models used for L1 structural validation."
    )
    prompt_set_version: str
    fact_schema_version: str
    validation_report_schema_version: str
    ig_packages: list[str]
    validator_version: str | None = None
    environment: str


class DependencyStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    status: Literal["up", "down", "degraded"]
    detail: str | None = None
    latency_ms: int | None = None
    version: str | None = None


class DependencyHealthResponse(BaseModel):
    """Body of ``GET /v1/health/dependencies``."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["up", "down", "degraded"] = Field(
        description=(
            "Worst status across dependencies. 'degraded' means the service is reachable "
            "but cannot make conformance claims, e.g. a required IG is not loaded."
        )
    )
    dependencies: list[DependencyStatus]


class ReadyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ready: bool
    dependencies: list[DependencyStatus] = Field(default_factory=list)


class LiveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"


class CapabilitiesResponse(BaseModel):
    """Body of ``GET /v1/capabilities`` — what this build can actually do."""

    model_config = ConfigDict(extra="forbid")

    service: str
    version: str
    fhir_versions: list[str]
    ig_packages: list[str]
    validation_layers: list[dict[str, Any]]
    endpoints_implemented: list[str]
    endpoints_not_implemented: list[str]
    llm_required: bool = Field(
        description="False for this build: no endpoint here calls an LLM (milestone M1)."
    )
    local_only_mode: bool
    credential_storage: str
    min_qualification_tier: str


__all__ = [
    "CapabilitiesResponse",
    "ConvertRequest",
    "ConvertResponse",
    "CraftRequest",
    "CraftResponse",
    "CraftToolCall",
    "DependencyHealthResponse",
    "DependencyStatus",
    "LiveResponse",
    "LlmCallInfo",
    "LlmProbeResponse",
    "ReadyResponse",
    "TerminologyMapMatch",
    "TerminologyMapRequest",
    "TerminologyMapResponse",
    "TerminologySearchCandidate",
    "TerminologySearchRequest",
    "TerminologySearchResponse",
    "ValidateCodeRequest",
    "ValidateCodeResponse",
    "ValidateRequest",
    "VersionResponse",
]
