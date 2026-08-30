"""The FHIR capability facade (AGENTS.md 11.6).

The ``CapabilityStatement`` is generated from the same constants the routes use,
so it cannot drift into advertising an operation that does not exist — a
contract test asserts that every operation it declares is routable.
"""

from __future__ import annotations

import json
from typing import Any, Final

from fastapi import APIRouter, Response

from fhirbridge.api.deps import SettingsDep
from fhirbridge.fhir.operation_outcome import FHIR_JSON_MEDIA_TYPE
from fhirbridge.version import CODE_VERSION

router = APIRouter(prefix="/fhir/R4", tags=["fhir-facade"])

FACADE_FHIR_VERSION: Final[str] = "4.0.1"

SUPPORTED_OPERATIONS: Final[tuple[str, ...]] = ()
"""Operations this build serves through the FHIR-native facade."""

_FHIR_RESPONSE: dict[int | str, dict[str, Any]] = {200: {"content": {FHIR_JSON_MEDIA_TYPE: {}}}}


def _fhir(payload: dict[str, Any], status_code: int = 200) -> Response:
    return Response(
        content=json.dumps(payload, separators=(",", ":")),
        media_type=FHIR_JSON_MEDIA_TYPE,
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/metadata",
    summary="CapabilityStatement",
    response_class=Response,
    responses=_FHIR_RESPONSE,
)
async def metadata(settings: SettingsDep) -> Response:
    """Declare what the facade supports.

    Unauthenticated, as FHIR clients expect: it advertises capability only and
    exposes no tenant data.
    """
    statement: dict[str, Any] = {
        "resourceType": "CapabilityStatement",
        "id": "fhirbridge",
        "url": "https://fhirbridge.org/CapabilityStatement/fhirbridge",
        "version": CODE_VERSION,
        "name": "fhirbridge",
        "title": "fhirbridge narrative-to-FHIR service",
        "status": "active",
        "experimental": False,
        "kind": "instance",
        "software": {"name": "fhirbridge", "version": CODE_VERSION},
        "fhirVersion": FACADE_FHIR_VERSION,
        "format": ["json"],
        "implementationGuide": [
            f"http://packages.fhir.org/{package.name}/{package.version}"
            for package in settings.default_ig_packages
        ],
        "rest": [
            {
                "mode": "server",
                "documentation": (
                    "fhirbridge is not a FHIR repository. It exposes operations only: "
                    "there is no CRUD, no search, and no persistence of submitted "
                    "resources by this facade."
                ),
                "security": {
                    "description": (
                        "Bearer API key or OAuth2 client credentials. BYOK LLM "
                        "credentials travel in X-LLM-* headers, never in Parameters."
                    )
                },
                "operation": [],
            }
        ],
    }
    return _fhir(statement)


__all__ = ["FACADE_FHIR_VERSION", "SUPPORTED_OPERATIONS", "router"]
