"""``POST /v1/validate`` behaviour (AGENTS.md 11.4; M1 acceptance criteria).

These run the real cascade against an intercepted validator and terminology
server, so they cover request construction, response parsing and report
assembly rather than a stubbed happy path.
"""

from __future__ import annotations

import json

import httpx
import respx

from tests.helpers import (
    OBSERVATION,
    TERMINOLOGY_URL,
    US_CORE_PATIENT,
    VALIDATOR_URL,
    fhir_json,
    operation_outcome,
    parameters,
)


async def test_clean_resource_is_reported_conformant(
    client: httpx.AsyncClient,
    validator_clean: respx.Route,
    fhirpath_true: respx.Route,
    terminology_valid: list[respx.Route],
) -> None:
    response = await client.post("/v1/validate", json={"resource": OBSERVATION})

    assert response.status_code == 200
    report = response.json()
    assert report["conformant"] is True
    assert report["resource_type"] == "Observation"
    assert report["resource_count"] == 1
    assert report["scores"]["conformance"] == 1.0
    assert validator_clean.called
    assert fhirpath_true.called
    assert any(route.called for route in terminology_valid)


async def test_every_layer_appears_in_the_report(
    client: httpx.AsyncClient, all_dependencies_healthy: None
) -> None:
    """A layer that did not run must say so, not be absent (AGENTS.md 10)."""
    response = await client.post("/v1/validate", json={"resource": OBSERVATION})

    layers = {layer["layer"]: layer for layer in response.json()["layers"]}
    assert set(layers) == {
        "structural",
        "profile",
        "terminology",
        "invariants",
        "plausibility",
        "fidelity",
        "coverage",
        "routing",
    }
    for name in ("fidelity", "coverage"):
        assert layers[name]["status"] == "not_applicable"
        assert layers[name]["skipped_reason"]


async def test_bare_fhir_body_is_accepted(
    client: httpx.AsyncClient, all_dependencies_healthy: None
) -> None:
    response = await client.post(
        "/v1/validate",
        content=json.dumps(OBSERVATION).encode(),
        headers={"Content-Type": "application/fhir+json"},
    )

    assert response.status_code == 200
    assert response.json()["resource_type"] == "Observation"


async def test_profile_errors_reject_the_resource(
    client: httpx.AsyncClient,
    mock_http: respx.MockRouter,
    fhirpath_true: respx.Route,
    terminology_valid: list[respx.Route],
) -> None:
    mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(
        return_value=fhir_json(
            operation_outcome(
                {
                    "severity": "error",
                    "code": "required",
                    "diagnostics": "Patient.identifier: minimum required = 1, but only found 0",
                    "expression": ["Patient.identifier"],
                }
            )
        )
    )

    response = await client.post(
        "/v1/validate",
        json={"resource": {"resourceType": "Patient", "id": "p1"}, "profiles": [US_CORE_PATIENT]},
    )

    assert response.status_code == 200
    report = response.json()
    assert report["conformant"] is False
    assert report["status"] == "reject"
    assert report["scores"]["conformance"] == 0.0


async def test_unresolvable_profile_is_not_a_pass(
    client: httpx.AsyncClient, mock_http: respx.MockRouter
) -> None:
    """A missing IG must fail the request, never yield a clean report."""
    mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(
        return_value=fhir_json(
            operation_outcome(
                {
                    "severity": "warning",
                    "code": "not-supported",
                    "diagnostics": (
                        f"Profile reference '{US_CORE_PATIENT}' has not been checked "
                        "because it could not be found"
                    ),
                }
            )
        )
    )

    response = await client.post(
        "/v1/validate",
        json={"resource": {"resourceType": "Patient", "id": "p1"}, "profiles": [US_CORE_PATIENT]},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["resourceType"] == "OperationOutcome"
    assert body["issue"][0]["details"]["coding"][0]["code"] == "ig-not-loaded"


async def test_unknown_resource_type_fails_structurally(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/validate", json={"resource": {"resourceType": "NotAResource"}}
    )

    assert response.status_code == 200
    report = response.json()
    assert report["conformant"] is False
    layers = {layer["layer"]: layer for layer in report["layers"]}
    assert layers["structural"]["status"] == "failed"
    assert layers["profile"]["status"] == "skipped"


async def test_report_carries_the_full_version_set(
    client: httpx.AsyncClient, all_dependencies_healthy: None
) -> None:
    """Principle 2.8: every artifact records the pins that produced it."""
    response = await client.post("/v1/validate", json={"resource": OBSERVATION})

    versions = response.json()["versions"]
    assert versions["fhir"] == "4.0.1"
    assert versions["validator"] == "6.9.8"
    assert versions["ig"] == ["hl7.fhir.us.core#9.0.0"]
    assert versions["terminology"] == {"snomed": "INT-20260501", "loinc": "2.79"}
    assert versions["code"]
    assert versions["report_schema"]


async def test_layer_selection_marks_the_rest_skipped(
    client: httpx.AsyncClient, mock_http: respx.MockRouter
) -> None:
    response = await client.post(
        "/v1/validate", json={"resource": OBSERVATION, "layers": ["structural"]}
    )

    assert response.status_code == 200
    report = response.json()
    layers = {layer["layer"]: layer for layer in report["layers"]}
    assert layers["structural"]["status"] == "passed"
    assert layers["profile"]["status"] == "skipped"
    # Nothing was asked of the validator, so nothing was sent to it.
    assert not mock_http.calls
    # A skipped blocking layer must not produce an automatic pass.
    assert report["status"] == "needs_review"


async def test_terminology_rejects_an_unknown_code(
    client: httpx.AsyncClient,
    mock_http: respx.MockRouter,
    validator_clean: respx.Route,
    fhirpath_true: respx.Route,
) -> None:
    for path in ("/CodeSystem/$validate-code", "/ValueSet/$validate-code"):
        mock_http.post(f"{TERMINOLOGY_URL}{path}").mock(
            return_value=fhir_json(
                parameters(result=False, message="Unknown code '8867-4' in LOINC")
            )
        )

    response = await client.post("/v1/validate", json={"resource": OBSERVATION})

    report = response.json()
    layers = {layer["layer"]: layer for layer in report["layers"]}
    assert layers["terminology"]["errors"] >= 1
    assert report["conformant"] is False


async def test_response_is_not_cacheable(
    client: httpx.AsyncClient, all_dependencies_healthy: None
) -> None:
    """The request body may be PHI; no intermediary should retain the exchange."""
    response = await client.post("/v1/validate", json={"resource": OBSERVATION})

    assert response.headers["cache-control"] == "no-store"


async def test_outcome_variant_returns_fhir_json(
    client: httpx.AsyncClient, all_dependencies_healthy: None
) -> None:
    response = await client.post("/v1/validate/outcome", json={"resource": OBSERVATION})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/fhir+json")
    body = response.json()
    assert body["resourceType"] == "OperationOutcome"
    # Layers that did not run are reported, so a caller reading only the outcome
    # still learns which checks were not performed.
    assert any("fidelity" in issue["details"]["text"] for issue in body["issue"])


async def test_malformed_body_is_a_client_error(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/validate", content=b"{not json", headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid-request"
