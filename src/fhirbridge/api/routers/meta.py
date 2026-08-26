"""Self-description endpoints: capabilities, loaded IGs, the error CodeSystem.

``GET /v1/capabilities`` deliberately lists what is *not* implemented as well as
what is. A client that has to discover missing endpoints by probing for 404s
cannot tell "not built yet" from "misconfigured deployment".
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Response

from fhirbridge.api.deps import PrincipalDep, SettingsDep
from fhirbridge.api.schemas import CapabilitiesResponse
from fhirbridge.domain.errors import error_code_system
from fhirbridge.fhir.operation_outcome import FHIR_JSON_MEDIA_TYPE
from fhirbridge.validation.models import CASCADE_ORDER, ValidationLayer
from fhirbridge.version import CODE_VERSION, SUPPORTED_FHIR_VERSIONS

router = APIRouter(prefix="/v1", tags=["platform"])

_LAYER_DESCRIPTION: dict[ValidationLayer, str] = {
    ValidationLayer.STRUCTURAL: "Typed-model round-trip and resource-type allowlist.",
    ValidationLayer.PROFILE: "Profile conformance via the validator sidecar.",
    ValidationLayer.TERMINOLOGY: "$validate-code per Coding plus binding-strength checks.",
    ValidationLayer.INVARIANTS: "FHIRPath invariants evaluated by the validator sidecar.",
    ValidationLayer.PLAUSIBILITY: "Rule pack: impossible values, date ordering, dose magnitude.",
    ValidationLayer.FIDELITY: "Entailment of each element against its source spans.",
    ValidationLayer.COVERAGE: "Clinical mentions in the source that the bundle omits.",
    ValidationLayer.ROUTING: "Calibrated confidence and critical-domain rules.",
}

_AVAILABLE_FROM_M3: frozenset[ValidationLayer] = frozenset(
    {ValidationLayer.FIDELITY, ValidationLayer.COVERAGE}
)

IMPLEMENTED_ENDPOINTS: list[str] = [
    "GET /livez",
    "GET /readyz",
    "GET /version",
    "GET /metrics",
    "GET /v1/health/dependencies",
    "GET /v1/capabilities",
    "GET /v1/igs",
    "GET /v1/error-codes",
    "POST /v1/validate",
    "POST /v1/validate/outcome",
    "POST /v1/convert",
    "POST /v1/craft",
    "POST /v1/craft/stream",
    "POST /v1/llm/probe",
    "POST /v1/terminology/search",
    "POST /v1/terminology/validate-code",
    "POST /v1/terminology/map",
    "GET /fhir/R4/metadata",
    "POST /fhir/R4/$validate",
]

NOT_IMPLEMENTED_ENDPOINTS: list[str] = [
    "POST /v1/documents (M3)",
    "POST /v1/conversions (M3)",
    "POST /v1/extract (M3)",
    "POST /v1/assert (M3)",
    "POST /v1/bind (M3)",
    "POST /v1/assemble (M3)",
    "POST /v1/verify (M3)",
    "POST /v1/repair (M3)",
    "POST /v1/normalize (M3)",
    "POST /v1/reviews (M4)",
    "POST /v1/llm/qualify (M5)",
    "POST /v1/deliveries (M6)",
    "POST /v1/translate/hl7v2 (out of scope for v1)",
    "POST /v1/translate/cda (out of scope for v1)",
    "POST /v1/translate/tabular (out of scope for v1)",
]


@router.get(
    "/capabilities",
    summary="What this build can do, and what it cannot",
    response_model=CapabilitiesResponse,
)
async def capabilities(settings: SettingsDep, principal: PrincipalDep) -> CapabilitiesResponse:
    del principal
    layers: list[dict[str, Any]] = [
        {
            "layer": str(layer),
            "number": layer.number,
            "description": _LAYER_DESCRIPTION[layer],
            "available": layer not in _AVAILABLE_FROM_M3,
            "requires_llm": layer in _AVAILABLE_FROM_M3,
        }
        for layer in CASCADE_ORDER
    ]
    return CapabilitiesResponse(
        service=settings.service_name,
        version=CODE_VERSION,
        fhir_versions=list(SUPPORTED_FHIR_VERSIONS),
        ig_packages=list(settings.ig_coordinates),
        validation_layers=layers,
        endpoints_implemented=IMPLEMENTED_ENDPOINTS,
        endpoints_not_implemented=NOT_IMPLEMENTED_ENDPOINTS,
        llm_required=False,
        local_only_mode=settings.local_only_mode,
        credential_storage=str(settings.credential_storage),
        min_qualification_tier=str(settings.min_qualification_tier),
    )


@router.get("/igs", summary="Implementation guides this deployment validates against")
async def implementation_guides(
    settings: SettingsDep, principal: PrincipalDep
) -> dict[str, list[dict[str, str]]]:
    """List the configured IGs.

    These are the packages the validator sidecar was *built* with: the sidecar
    loads them from disk at startup and exposes no runtime load endpoint, which
    is what makes a conformance claim reproducible. Use ``GET /readyz`` to
    confirm they actually resolved.
    """
    del principal
    return {
        "ig_packages": [
            {"name": package.name, "version": package.version, "coordinate": package.coordinate}
            for package in settings.default_ig_packages
        ]
    }


@router.get(
    "/error-codes",
    summary="The error CodeSystem this API returns",
    response_class=Response,
    responses={200: {"content": {FHIR_JSON_MEDIA_TYPE: {}}}},
)
async def error_codes() -> Response:
    """Publish the error catalogue as a FHIR ``CodeSystem``.

    Unauthenticated on purpose: it contains no tenant data, and a client should
    be able to fetch the code list before it has credentials.
    """
    return Response(
        content=json.dumps(error_code_system(), separators=(",", ":")),
        media_type=FHIR_JSON_MEDIA_TYPE,
    )


__all__ = ["IMPLEMENTED_ENDPOINTS", "NOT_IMPLEMENTED_ENDPOINTS", "router"]
