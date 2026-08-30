"""Principle 2.4: when a verifier is unavailable, the request fails.

This is the property that makes every conformance claim this service publishes
meaningful. If a terminology outage silently degraded to "code accepted", or a
validator outage to "no issues found", the report would be worse than useless —
it would be confidently wrong.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from fhirbridge.api.deps import AppServices
from fhirbridge.config import Settings
from fhirbridge.storage.models import TENANT_SCOPED_TABLES
from tests.helpers import OBSERVATION, TERMINOLOGY_URL, VALIDATOR_URL, fhir_json, parameters

pytestmark = pytest.mark.security


def _bypassing_session_factory() -> Any:
    """A session whose database reports that no policy applies to this role.

    This is what Postgres answers when the application connects as a superuser
    or a ``BYPASSRLS`` role — the shape verified against a real server in
    ``tests/integration/test_row_level_security.py``.
    """

    rows = [
        SimpleNamespace(table_name=table, active=False, enabled=True, forced=True)
        for table in TENANT_SCOPED_TABLES
    ]

    class _Result:
        def scalar_one(self) -> object:
            return "postgres"

        def all(self) -> list[Any]:
            return rows

    class _Session:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def execute(self, *_: object, **__: object) -> _Result:
            return _Result()

    return _Session


async def test_validator_connection_failure_returns_503(
    client: httpx.AsyncClient, mock_http: respx.MockRouter
) -> None:
    mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    response = await client.post("/v1/validate", json={"resource": OBSERVATION})

    assert response.status_code == 503
    body = response.json()
    assert body["resourceType"] == "OperationOutcome"
    assert body["issue"][0]["details"]["coding"][0]["code"] == "validator-unavailable"
    assert response.headers["retry-after"] == "30"


async def test_validator_timeout_returns_503(
    client: httpx.AsyncClient, mock_http: respx.MockRouter
) -> None:
    mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )

    response = await client.post("/v1/validate", json={"resource": OBSERVATION})

    assert response.status_code == 503
    assert response.json()["issue"][0]["details"]["coding"][0]["code"] == "validator-unavailable"


async def test_validator_5xx_returns_503(
    client: httpx.AsyncClient, mock_http: respx.MockRouter
) -> None:
    mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(
        return_value=httpx.Response(500, text="internal error")
    )

    response = await client.post("/v1/validate", json={"resource": OBSERVATION})

    assert response.status_code == 503


async def test_validator_garbage_response_returns_503(
    client: httpx.AsyncClient, mock_http: respx.MockRouter
) -> None:
    """A 200 that is not an OperationOutcome is not a pass."""
    mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(
        return_value=httpx.Response(200, text="<html>proxy error</html>")
    )

    response = await client.post("/v1/validate", json={"resource": OBSERVATION})

    assert response.status_code == 503


async def test_terminology_outage_returns_503(
    client: httpx.AsyncClient,
    mock_http: respx.MockRouter,
    validator_clean: respx.Route,
    fhirpath_true: respx.Route,
) -> None:
    for path in ("/CodeSystem/$validate-code", "/ValueSet/$validate-code"):
        mock_http.post(f"{TERMINOLOGY_URL}{path}").mock(
            side_effect=httpx.ConnectError("connection refused")
        )

    response = await client.post("/v1/validate", json={"resource": OBSERVATION})

    assert response.status_code == 503
    body = response.json()
    assert body["issue"][0]["details"]["coding"][0]["code"] == "terminology-unavailable"
    assert response.headers["retry-after"] == "30"


async def test_readiness_reports_degraded_when_the_ig_is_missing(
    client: httpx.AsyncClient, mock_http: respx.MockRouter, database_up: None
) -> None:
    """A reachable validator without US Core cannot support conformance claims."""
    mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(
        return_value=fhir_json(
            {
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "warning",
                        "code": "not-supported",
                        "diagnostics": (
                            "Profile reference 'http://hl7.org/fhir/us/core/"
                            "StructureDefinition/us-core-patient' could not be resolved"
                        ),
                    }
                ],
            }
        )
    )
    mock_http.get(f"{TERMINOLOGY_URL}/metadata").mock(
        return_value=fhir_json({"resourceType": "CapabilityStatement", "fhirVersion": "4.0.1"})
    )
    mock_http.post(f"{TERMINOLOGY_URL}/CodeSystem/$lookup").mock(
        return_value=fhir_json(parameters(display="probe"))
    )

    response = await client.get("/v1/health/dependencies")

    body = response.json()
    validator = next(item for item in body["dependencies"] if item["name"] == "validator")
    assert validator["status"] == "degraded"
    assert body["status"] == "degraded"


class TestReadinessWhenTenantIsolationIsNotEnforced:
    """Fail-closed applied to RLS, not just to the verifiers.

    A database that answers every query is "up" by any ordinary probe. If its
    policies do not apply to the connected role, though, the service can serve
    one tenant's chart to another — so reachability is not readiness. See
    ``fhirbridge.storage.rls``.
    """

    @pytest.fixture
    def other_dependencies_up(
        self, mock_http: respx.MockRouter, all_dependencies_healthy: None
    ) -> None:
        """Everything except Postgres answering, so RLS is the only variable."""
        del all_dependencies_healthy
        mock_http.get(f"{TERMINOLOGY_URL}/metadata").mock(
            return_value=fhir_json({"resourceType": "CapabilityStatement", "fhirVersion": "4.0.1"})
        )
        mock_http.post(f"{TERMINOLOGY_URL}/CodeSystem/$lookup").mock(
            return_value=fhir_json(parameters(display="probe"))
        )

    async def test_readyz_refuses_to_serve(
        self,
        anon_client: httpx.AsyncClient,
        services: AppServices,
        other_dependencies_up: None,
    ) -> None:
        services.session_factory = _bypassing_session_factory()

        response = await anon_client.get("/readyz")

        assert response.status_code == 503
        assert response.headers["retry-after"] == "15"
        body = response.json()
        assert body["ready"] is False
        postgres = next(item for item in body["dependencies"] if item["name"] == "postgres")
        assert postgres["status"] == "down"

    async def test_the_detail_says_what_to_change_without_leaking(
        self,
        client: httpx.AsyncClient,
        services: AppServices,
        other_dependencies_up: None,
    ) -> None:
        """An operator needs the fix; nobody needs the DSN."""
        services.session_factory = _bypassing_session_factory()

        response = await client.get("/v1/health/dependencies")

        postgres = next(
            item for item in response.json()["dependencies"] if item["name"] == "postgres"
        )
        detail = postgres["detail"]
        assert "BYPASSRLS" in detail
        assert "docs/deployment.md" in detail
        assert "password" not in detail.lower()
        assert "postgresql://" not in detail

    async def test_the_gate_can_be_lowered_deliberately(
        self,
        client: httpx.AsyncClient,
        settings: Settings,
        services: AppServices,
        other_dependencies_up: None,
    ) -> None:
        """``REQUIRE_RLS_ENFORCEMENT=false`` degrades rather than refuses.

        Some deployments genuinely cannot create a second role — a managed
        instance where the application owns the schema, say. They may opt out,
        but the status still says degraded and the detail still explains why, so
        the choice is visible rather than invisible.
        """
        object.__setattr__(settings, "require_rls_enforcement", False)
        services.session_factory = _bypassing_session_factory()

        response = await client.get("/v1/health/dependencies")

        assert response.status_code == 200
        postgres = next(
            item for item in response.json()["dependencies"] if item["name"] == "postgres"
        )
        assert postgres["status"] == "degraded"
        assert postgres["detail"]
