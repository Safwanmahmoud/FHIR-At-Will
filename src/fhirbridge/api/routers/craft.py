"""``POST /v1/craft`` — agentic narrative-to-FHIR (AGENTS.md 7, principle 2.3).

Where ``/v1/NAR2FHIR`` uses a grounded extraction and assembly pipeline,
``/v1/craft`` gives the model deterministic tools and lets it build the record
step by step. Each tool validates its own edit against the typed
models and the terminology server before committing, so the draft can never enter
a non-conformant state — the model chooses the facts, the tools guarantee the
FHIR. The assembled bundle is then run through the same L1-L5 cascade as every
other path, so the response is a report as much as a conversion.

BYOK and ``conversions:write`` like ``/v1/NAR2FHIR``: the loop spends the caller's
money across several model calls, bounded by ``MAX_AGENT_ITERATIONS`` and
``MAX_COST_USD_PER_CONVERSION``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Response
from fastapi.responses import StreamingResponse

from fhirbridge.agent.loop import CraftAgent, CraftResult
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
from fhirbridge.domain.errors import FhirbridgeError
from fhirbridge.domain.ids import IdPrefix, new_id
from fhirbridge.llm.invocation import LlmInvocation
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
        validate_output=body.validate_output,
    )

    _log_result(result, conversion_id=conversion_id, actor_id=principal.actor_id)

    response.headers["Cache-Control"] = "no-store"
    return _craft_response(result, invocation=invocation, conversion_id=conversion_id)


@router.post(
    "/craft/stream",
    summary="Stream a tool-driven narrative-to-FHIR conversion as NDJSON (BYOK)",
    response_class=StreamingResponse,
    responses={
        **_LLM_ERROR_RESPONSES,
        200: {
            "description": (
                "Newline-delimited JSON events. Draft events contain the live Bundle; "
                "the final complete event contains the normal CraftResponse fields."
            ),
            "content": {"application/x-ndjson": {}},
        },
    },
)
async def craft_stream(
    body: CraftRequest,
    principal: PrincipalDep,
    invocation: LlmInvocationDep,
    gateway: LlmGatewayDep,
    terminology: TerminologyDep,
    cascade: CascadeDep,
    settings: SettingsDep,
) -> StreamingResponse:
    """Stream tool activity and immutable snapshots of the evolving FHIR draft."""
    principal.require(Scope.CONVERSIONS_WRITE)
    gateway.authorize(invocation, sending_phi=True)
    conversion_id = new_id(IdPrefix.CONVERSION)
    actor_id = principal.actor_id

    async def events() -> AsyncIterator[bytes]:
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def emit(event: dict[str, Any]) -> None:
            await queue.put(event)

        async def produce() -> None:
            try:
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
                    on_event=emit,
                    authorize=False,
                    validate_output=body.validate_output,
                )
                _log_result(result, conversion_id=conversion_id, actor_id=actor_id)
                payload = _craft_response(
                    result, invocation=invocation, conversion_id=conversion_id
                ).model_dump(mode="json")
                await emit({"type": "complete", **payload})
            except asyncio.CancelledError:
                raise
            except FhirbridgeError as exc:
                logger.warning(
                    "craft_stream_failed",
                    extra={
                        "conversion_id": conversion_id,
                        "error_code": str(exc.code),
                        "http_status": exc.http_status,
                    },
                )
                await emit(
                    {
                        "type": "error",
                        "code": str(exc.code),
                        "status": exc.http_status,
                        "message": exc.spec.title,
                    }
                )
            except Exception:
                logger.exception(
                    "craft_stream_failed",
                    extra={"conversion_id": conversion_id},
                )
                await emit(
                    {
                        "type": "error",
                        "code": "internal-error",
                        "status": 500,
                        "message": "The craft stream failed unexpectedly.",
                    }
                )
            finally:
                await queue.put(None)

        producer = asyncio.create_task(produce())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield (json.dumps(event, separators=(",", ":")) + "\n").encode()
        finally:
            if not producer.done():
                producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


def _craft_response(
    result: CraftResult,
    *,
    invocation: LlmInvocation,
    conversion_id: str,
) -> CraftResponse:
    return CraftResponse(
        conversion_id=conversion_id,
        bundle=result.bundle,
        report=result.report,
        validated=result.validated,
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


def _log_result(result: CraftResult, *, conversion_id: str, actor_id: str) -> None:
    logger.info(
        "craft_request_completed",
        extra={
            # Counts and decisions only; the narrative and bundle are PHI and never
            # reach a log (principle 2.6).
            "conversion_id": conversion_id,
            "resource_type": (
                result.report.resource_type if result.report is not None else "Bundle"
            ),
            "decision": (
                str(result.report.status) if result.report is not None else "not_validated"
            ),
            "validated": result.validated,
            "actor_id": actor_id,
            "model": result.model,
            "iterations": result.iterations,
            "stop_reason": result.stop_reason,
        },
    )


__all__ = ["router"]
