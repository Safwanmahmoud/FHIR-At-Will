"""Principle 2.6: PHI lives in request bodies only — never in URLs, logs or metrics.

The routing tests are static (they inspect the app's own route table), so a
future endpoint that takes clinical text as a query parameter fails CI the moment
it is added rather than the day a proxy log is subpoenaed.
"""

from __future__ import annotations

import logging

import httpx
import pytest
import respx
from fastapi import FastAPI

from fhirbridge.observability import metrics
from tests.helpers import OBSERVATION, TERMINOLOGY_URL, api_routes

pytestmark = pytest.mark.security

CLINICAL_PARAMETER_NAMES = {
    "text",
    "note",
    "narrative",
    "resource",
    "bundle",
    "document",
    "content",
    "code",
    "codes",
    "display",
    "patient",
    "mrn",
    "name",
    "birth_date",
    "birthdate",
    "value_set",
    "system",
}


def test_route_discovery_is_not_vacuous(app: FastAPI) -> None:
    """The static checks below are worthless if they inspect an empty list."""
    paths = {route.path for route in api_routes(app)}

    assert {"/v1/validate", "/v1/NAR2FHIR"} <= paths


def test_no_route_accepts_clinical_content_as_a_query_parameter(app: FastAPI) -> None:
    offenders: list[str] = []
    for route in api_routes(app):
        for field in route.dependant.query_params:
            if field.name.lower() in CLINICAL_PARAMETER_NAMES:
                offenders.append(f"{sorted(route.methods)} {route.path}?{field.name}")

    assert not offenders, "these routes take clinical content in the query string: " + ", ".join(
        offenders
    )


def test_no_route_accepts_clinical_content_in_a_path_segment(app: FastAPI) -> None:
    offenders = [
        f"{sorted(route.methods)} {route.path}"
        for route in api_routes(app)
        for field in route.dependant.path_params
        if field.name.lower() in CLINICAL_PARAMETER_NAMES
    ]

    assert not offenders, "path parameters must be opaque ids: " + ", ".join(offenders)


def test_endpoints_that_take_clinical_content_are_not_get(app: FastAPI) -> None:
    """AGENTS.md 3: no GET endpoint accepts clinical text, ever."""
    offenders = [
        f"{sorted(route.methods)} {route.path}"
        for route in api_routes(app)
        if route.body_field is not None and route.methods & {"GET", "HEAD", "DELETE"}
    ]

    assert not offenders, "these routes carry a body on a safe method: " + ", ".join(offenders)


async def test_outbound_terminology_calls_put_codes_in_the_body(
    client: httpx.AsyncClient, mock_http: respx.MockRouter, all_dependencies_healthy: None
) -> None:
    """The code must not reach the terminology server's access log either."""
    await client.post("/v1/validate", json={"resource": OBSERVATION})

    calls = [call for call in mock_http.calls if TERMINOLOGY_URL in str(call.request.url)]
    assert calls, "expected at least one terminology call"
    for call in calls:
        assert call.request.method == "POST"
        assert not call.request.url.query
        assert "8867-4" not in str(call.request.url)
    # The code was still sent — in the Parameters body, where it belongs.
    assert any(b"8867-4" in call.request.content for call in calls)


async def test_metrics_carry_no_identifiers(
    client: httpx.AsyncClient, all_dependencies_healthy: None
) -> None:
    """Metric labels are a PHI leak *and* a cardinality bomb (AGENTS.md 15)."""
    await client.post("/v1/validate", json={"resource": OBSERVATION})

    rendered = metrics.render().decode()

    for forbidden in ("8867-4", "obs-1", "Patient/example", "Heart rate", "loinc.org"):
        assert forbidden not in rendered
    # The route template is present; the concrete path never is.
    assert 'route="/v1/validate"' in rendered


async def test_access_log_records_the_route_template_not_the_path(
    client: httpx.AsyncClient,
    all_dependencies_healthy: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        await client.post("/v1/validate", json={"resource": OBSERVATION})

    records = [record for record in caplog.records if record.message == "http_request"]
    assert records
    assert records[0].route == "/v1/validate"
    assert not hasattr(records[0], "path")


async def test_validation_logs_carry_counts_not_content(
    client: httpx.AsyncClient,
    all_dependencies_healthy: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue messages quote element values, so they may be returned but not logged."""
    with caplog.at_level(logging.INFO):
        await client.post("/v1/validate", json={"resource": OBSERVATION})

    serialized = "\n".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    for forbidden in ("8867-4", "obs-1", "Patient/example", "Heart rate"):
        assert forbidden not in serialized


async def test_trace_and_request_ids_are_returned_for_correlation(
    client: httpx.AsyncClient, all_dependencies_healthy: None
) -> None:
    """The only way to investigate a request without logging its content."""
    response = await client.post(
        "/v1/validate",
        json={"resource": OBSERVATION},
        headers={"X-Request-Id": "client-supplied-id"},
    )

    assert response.headers["x-request-id"] == "client-supplied-id"
    assert response.headers["x-trace-id"]


async def test_hostile_request_id_is_replaced(
    client: httpx.AsyncClient, all_dependencies_healthy: None
) -> None:
    """The echoed id reaches every log line, so it is untrusted input."""
    response = await client.post(
        "/v1/validate",
        json={"resource": OBSERVATION},
        headers={"X-Request-Id": 'evil","injected":"value'},
    )

    assert response.headers["x-request-id"] != 'evil","injected":"value'
