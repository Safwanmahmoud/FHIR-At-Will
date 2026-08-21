"""The FHIR facade (AGENTS.md 11.6).

A thin layer over the same engine, so FHIR-native tooling can reach it without
learning our JSON shapes. It adds no capability of its own: ``$validate`` runs
exactly the cascade that ``POST /v1/validate`` runs, and the operations that
need the pipeline return ``501`` until M3 rather than pretending.

The ``CapabilityStatement`` is generated from the same constants the routes use,
so it cannot drift into advertising an operation that does not exist — a
contract test asserts that every operation it declares is routable.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Final

from fastapi import APIRouter, Response

from fhirbridge.api.deps import CascadeDep, PrincipalDep, SettingsDep
from fhirbridge.api.routers.validate import report_to_outcome
from fhirbridge.domain.errors import InvalidFhirResourceError, NotImplementedInV1Error
from fhirbridge.fhir.operation_outcome import FHIR_JSON_MEDIA_TYPE
from fhirbridge.validation.cascade import ValidationSpec
from fhirbridge.version import CODE_VERSION

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/fhir/R4", tags=["fhir-facade"])

FACADE_FHIR_VERSION: Final[str] = "4.0.1"

SUPPORTED_OPERATIONS: Final[tuple[str, ...]] = ("validate",)
"""Operations this build actually serves. ``convert`` and ``extract`` arrive with
the pipeline in M3 and are advertised nowhere until then."""

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
                "operation": [
                    {
                        "name": "validate",
                        "definition": "http://hl7.org/fhir/OperationDefinition/Resource-validate",
                        "documentation": (
                            "Runs the L1-L5 validation cascade and returns an "
                            "OperationOutcome. Fails closed with 503 when the validator "
                            "or terminology server is unavailable."
                        ),
                    }
                ],
            }
        ],
    }
    return _fhir(statement)


@router.post(
    "/$validate",
    summary="Validate a resource (runs the same cascade as POST /v1/validate)",
    response_class=Response,
    responses=_FHIR_RESPONSE
    | {503: {"description": "Validator or terminology server unavailable; failing closed."}},
)
async def validate_operation(
    payload: dict[str, Any],
    principal: PrincipalDep,
    cascade: CascadeDep,
    settings: SettingsDep,
) -> Response:
    """``POST /fhir/R4/$validate``.

    Accepts either a bare resource or a ``Parameters`` resource carrying
    ``resource`` and optional ``profile`` parameters, which is how FHIR clients
    normally invoke the operation.
    """
    del principal
    resource, profiles = _unwrap_parameters(payload)
    report = await cascade.run(
        resource,
        ValidationSpec(profiles=tuple(profiles), ig_packages=settings.ig_coordinates),
    )
    logger.info(
        "facade_validate_completed",
        extra={"resource_type": report.resource_type, "decision": str(report.status)},
    )
    return _fhir(report_to_outcome(report))


def _unwrap_parameters(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Pull the resource and requested profiles out of a ``Parameters`` wrapper."""
    if payload.get("resourceType") != "Parameters":
        if "resourceType" not in payload:
            raise InvalidFhirResourceError(
                "The body must be a FHIR resource or a Parameters resource containing one."
            )
        return payload, []

    resource: dict[str, Any] | None = None
    profiles: list[str] = []
    for parameter in payload.get("parameter", []) or []:
        if not isinstance(parameter, dict):
            continue
        match parameter.get("name"):
            case "resource" if isinstance(parameter.get("resource"), dict):
                resource = parameter["resource"]
            case "profile":
                value = parameter.get("valueUri") or parameter.get("valueCanonical")
                if isinstance(value, str):
                    profiles.append(value)
            case _:
                pass

    if resource is None:
        raise InvalidFhirResourceError(
            "The Parameters resource does not contain a 'resource' parameter."
        )
    return resource, profiles


_PIPELINE_PENDING = (
    "This operation runs the extraction pipeline, which ships in milestone M3. "
    "Validation endpoints are available now: POST /fhir/R4/$validate and "
    "POST /v1/validate."
)


@router.post(
    "/$convert",
    summary="Convert narrative to a Bundle (available from M3)",
    responses={501: {"description": _PIPELINE_PENDING}},
)
async def convert_operation(principal: PrincipalDep) -> None:
    del principal
    raise NotImplementedInV1Error(_PIPELINE_PENDING, safe_context={"operation": "convert"})


@router.post(
    "/$extract",
    summary="Extract facts from narrative (available from M3)",
    responses={501: {"description": _PIPELINE_PENDING}},
)
async def extract_operation(principal: PrincipalDep) -> None:
    del principal
    raise NotImplementedInV1Error(_PIPELINE_PENDING, safe_context={"operation": "extract"})


__all__ = ["FACADE_FHIR_VERSION", "SUPPORTED_OPERATIONS", "router"]
