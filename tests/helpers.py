"""Payload builders shared across the suite.

Kept out of ``conftest.py`` so test modules can import them by name instead of
reaching into a plugin module.
"""

from __future__ import annotations

import json
from typing import Any, Final

import httpx
from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.routing import Mount, Route

VALIDATOR_URL: Final[str] = "http://validator.test"
TERMINOLOGY_URL: Final[str] = "http://terminology.test"

US_CORE_PATIENT: Final[str] = "http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient"


def api_routes(app: FastAPI) -> list[APIRoute]:
    """Every :class:`APIRoute` in the app, flattened.

    Recent FastAPI wraps each ``include_router`` call in an ``_IncludedRouter``
    object, so ``app.routes`` is a tree rather than a list. Walking it matters: a
    static check over ``app.routes`` alone finds only ``/openapi.json`` and
    ``/docs``, and passes vacuously.
    """
    found: list[APIRoute] = []
    stack: list[object] = list(app.routes)
    while stack:
        item = stack.pop()
        if isinstance(item, APIRoute):
            found.append(item)
            continue
        if isinstance(item, Mount) and item.routes:
            stack.extend(item.routes)
            continue
        if isinstance(item, Route):
            continue
        nested = getattr(item, "original_router", None) or item
        stack.extend(getattr(nested, "routes", None) or [])
    return found


def operation_outcome(*issues: dict[str, Any]) -> dict[str, Any]:
    """Build the ``OperationOutcome`` the validator sidecar would return."""
    return {
        "resourceType": "OperationOutcome",
        "issue": list(issues)
        or [{"severity": "information", "code": "informational", "details": {"text": "All OK"}}],
    }


def parameters(**values: Any) -> dict[str, Any]:
    """Build a FHIR ``Parameters`` resource from simple keyword values."""
    parameter: list[dict[str, Any]] = []
    for name, value in values.items():
        if isinstance(value, bool):
            parameter.append({"name": name, "valueBoolean": value})
        elif isinstance(value, int):
            parameter.append({"name": name, "valueInteger": value})
        elif isinstance(value, dict):
            parameter.append({"name": name, "resource": value})
        else:
            parameter.append({"name": name, "valueString": str(value)})
    return {"resourceType": "Parameters", "parameter": parameter}


def fhir_json(payload: dict[str, Any]) -> httpx.Response:
    """A 200 response carrying ``application/fhir+json``."""
    return httpx.Response(
        200,
        content=json.dumps(payload).encode(),
        headers={"Content-Type": "application/fhir+json"},
    )


OBSERVATION: Final[dict[str, Any]] = {
    "resourceType": "Observation",
    "id": "obs-1",
    "status": "preliminary",
    "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4", "display": "Heart rate"}]},
    "subject": {"reference": "Patient/example"},
    "effectiveDateTime": "2026-01-02T10:00:00Z",
    "valueQuantity": {
        "value": 72,
        "unit": "beats/minute",
        "system": "http://unitsofmeasure.org",
        "code": "/min",
    },
}
"""A minimal, conformant vital-sign Observation. Synthetic (AGENTS.md 16.8)."""


__all__ = [
    "OBSERVATION",
    "TERMINOLOGY_URL",
    "US_CORE_PATIENT",
    "VALIDATOR_URL",
    "api_routes",
    "fhir_json",
    "operation_outcome",
    "parameters",
]
