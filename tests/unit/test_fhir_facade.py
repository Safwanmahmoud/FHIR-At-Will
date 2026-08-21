"""The FHIR facade (AGENTS.md 11.6).

The facade's contract is that it adds nothing: ``POST /fhir/R4/$validate`` runs
the same cascade as ``POST /v1/validate`` and reaches the same verdict. Tests
here assert that equivalence directly, because a facade that quietly validates
more loosely than the primary endpoint is the worst kind of bug — it looks like
the safe path and is not.

The operations that need the pipeline return 501 rather than a degraded answer.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from fhirbridge.api.routers.fhir_facade import FACADE_FHIR_VERSION, SUPPORTED_OPERATIONS
from tests.helpers import (
    OBSERVATION,
    US_CORE_PATIENT,
    VALIDATOR_URL,
    fhir_json,
    operation_outcome,
)


def parameters_with(resource: dict[str, Any], *profiles: str) -> dict[str, Any]:
    """A ``Parameters`` resource as a FHIR client would invoke ``$validate``."""
    parameter: list[dict[str, Any]] = [{"name": "resource", "resource": resource}]
    parameter.extend({"name": "profile", "valueUri": profile} for profile in profiles)
    return {"resourceType": "Parameters", "parameter": parameter}


class TestMetadata:
    async def test_it_is_reachable_without_credentials(
        self, anon_client: httpx.AsyncClient
    ) -> None:
        """FHIR clients fetch ``/metadata`` before they authenticate."""
        response = await anon_client.get("/fhir/R4/metadata")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/fhir+json")

    async def test_it_declares_r4(self, anon_client: httpx.AsyncClient) -> None:
        body = (await anon_client.get("/fhir/R4/metadata")).json()

        assert body["resourceType"] == "CapabilityStatement"
        assert body["fhirVersion"] == FACADE_FHIR_VERSION == "4.0.1"

    async def test_it_declares_only_the_operations_this_build_serves(
        self, anon_client: httpx.AsyncClient
    ) -> None:
        body = (await anon_client.get("/fhir/R4/metadata")).json()

        declared = {operation["name"] for operation in body["rest"][0]["operation"]}
        assert declared == set(SUPPORTED_OPERATIONS) == {"validate"}
        assert "convert" not in declared
        assert "extract" not in declared

    async def test_it_disclaims_being_a_fhir_repository(
        self, anon_client: httpx.AsyncClient
    ) -> None:
        """AGENTS.md 3: this is not a FHIR server. The statement must say so."""
        body = (await anon_client.get("/fhir/R4/metadata")).json()
        rest = body["rest"][0]

        assert "resource" not in rest
        assert "not a FHIR repository" in rest["documentation"]

    async def test_it_names_the_configured_implementation_guides(
        self, anon_client: httpx.AsyncClient
    ) -> None:
        body = (await anon_client.get("/fhir/R4/metadata")).json()

        assert any("us.core" in guide for guide in body["implementationGuide"])

    async def test_it_points_byok_credentials_at_headers_not_parameters(
        self, anon_client: httpx.AsyncClient
    ) -> None:
        body = (await anon_client.get("/fhir/R4/metadata")).json()

        assert "X-LLM-" in body["rest"][0]["security"]["description"]


class TestValidateOperation:
    async def test_a_bare_resource_is_accepted(
        self, client: httpx.AsyncClient, all_dependencies_healthy: None
    ) -> None:
        response = await client.post("/fhir/R4/$validate", json=OBSERVATION)

        assert response.status_code == 200
        body = response.json()
        assert body["resourceType"] == "OperationOutcome"
        assert response.headers["content-type"].startswith("application/fhir+json")

    async def test_a_parameters_wrapper_is_accepted(
        self, client: httpx.AsyncClient, all_dependencies_healthy: None
    ) -> None:
        response = await client.post("/fhir/R4/$validate", json=parameters_with(OBSERVATION))

        assert response.status_code == 200
        assert response.json()["resourceType"] == "OperationOutcome"

    async def test_a_profile_parameter_reaches_the_validator(
        self,
        client: httpx.AsyncClient,
        mock_http: respx.MockRouter,
        fhirpath_true: respx.Route,
        terminology_valid: list[respx.Route],
    ) -> None:
        del fhirpath_true, terminology_valid
        route = mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(
            return_value=fhir_json(operation_outcome())
        )

        await client.post("/fhir/R4/$validate", json=parameters_with(OBSERVATION, US_CORE_PATIENT))

        assert route.called
        assert route.calls.last.request.url.params["profile"] == US_CORE_PATIENT

    async def test_a_valueCanonical_profile_is_also_read(
        self,
        client: httpx.AsyncClient,
        mock_http: respx.MockRouter,
        fhirpath_true: respx.Route,
        terminology_valid: list[respx.Route],
    ) -> None:
        """FHIR allows either ``valueUri`` or ``valueCanonical`` for ``profile``."""
        del fhirpath_true, terminology_valid
        route = mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(
            return_value=fhir_json(operation_outcome())
        )
        payload = {
            "resourceType": "Parameters",
            "parameter": [
                {"name": "resource", "resource": OBSERVATION},
                {"name": "profile", "valueCanonical": US_CORE_PATIENT},
            ],
        }

        await client.post("/fhir/R4/$validate", json=payload)

        assert route.calls.last.request.url.params["profile"] == US_CORE_PATIENT

    async def test_the_verdict_matches_the_primary_endpoint(
        self, client: httpx.AsyncClient, mock_http: respx.MockRouter, fhirpath_true: respx.Route
    ) -> None:
        """The facade must not be a softer door into the same engine."""
        del fhirpath_true
        mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(
            return_value=fhir_json(
                operation_outcome(
                    {
                        "severity": "error",
                        "code": "structure",
                        "diagnostics": "Observation.status is required",
                        "expression": ["Observation.status"],
                    }
                )
            )
        )
        mock_http.post(url__regex=r".*\$validate-code").mock(
            return_value=fhir_json(
                {
                    "resourceType": "Parameters",
                    "parameter": [{"name": "result", "valueBoolean": True}],
                }
            )
        )

        facade = await client.post("/fhir/R4/$validate", json=OBSERVATION)
        primary = await client.post("/v1/validate/outcome", json={"resource": OBSERVATION})

        assert facade.status_code == primary.status_code
        assert _severities(facade.json()) == _severities(primary.json())

    async def test_an_outage_fails_closed(
        self, client: httpx.AsyncClient, mock_http: respx.MockRouter
    ) -> None:
        mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(
            side_effect=httpx.ConnectError("refused")
        )

        response = await client.post("/fhir/R4/$validate", json=OBSERVATION)

        assert response.status_code == 503
        assert "validator-unavailable" in response.text

    async def test_a_body_that_is_not_a_resource_is_rejected(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.post("/fhir/R4/$validate", json={"not": "a resource"})

        assert response.status_code == 400
        assert response.json()["resourceType"] == "OperationOutcome"

    async def test_a_parameters_wrapper_with_no_resource_is_rejected(
        self, client: httpx.AsyncClient
    ) -> None:
        payload = {
            "resourceType": "Parameters",
            "parameter": [
                None,
                "junk",
                {"name": "profile", "valueUri": US_CORE_PATIENT},
                {"name": "resource", "valueString": "not a resource"},
            ],
        }

        response = await client.post("/fhir/R4/$validate", json=payload)

        assert response.status_code == 400
        assert "resource" in response.text

    async def test_an_unauthenticated_caller_is_refused(
        self, anon_client: httpx.AsyncClient
    ) -> None:
        response = await anon_client.post("/fhir/R4/$validate", json=OBSERVATION)

        assert response.status_code == 401

    async def test_the_outcome_is_not_cached(
        self, client: httpx.AsyncClient, all_dependencies_healthy: None
    ) -> None:
        response = await client.post("/fhir/R4/$validate", json=OBSERVATION)

        assert response.headers["Cache-Control"] == "no-store"


def _severities(outcome: dict[str, Any]) -> list[str]:
    return [issue["severity"] for issue in outcome.get("issue", [])]


class TestPipelineOperationsAreHonestlyUnimplemented:
    @pytest.mark.parametrize("operation", ["$convert", "$extract"])
    async def test_it_returns_501_and_names_the_milestone(
        self, client: httpx.AsyncClient, operation: str
    ) -> None:
        response = await client.post(f"/fhir/R4/{operation}", json={})

        assert response.status_code == 501
        assert "M3" in response.json()["error"]["message"]

    @pytest.mark.parametrize("operation", ["$convert", "$extract"])
    async def test_it_points_at_the_endpoint_that_does_work(
        self, client: httpx.AsyncClient, operation: str
    ) -> None:
        response = await client.post(f"/fhir/R4/{operation}", json={})

        assert "/v1/validate" in response.json()["error"]["message"]

    @pytest.mark.parametrize("operation", ["$convert", "$extract"])
    async def test_it_still_requires_authentication(
        self, anon_client: httpx.AsyncClient, operation: str
    ) -> None:
        """A 501 that leaks whether a deployment exists is still a leak."""
        response = await anon_client.post(f"/fhir/R4/{operation}", json={})

        assert response.status_code == 401


class TestNoRepositoryEndpointsExist:
    """AGENTS.md 3: no general FHIR server. These must not be routable."""

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
