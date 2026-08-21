"""The error model (AGENTS.md 12).

Two envelopes, one catalogue: domain and clinical failures render as a FHIR
``OperationOutcome``; platform failures render as ``{"error": {...}}``. The
status-code mapping is part of the contract — in particular the ``400``/``422``
split, which tells a client "you sent a malformed request" apart from "your
request was well-formed and I cannot process it".

``_sanitize_violations`` gets its own attention: pydantic's error list carries
the offending ``input``, which for this service is clinical narrative. That must
not reach a response body (principle 2.6).
"""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi import FastAPI

from fhirbridge.api.auth import Principal
from fhirbridge.api.deps import get_cascade, get_principal
from fhirbridge.api.errors import _CODE_BY_STATUS, error_for_status
from fhirbridge.domain.errors import (
    ERROR_SPECS,
    DomainError,
    ErrorCategory,
    ErrorCode,
    InvalidRequestError,
    PlatformError,
    TerminologyUnavailableError,
)
from fhirbridge.validation.cascade import ValidationCascade
from tests.helpers import OBSERVATION, VALIDATOR_URL

NARRATIVE = "Patient Jane Q. Public, MRN 998877, denies chest pain."


class TestStatusMapping:
    def test_every_mapped_status_keeps_its_own_status(self) -> None:
        """Several statuses share one machine code; they must not share a status."""
        for status in _CODE_BY_STATUS:
            assert error_for_status(status).http_status == status

    def test_the_code_is_still_the_catalogue_code(self) -> None:
        assert error_for_status(405).code is ErrorCode.INVALID_REQUEST
        assert error_for_status(422).code is ErrorCode.INVALID_REQUEST
        assert error_for_status(404).code is ErrorCode.NOT_FOUND

    def test_an_unmapped_status_becomes_an_internal_error(self) -> None:
        error = error_for_status(418)

        assert error.code is ErrorCode.INTERNAL_ERROR
        assert error.http_status == 418

    def test_without_an_override_the_spec_status_is_used(self) -> None:
        assert InvalidRequestError().http_status == 400
        assert TerminologyUnavailableError().http_status == 503

    async def test_a_wrong_method_stays_a_405(self, client: httpx.AsyncClient) -> None:
        """Collapsing 405 onto 400 would tell the client its body was wrong."""
        response = await client.get("/v1/validate")

        assert response.status_code == 405

    async def test_a_405_still_advertises_the_allowed_methods(
        self, client: httpx.AsyncClient
    ) -> None:
        """``Allow`` is part of the HTTP contract and must survive re-rendering."""
        response = await client.get("/v1/validate")

        assert "POST" in response.headers.get("allow", "")

    async def test_an_unknown_path_is_a_404_in_the_platform_envelope(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get("/v1/nope")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not-found"


class TestEnvelopes:
    async def test_a_domain_error_renders_as_an_operation_outcome(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.post("/fhir/R4/$validate", json={"no": "resourceType"})

        assert response.headers["content-type"].startswith("application/fhir+json")
        body = response.json()
        assert body["resourceType"] == "OperationOutcome"
        assert body["issue"][0]["severity"] == "error"
        assert body["issue"][0]["code"] == "structure"

    async def test_a_platform_error_renders_as_the_json_envelope(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.post("/v1/translate/hl7v2", json={})

        assert response.headers["content-type"].startswith("application/json")
        error = response.json()["error"]
        assert set(error) >= {"code", "message", "trace_id", "details"}

    def test_every_code_declares_which_envelope_it_uses(self) -> None:
        for spec in ERROR_SPECS.values():
            assert spec.category in {ErrorCategory.DOMAIN, ErrorCategory.PLATFORM}

    def test_the_dependency_errors_are_domain_errors_that_fail_closed(self) -> None:
        """Principle 2.4: a 503 with ``Retry-After``, never a soft pass."""
        error = TerminologyUnavailableError()

        assert isinstance(error, DomainError)
        assert error.http_status == 503
        assert error.retry_after_s is not None
        assert error.spec.retryable is True

    async def test_a_503_carries_retry_after(
        self, client: httpx.AsyncClient, mock_http: respx.MockRouter
    ) -> None:
        mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(
            side_effect=httpx.ConnectError("refused")
        )

        response = await client.post("/v1/validate", json={"resource": OBSERVATION})

        assert response.status_code == 503
        assert int(response.headers["Retry-After"]) > 0


class TestNoPhiInErrorBodies:
    async def test_a_schema_violation_does_not_echo_the_offending_value(
        self, client: httpx.AsyncClient
    ) -> None:
        """Pydantic's ``input`` field would carry the narrative straight back out."""
        response = await client.post(
            "/v1/validate",
            json={"resource": {"resourceType": "Observation"}, "profiles": NARRATIVE},
        )

        assert response.status_code == 400
        assert NARRATIVE not in response.text
        assert "998877" not in response.text

    async def test_a_violation_still_says_where_the_problem_is(
        self, client: httpx.AsyncClient
    ) -> None:
        """Redaction must not make the error useless."""
        response = await client.post(
            "/v1/validate",
            json={"resource": {"resourceType": "Observation"}, "max_terminology_checks": 99999},
        )

        violations = response.json()["error"]["details"]["violations"]
        assert violations
        assert any("max_terminology_checks" in v["location"] for v in violations)
        assert all(set(v) == {"location", "type", "message"} for v in violations)

    async def test_the_violation_list_is_bounded(self, client: httpx.AsyncClient) -> None:
        """A pathological body should not produce a megabyte of error detail."""
        response = await client.post(
            "/v1/validate",
            json={"resource": {"resourceType": "Observation"}, "layers": ["nope"] * 200},
        )

        assert len(response.json()["error"]["details"]["violations"]) <= 50

    async def test_an_unhandled_exception_returns_a_generic_message(
        self, app: FastAPI, principal: Principal, mock_http: respx.MockRouter
    ) -> None:
        """An exception string can hold anything the failing frame was holding."""
        del mock_http

        def explode() -> ValidationCascade:
            raise RuntimeError(f"boom while handling {NARRATIVE}")

        app.dependency_overrides[get_principal] = lambda: principal
        app.dependency_overrides[get_cascade] = explode

        # ``raise_app_exceptions=False`` because Starlette re-raises after its
        # 500 handler runs, so the default transport would surface the exception
        # instead of the response body this test is about.
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            response = await client.post("/v1/validate", json={"resource": OBSERVATION})

        assert response.status_code == 500
        assert NARRATIVE not in response.text
        assert "998877" not in response.text
        assert "boom" not in response.text

        # The trace_id is the only thing connecting this response to the log line
        # that does hold the traceback, so a 500 without one is unsupportable.
        assert response.json()["error"]["trace_id"]
        assert response.headers["X-Trace-Id"] == response.json()["error"]["trace_id"]
        assert response.headers["X-Request-Id"]


class TestSafeContext:
    def test_context_is_scalars_only(self) -> None:
        """A dict or list in ``safe_context`` is how clinical content sneaks in."""
        error = PlatformError("nope", code=ErrorCode.INVALID_REQUEST)
        error.with_context(resource_type="Observation", layer_number=3, blocking=True)

        assert all(
            isinstance(value, str | int | float | bool) for value in error.safe_context.values()
        )

    @pytest.mark.parametrize("code", list(ErrorCode))
    def test_every_code_has_a_spec_and_a_fhir_issue_type(self, code: ErrorCode) -> None:
        spec = ERROR_SPECS[code]

        assert spec.title
        assert spec.issue_type
        assert 400 <= spec.http_status <= 599
