"""Request and response schemas for the M0/M1 endpoints.

Every field carries a description: these models are the OpenAPI document, which
is the contract clients use to validate requests and responses.
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
    """Body of ``POST /v1/NAR2FHIR``.

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


class LlmCallInfo(BaseModel):
    """Aggregate model-call provenance. No prompt or completion content (principle 2.6)."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str = Field(description="The model id reported by the final generation call.")
    usage: dict[str, int] = Field(
        default_factory=dict,
        description="Token counts aggregated across the endpoint's calls, when available.",
    )
    cost_usd: float | None = Field(
        default=None, description="Provider-reported cost of this call, when pricing is known."
    )
    latency_ms: int = 0
    qualification_tier: str = Field(description="This build's qualification tier for the model.")


class ConvertResponse(BaseModel):
    """Body of ``POST /v1/NAR2FHIR``."""

    model_config = ConfigDict(extra="forbid")

    conversion_id: str = Field(description="Opaque id for this conversion; also on the report.")
    bundle: FhirResource = Field(description="The generated FHIR R4 Bundle.")
    report: ValidationReport = Field(
        description="The L1-L5 validation of the generated bundle. Read status before trusting it."
    )
    llm: LlmCallInfo


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
    "DependencyHealthResponse",
    "DependencyStatus",
    "LiveResponse",
    "LlmCallInfo",
    "ReadyResponse",
    "ValidateRequest",
    "VersionResponse",
]
