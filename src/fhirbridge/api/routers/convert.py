"""``POST /v1/convert`` and ``POST /v1/llm/probe`` (AGENTS.md 7, 11.4).

These are the first endpoints that call an LLM, and they exist only because the
validation cascade already does. ``/v1/convert`` turns clinical narrative into a
FHIR bundle and immediately scores that bundle through the same L1-L5 cascade
that ``POST /v1/validate`` runs: the model's output is never returned as trusted,
only as measured.

Both endpoints are BYOK — the caller supplies provider, model and key in
``X-LLM-*`` headers — and both require the ``conversions:write`` scope, because
both spend the caller's money and send traffic to an external provider.

This build's conversion is synchronous and stateless: like ``/v1/validate`` it
retains nothing. The persisted, fact-based, asynchronous ``/v1/conversions``
resource (with extraction stages and source-span fidelity) is milestone M3.
"""

from __future__ import annotations

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
from fhirbridge.llm.prompts import NARRATIVE_TO_BUNDLE, PROMPT_SET_VERSION
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
    "/convert",
    summary="Convert clinical narrative to a validated FHIR Bundle (BYOK)",
    response_model=ConvertResponse,
    responses=_LLM_ERROR_RESPONSES,
)
async def convert(
    body: ConvertRequest,
    principal: PrincipalDep,
    invocation: LlmInvocationDep,
    gateway: LlmGatewayDep,
    cascade: CascadeDep,
    settings: SettingsDep,
    response: Response,
) -> ConvertResponse:
    """Generate a FHIR bundle from narrative, then validate it before returning."""
    principal.require(Scope.CONVERSIONS_WRITE)
    conversion_id = new_id(IdPrefix.CONVERSION)

    profiles = ", ".join(body.profiles) if body.profiles else "none"
    result = await gateway.complete_json(
        invocation,
        system_prompt=NARRATIVE_TO_BUNDLE.system,
        user_prompt=NARRATIVE_TO_BUNDLE.render_user(narrative=body.text, profiles=profiles),
    )

    report = await cascade.run(
        result.resource,
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
    report.versions.model = {"convert": result.model}
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
            "model": result.model,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return ConvertResponse(
        conversion_id=conversion_id,
        bundle=result.resource,
        report=report,
        llm=LlmCallInfo(
            provider=invocation.provider,
            model=result.model,
            usage=result.usage,
            cost_usd=float(result.cost_usd) if result.cost_usd is not None else None,
            latency_ms=result.latency_ms,
            qualification_tier=str(resolve_tier(invocation.model)),
        ),
    )


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
