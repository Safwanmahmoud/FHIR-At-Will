"""``POST /v1/deidentify`` — expose the configured deterministic minimizer."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Response

from fhirbridge.api.auth import Scope
from fhirbridge.api.deps import PrincipalDep, SettingsDep
from fhirbridge.api.routers.convert import declared_identifiers_of, deid_info_of
from fhirbridge.api.schemas import DeidentifyRequest, DeidentifyResponse
from fhirbridge.deid.minimize import minimize
from fhirbridge.deid.policy import DeidPolicy
from fhirbridge.domain.errors import PhiMinimizationRequiredError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["conversion"])

_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    422: {
        "description": (
            "De-identification is not enforced for this deployment, or the request is invalid."
        )
    },
    503: {"description": "The required de-identification assets are unavailable."},
}


@router.post(
    "/deidentify",
    summary="De-identify a clinical narrative with the configured deterministic layer",
    response_model=DeidentifyResponse,
    responses=_ERROR_RESPONSES,
)
async def deidentify(
    body: DeidentifyRequest,
    principal: PrincipalDep,
    settings: SettingsDep,
    response: Response,
) -> DeidentifyResponse:
    principal.require(Scope.CONVERSIONS_WRITE)
    policy = DeidPolicy.from_settings(settings)
    if not policy.enforced:
        raise PhiMinimizationRequiredError(
            "Set DEID_MODE=enforced before using the de-identification endpoint."
        )

    result = minimize(
        body.text,
        policy=policy,
        declared=declared_identifiers_of(body.known_identifiers),
    )
    try:
        result.assert_safe_payload(result.safe_text)
        report = result.report()
        logger.info(
            "narrative_deidentified",
            extra={
                "profile": report.profile,
                "ruleset_version": report.ruleset_version,
                "replacement_count": report.replacements,
            },
        )
        response.headers["Cache-Control"] = "no-store"
        return DeidentifyResponse(text=result.safe_text, deid=deid_info_of(report))
    finally:
        result.close()


__all__ = ["router"]
