"""``POST /v1/NAR2FHIR`` and ``POST /v1/llm/probe``.

NAR2FHIR uses two grounded calls: extract catalog-constrained facts first, then
assemble those facts into typed FHIR structures. The generated Bundle is scored
through the same L1-L5 cascade as ``POST /v1/validate``.

Both endpoints are BYOK — the caller supplies provider, model and key in
``X-LLM-*`` headers — and both require the ``conversions:write`` scope, because
both spend the caller's money and send traffic to an external provider.

This build's conversion is synchronous and stateless: like ``/v1/validate`` it
retains nothing. The persisted, fact-based, asynchronous ``/v1/conversions``
resource (with extraction stages and source-span fidelity) is milestone M3.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Response

from fhirbridge.api.auth import Scope
from fhirbridge.api.deps import (
    CascadeDep,
    LlmGatewayDep,
    LlmInvocationDep,
    PrincipalDep,
    SettingsDep,
)
from fhirbridge.api.schemas import (
    ConvertRequest,
    ConvertResponse,
    LlmCallInfo,
    LlmProbeResponse,
)
from fhirbridge.domain.ids import IdPrefix, new_id
from fhirbridge.llm.gateway import LlmResult
from fhirbridge.llm.nar2fhir import (
    parse_entities,
    require_fhir_bundle,
    resource_field_reference,
)
from fhirbridge.llm.prompts import (
    ENTITIES_TO_FHIR_BUNDLE,
    NARRATIVE_TO_ENTITIES,
    PROMPT_SET_VERSION,
)
from fhirbridge.llm.qualification import resolve_tier
from fhirbridge.validation.cascade import ValidationSpec

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["conversion"])

_LLM_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"description": "No LLM credentials, or the provider rejected the key."},
    422: {
        "description": (
            "The model output could not be parsed, the model is not qualified, the "
            "input exceeds the context window, or the budget would be exceeded."
        )
    },
    429: {"description": "The LLM provider rate-limited the request. Retryable."},
    451: {"description": "The target LLM host is blocked by egress policy."},
    503: {"description": "The validator or terminology server is unavailable; failing closed."},
}


@router.post(
    "/NAR2FHIR",
    summary="Convert narrative to a validated FHIR Bundle with grounded extraction (BYOK)",
    response_model=ConvertResponse,
    responses=_LLM_ERROR_RESPONSES,
)
@router.post(
    "/convert",
    include_in_schema=False,
    deprecated=True,
    response_model=ConvertResponse,
    responses=_LLM_ERROR_RESPONSES,
)
async def nar2fhir(
    body: ConvertRequest,
    principal: PrincipalDep,
    invocation: LlmInvocationDep,
    gateway: LlmGatewayDep,
    cascade: CascadeDep,
    settings: SettingsDep,
    response: Response,
) -> ConvertResponse:
    """Extract grounded facts, assemble a FHIR Bundle, then validate it."""
    principal.require(Scope.CONVERSIONS_WRITE)
    conversion_id = new_id(IdPrefix.CONVERSION)

    profiles = ", ".join(body.profiles) if body.profiles else "none"
    extraction = await gateway.complete_json(
        invocation,
        system_prompt=NARRATIVE_TO_ENTITIES.system,
        user_prompt=NARRATIVE_TO_ENTITIES.render_user(narrative=body.text),
    )
    entities = parse_entities(extraction.resource)
    candidate_resource_types = {entity["resourceType"] for entity in entities}

    generation = await gateway.complete_json(
        invocation,
        system_prompt=ENTITIES_TO_FHIR_BUNDLE.system,
        user_prompt=ENTITIES_TO_FHIR_BUNDLE.render_user(
            narrative=body.text,
            profiles=profiles,
            entities=json.dumps(entities, ensure_ascii=False, separators=(",", ":")),
            field_reference=resource_field_reference(candidate_resource_types),
        ),
    )
    bundle = require_fhir_bundle(
        generation.resource,
        allowed_resource_types=candidate_resource_types,
    )

    report = await cascade.run(
        bundle,
        ValidationSpec(
            profiles=tuple(body.profiles),
            layers=frozenset(body.layers) if body.layers is not None else None,
            max_terminology_checks=body.max_terminology_checks,
            ig_packages=settings.ig_coordinates,
            conversion_id=conversion_id,
        ),
    )
    # Stamp the generation provenance the validate-only path leaves empty
    # (principle 2.8): which model produced this, and which prompt set.
    report.versions.model = {
        "nar2fhir_extract": extraction.model,
        "nar2fhir_generate": generation.model,
    }
    report.versions.prompt_set = PROMPT_SET_VERSION

    logger.info(
        "conversion_completed",
        extra={
            # Counts and decisions only; the narrative and the bundle are PHI and
            # never reach a log (principle 2.6).
            "conversion_id": conversion_id,
            "resource_type": report.resource_type,
            "decision": str(report.status),
            "actor_id": principal.actor_id,
            "model": generation.model,
            "entity_count": len(entities),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return ConvertResponse(
        conversion_id=conversion_id,
        bundle=bundle,
        report=report,
        llm=LlmCallInfo(
            provider=invocation.provider,
            model=generation.model,
            usage=_combined_usage(extraction, generation),
            cost_usd=_combined_cost(extraction, generation),
            latency_ms=extraction.latency_ms + generation.latency_ms,
            qualification_tier=str(resolve_tier(invocation.model)),
        ),
    )


def _combined_usage(*results: LlmResult) -> dict[str, int]:
    keys = {key for result in results for key in result.usage}
    return {key: sum(result.usage.get(key, 0) for result in results) for key in sorted(keys)}


def _combined_cost(*results: LlmResult) -> float | None:
    if any(result.cost_usd is None for result in results):
        return None
    return float(sum(result.cost_usd for result in results if result.cost_usd is not None))


@router.post(
    "/llm/probe",
    summary="Verify BYOK connectivity and credentials without sending PHI",
    response_model=LlmProbeResponse,
    responses=_LLM_ERROR_RESPONSES,
)
async def probe(
    principal: PrincipalDep,
    invocation: LlmInvocationDep,
    gateway: LlmGatewayDep,
    response: Response,
) -> LlmProbeResponse:
    """Send a trivial, PHI-free prompt to confirm the caller's model is reachable."""
    principal.require(Scope.CONVERSIONS_WRITE)
    result = await gateway.probe(invocation)
    logger.info(
        "llm_probe_completed",
        extra={"actor_id": principal.actor_id, "model": result.model},
    )
    response.headers["Cache-Control"] = "no-store"
    return LlmProbeResponse(
        ok=True,
        provider=invocation.provider,
        model=result.model,
        qualification_tier=str(result.tier),
        latency_ms=result.latency_ms,
        cost_usd=float(result.cost_usd) if result.cost_usd is not None else None,
        sample=result.sample,
    )


__all__ = ["router"]
