"""The FHIR CapabilityStatement facade (AGENTS.md 11.6)."""

from __future__ import annotations

import httpx
import pytest

from fhirbridge.api.routers.fhir_facade import FACADE_FHIR_VERSION, SUPPORTED_OPERATIONS


class TestMetadata:
    async def test_it_is_reachable_without_credentials(
        self, anon_client: httpx.AsyncClient
    ) -> None:
        response = await anon_client.get("/fhir/R4/metadata")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/fhir+json")

    async def test_it_declares_r4(self, anon_client: httpx.AsyncClient) -> None:
        body = (await anon_client.get("/fhir/R4/metadata")).json()

        assert body["resourceType"] == "CapabilityStatement"
        assert body["fhirVersion"] == FACADE_FHIR_VERSION == "4.0.1"

    async def test_it_declares_no_operations(self, anon_client: httpx.AsyncClient) -> None:
        body = (await anon_client.get("/fhir/R4/metadata")).json()

        declared = {operation["name"] for operation in body["rest"][0]["operation"]}
        assert declared == set(SUPPORTED_OPERATIONS) == set()

    async def test_it_disclaims_being_a_fhir_repository(
        self, anon_client: httpx.AsyncClient
    ) -> None:
        body = (await anon_client.get("/fhir/R4/metadata")).json()
        rest = body["rest"][0]

        assert "resource" not in rest
        assert "not a FHIR repository" in rest["documentation"]


class TestNoRepositoryEndpointsExist:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/fhir/R4/Patient"),
            ("POST", "/fhir/R4/Patient"),
            ("GET", "/fhir/R4/Patient/example"),
            ("PUT", "/fhir/R4/Observation/obs-1"),
            ("DELETE", "/fhir/R4/Observation/obs-1"),
            ("GET", "/fhir/R4/Observation"),
        ],
    )
    async def test_crud_and_search_are_absent(
        self, client: httpx.AsyncClient, method: str, path: str
    ) -> None:
        response = await client.request(method, path, json={})

        assert response.status_code == 404
