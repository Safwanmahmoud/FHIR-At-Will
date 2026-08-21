"""The FHIR facade must not advertise what it cannot do (AGENTS.md 16.3).

A ``CapabilityStatement`` claiming an operation that returns 404 or 501 is worse
than omitting it: FHIR clients branch on this document, so an overclaim turns
into a failed integration at the customer's end.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from fhirbridge.api.routers.fhir_facade import FACADE_FHIR_VERSION, SUPPORTED_OPERATIONS
from tests.helpers import api_routes

pytestmark = pytest.mark.contract


async def test_capability_statement_is_served_as_fhir_json(
    anon_client: httpx.AsyncClient,
) -> None:
    response = await anon_client.get("/fhir/R4/metadata")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/fhir+json")
    assert response.json()["resourceType"] == "CapabilityStatement"


async def test_declared_fhir_version_is_r4(anon_client: httpx.AsyncClient) -> None:
    statement = (await anon_client.get("/fhir/R4/metadata")).json()

    assert statement["fhirVersion"] == FACADE_FHIR_VERSION == "4.0.1"


async def test_every_declared_operation_is_routable(
    anon_client: httpx.AsyncClient, app: FastAPI
) -> None:
    statement = (await anon_client.get("/fhir/R4/metadata")).json()
    declared = {
        operation["name"] for rest in statement["rest"] for operation in rest.get("operation", [])
    }
    routed = {
        route.path.removeprefix("/fhir/R4/$")
        for route in api_routes(app)
        if route.path.startswith("/fhir/R4/$")
    }

    assert declared == set(SUPPORTED_OPERATIONS)
    assert declared <= routed


async def test_unimplemented_operations_are_not_advertised(
    anon_client: httpx.AsyncClient,
) -> None:
    statement = (await anon_client.get("/fhir/R4/metadata")).json()
    declared = {
        operation["name"] for rest in statement["rest"] for operation in rest.get("operation", [])
    }

    assert "convert" not in declared
    assert "extract" not in declared


async def test_statement_says_this_is_not_a_repository(
    anon_client: httpx.AsyncClient,
) -> None:
    """AGENTS.md 3: no CRUD, no search. Say so where clients will read it."""
    statement = (await anon_client.get("/fhir/R4/metadata")).json()
    rest = statement["rest"][0]

    assert "resource" not in rest
    assert "not a FHIR repository" in rest["documentation"]
