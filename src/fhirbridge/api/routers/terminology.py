"""Terminology primitives (AGENTS.md 11.4).

Both endpoints are thin, honest passthroughs to the configured terminology
server. They exist so that the rule in principle 2.3 — a code is only ever
emitted after the server confirmed it — is testable and inspectable from
outside the pipeline.

Note the method: ``POST`` with a body, never ``GET`` with query parameters. A
code plus a ValueSet describes a patient's clinical state, and principle 2.6
keeps that out of URLs and therefore out of proxy and access logs.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Response

from fhirbridge.api.deps import PrincipalDep, TerminologyDep
from fhirbridge.api.schemas import (
    TerminologyMapMatch,
    TerminologyMapRequest,
    TerminologyMapResponse,
    TerminologySearchCandidate,
    TerminologySearchRequest,
    TerminologySearchResponse,
    ValidateCodeRequest,
    ValidateCodeResponse,
)
from fhirbridge.domain.errors import InvalidRequestError
from fhirbridge.terminology.interface import search_value_set_for_system

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/terminology", tags=["terminology"])

_UNAVAILABLE = {
    "description": (
        "The terminology server is unavailable. Failing closed: an unanswered "
        "question is never reported as 'code not valid'."
    )
}


@router.post(
    "/validate-code",
    summary="Confirm a code against the configured terminology server",
    response_model=ValidateCodeResponse,
    responses={503: _UNAVAILABLE},
)
async def validate_code(
    body: ValidateCodeRequest,
    principal: PrincipalDep,
    terminology: TerminologyDep,
    response: Response,
) -> ValidateCodeResponse:
    if not body.system and not body.value_set:
        raise InvalidRequestError(
            "Supply 'system' (to validate against a CodeSystem) or 'value_set' "
            "(to validate membership), or both."
        )

    result = await terminology.validate_code(
        system=body.system,
        code=body.code,
        display=body.display,
        version=body.version,
        value_set=body.value_set,
    )
    logger.info(
        "validate_code_completed",
        extra={
            # The system is a canonical URL and safe; the code is not logged.
            "system": body.system or "",
            "result": result.result,
            "actor_id": principal.actor_id,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return ValidateCodeResponse(
        result=result.result,
        system=body.system,
        code=body.code,
        display=result.display,
        value_set=body.value_set,
        code_system_version=result.code_system_version,
        message=result.message,
        issues=list(result.issues),
    )


@router.post(
    "/search",
    summary="Search a CodeSystem or ValueSet for candidate codes",
    response_model=TerminologySearchResponse,
    responses={503: _UNAVAILABLE},
)
async def search_terminology(
    body: TerminologySearchRequest,
    principal: PrincipalDep,
    terminology: TerminologyDep,
    response: Response,
) -> TerminologySearchResponse:
    if not body.system and not body.value_set:
        raise InvalidRequestError("Supply 'system' or 'value_set' to search.")

    value_set = body.value_set or search_value_set_for_system(body.system or "")
    result = await terminology.expand(
        value_set=value_set,
        filter_text=body.query,
        count=body.count,
    )
    candidates = [
        TerminologySearchCandidate(
            system=coding.system,
            code=coding.code,
            display=coding.display,
        )
        for coding in result.contains
        if coding.code
    ]
    logger.info(
        "terminology_search_completed",
        extra={
            # Query text can be clinical and is deliberately not logged.
            "system": body.system or "",
            "candidate_count": len(candidates),
            "actor_id": principal.actor_id,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return TerminologySearchResponse(candidates=candidates)


@router.post(
    "/map",
    summary="Translate a code between systems via $translate",
    response_model=TerminologyMapResponse,
    responses={503: _UNAVAILABLE},
)
async def map_code(
    body: TerminologyMapRequest,
    principal: PrincipalDep,
    terminology: TerminologyDep,
    response: Response,
) -> TerminologyMapResponse:
    """Translate a code using a ConceptMap on the terminology server.

    Mapping is delegated entirely to the server. This service does not infer
    equivalences, and a translation returned here is still subject to
    ``$validate-code`` before it may appear in a resource.
    """
    if not body.target_system and not body.concept_map:
        raise InvalidRequestError("Supply 'target_system' or 'concept_map'.")

    result = await terminology.translate(
        system=body.system,
        code=body.code,
        target_system=body.target_system,
        concept_map=body.concept_map,
    )
    del principal
    response.headers["Cache-Control"] = "no-store"
    return TerminologyMapResponse(
        result=result.result,
        matches=[
            TerminologyMapMatch(
                equivalence=match.equivalence,
                system=match.concept.system,
                code=match.concept.code,
                display=match.concept.display,
                version=match.concept.version,
                source=match.source,
            )
            for match in result.matches
        ],
        message=result.message,
    )


__all__ = ["router"]
