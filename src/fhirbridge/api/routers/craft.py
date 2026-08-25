"""``POST /v1/craft`` — agentic narrative-to-FHIR (AGENTS.md 7, principle 2.3).

Where ``/v1/convert`` asks a model for a whole Bundle in one shot and then scores
it, ``/v1/craft`` gives the model a set of deterministic tools and lets it build
the record step by step. Each tool validates its own edit against the typed
models and the terminology server before committing, so the draft can never enter
a non-conformant state — the model chooses the facts, the tools guarantee the
FHIR. The assembled bundle is then run through the same L1-L5 cascade as every
other path, so the response is a report as much as a conversion.

BYOK and ``conversions:write`` like ``/v1/convert``: the loop spends the caller's
money across several model calls, bounded by ``MAX_AGENT_ITERATIONS`` and
``MAX_COST_USD_PER_CONVERSION``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Response

from fhirbridge.agent.loop import CraftAgent
from fhirbridge.api.auth import Scope
from fhirbridge.api.deps import (
    CascadeDep,
    LlmGatewayDep,
    LlmInvocationDep,
    PrincipalDep,
    SettingsDep,
    TerminologyDep,
)
from fhirbridge.api.schemas import CraftRequest, CraftResponse, CraftToolCall, LlmCallInfo
from fhirbridge.domain.ids import IdPrefix, new_id
from fhirbridge.llm.qualification import resolve_tier
from fhirbridge.version import AGENT_TOOLSET_VERSION

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["conversion"])

_LLM_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"description": "No LLM credentials, or the provider rejected the key."},
    422: {
        "description": (
            "The model is not qualified, does not support tool calling, the input "
            "exceeds the context window, or the budget would be exceeded."
        )
    },
    429: {"description": "The LLM provider rate-limited the request. Retryable."},
    451: {"description": "The target LLM host is blocked by egress policy."},
    503: {"description": "The validator or terminology server is unavailable; failing closed."},
}


@router.post(
    "/craft",
    summary="Build a validated FHIR Bundle from narrative with a tool-driven agent (BYOK)",
    response_model=CraftResponse,
    responses=_LLM_ERROR_RESPONSES,
)
async def craft(
    body: CraftRequest,
    principal: PrincipalDep,
    invocation: LlmInvocationDep,
    gateway: LlmGatewayDep,
    terminology: TerminologyDep,
    cascade: CascadeDep,
    settings: SettingsDep,
    response: Response,
) -> CraftResponse:
    """Drive a model through deterministic tools to assemble a validated bundle."""
    principal.require(Scope.CONVERSIONS_WRITE)
    conversion_id = new_id(IdPrefix.CONVERSION)

    agent = CraftAgent(gateway=gateway, settings=settings)
    result = await agent.run(
        invocation,
        terminology=terminology,
        cascade=cascade,
        narrative=body.text,
        profiles=tuple(body.profiles),
        layers=frozenset(body.layers) if body.layers is not None else None,
        max_terminology_checks=body.max_terminology_checks,
        ig_packages=settings.ig_coordinates,
        conversion_id=conversion_id,
    )

    logger.info(
        "craft_request_completed",
        extra={
            # Counts and decisions only; the narrative and bundle are PHI and never
            # reach a log (principle 2.6).
            "conversion_id": conversion_id,
            "resource_type": result.report.resource_type,
            "decision": str(result.report.status),
            "actor_id": principal.actor_id,
            "model": result.model,
            "iterations": result.iterations,
            "stop_reason": result.stop_reason,
        },
    )

    response.headers["Cache-Control"] = "no-store"
    return CraftResponse(
        conversion_id=conversion_id,
        bundle=result.bundle,
        report=result.report,
        llm=LlmCallInfo(
            provider=invocation.provider,
            model=result.model,
            usage=result.usage,
            cost_usd=float(result.cost_usd) if result.cost_usd is not None else None,
            latency_ms=result.latency_ms,
            qualification_tier=str(resolve_tier(invocation.model)),
        ),
        trace=[CraftToolCall(**entry) for entry in result.trace],
        iterations=result.iterations,
        stop_reason=result.stop_reason,
        toolset_version=AGENT_TOOLSET_VERSION,
    )


__all__ = ["router"]
