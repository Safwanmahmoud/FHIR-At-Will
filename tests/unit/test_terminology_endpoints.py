"""``POST /v1/terminology/validate-code`` and ``/map`` (AGENTS.md 11.4).

These two endpoints are the externally visible form of principle 2.3: a code is
only ever confirmed by the terminology server, never by this service. So the
tests assert the passthrough is honest — a "no" is reported as a "no", an outage
is reported as an outage and never as a "no", and nothing about the code reaches
a URL or a log line.

Every §21 error path is covered per endpoint: a malformed body, an unanswerable
request, an unauthenticated caller, and a dependency outage.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from tests.helpers import TERMINOLOGY_URL, fhir_json, operation_outcome, parameters

CODE_SYSTEM_VALIDATE = f"{TERMINOLOGY_URL}/CodeSystem/$validate-code"
VALUE_SET_VALIDATE = f"{TERMINOLOGY_URL}/ValueSet/$validate-code"
TRANSLATE = f"{TERMINOLOGY_URL}/ConceptMap/$translate"
EXPAND = f"{TERMINOLOGY_URL}/ValueSet/$expand"

LOINC = "http://loinc.org"
HEART_RATE = "8867-4"
VITALS_VALUE_SET = "http://hl7.org/fhir/ValueSet/observation-vitalsignresult"


def concept_map_result(*, result: bool, equivalence: str = "equivalent") -> dict[str, Any]:
    """A ``$translate`` response with one match."""
    return {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "result", "valueBoolean": result},
            {
                "name": "match",
                "part": [
                    {"name": "equivalence", "valueCode": equivalence},
                    {
                        "name": "concept",
                        "valueCoding": {
                            "system": "http://snomed.info/sct",
                            "code": "364075005",
                            "display": "Heart rate",
                        },
                    },
                ],
            },
        ],
    }


class TestValidateCode:
    async def test_a_confirmed_code_is_reported_as_confirmed(
        self, client: httpx.AsyncClient, mock_http: respx.MockRouter
    ) -> None:
        mock_http.post(CODE_SYSTEM_VALIDATE).mock(
            return_value=fhir_json(parameters(result=True, display="Heart rate", version="2.79"))
        )

        response = await client.post(
            "/v1/terminology/validate-code", json={"system": LOINC, "code": HEART_RATE}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["result"] is True
        assert body["code"] == HEART_RATE
        assert body["display"] == "Heart rate"

    async def test_a_rejected_code_is_reported_as_rejected(
        self, client: httpx.AsyncClient, mock_http: respx.MockRouter
    ) -> None:
        mock_http.post(CODE_SYSTEM_VALIDATE).mock(
            return_value=fhir_json(parameters(result=False, message="Unknown code"))
        )

        response = await client.post(
            "/v1/terminology/validate-code", json={"system": LOINC, "code": "not-a-loinc"}
        )

        assert response.status_code == 200
        assert response.json()["result"] is False

    async def test_a_value_set_check_goes_to_the_value_set_endpoint(
        self, client: httpx.AsyncClient, mock_http: respx.MockRouter
    ) -> None:
        route = mock_http.post(VALUE_SET_VALIDATE).mock(
            return_value=fhir_json(parameters(result=True))
        )

        response = await client.post(
            "/v1/terminology/validate-code",
            json={"system": LOINC, "code": HEART_RATE, "value_set": VITALS_VALUE_SET},
        )

        assert response.status_code == 200
        assert route.called
        assert response.json()["value_set"] == VITALS_VALUE_SET

    async def test_the_answer_is_never_cached_by_an_intermediary(
        self, client: httpx.AsyncClient, mock_http: respx.MockRouter
    ) -> None:
        """A shared cache holding a code-plus-ValueSet answer is a PHI leak."""
        mock_http.post(CODE_SYSTEM_VALIDATE).mock(return_value=fhir_json(parameters(result=True)))

        response = await client.post(
            "/v1/terminology/validate-code", json={"system": LOINC, "code": HEART_RATE}
        )

        assert response.headers["Cache-Control"] == "no-store"

    async def test_a_request_naming_neither_a_system_nor_a_value_set_is_rejected(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.post("/v1/terminology/validate-code", json={"code": HEART_RATE})

        assert response.status_code == 400
        assert "value_set" in response.text

    async def test_an_unknown_field_is_rejected_rather_than_ignored(
        self, client: httpx.AsyncClient
    ) -> None:
        """Silently dropping ``valueset`` would answer a different question."""
        response = await client.post(
            "/v1/terminology/validate-code",
            json={"system": LOINC, "code": HEART_RATE, "valueset": VITALS_VALUE_SET},
        )

        assert response.status_code == 400

    async def test_a_missing_code_is_rejected(self, client: httpx.AsyncClient) -> None:
        response = await client.post("/v1/terminology/validate-code", json={"system": LOINC})

        assert response.status_code == 400

    async def test_an_outage_fails_closed_rather_than_answering_false(
        self, client: httpx.AsyncClient, mock_http: respx.MockRouter
    ) -> None:
        """Principle 2.4. "The server did not answer" must never read as "invalid"."""
        mock_http.post(CODE_SYSTEM_VALIDATE).mock(
            side_effect=httpx.ConnectError("connection refused")
        )

        response = await client.post(
            "/v1/terminology/validate-code", json={"system": LOINC, "code": HEART_RATE}
        )

        assert response.status_code == 503
        assert "terminology-unavailable" in response.text
        assert "false" not in response.text.lower()

    async def test_an_unauthenticated_caller_is_refused(
        self, anon_client: httpx.AsyncClient
    ) -> None:
        response = await anon_client.post(
            "/v1/terminology/validate-code", json={"system": LOINC, "code": HEART_RATE}
        )

        assert response.status_code == 401

    async def test_the_code_does_not_appear_in_the_outbound_url(
        self, client: httpx.AsyncClient, mock_http: respx.MockRouter
    ) -> None:
        route = mock_http.post(CODE_SYSTEM_VALIDATE).mock(
            return_value=fhir_json(parameters(result=True))
        )

        await client.post(
            "/v1/terminology/validate-code", json={"system": LOINC, "code": HEART_RATE}
        )

        assert route.called
        url = str(route.calls.last.request.url)
        assert HEART_RATE not in url
        assert HEART_RATE.encode() in route.calls.last.request.content


class TestTerminologySearch:
    async def test_a_code_system_search_returns_candidates(
        self, client: httpx.AsyncClient, mock_http: respx.MockRouter
    ) -> None:
        route = mock_http.post(EXPAND).mock(
            return_value=fhir_json(
                {
                    "resourceType": "ValueSet",
                    "expansion": {
                        "total": 1,
                        "contains": [
                            {
                                "system": LOINC,
                                "code": HEART_RATE,
                                "display": "Heart rate",
                            }
                        ],
                    },
                }
            )
        )

        response = await client.post(
            "/v1/terminology/search",
            json={"query": "heart rate", "system": LOINC, "count": 10},
        )

        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert b"http://loinc.org/vs" in route.calls.last.request.content
        assert response.json()["candidates"] == [
            {"system": LOINC, "code": HEART_RATE, "display": "Heart rate"}
        ]

    async def test_a_search_requires_a_system_or_value_set(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.post(
            "/v1/terminology/search",
            json={"query": "heart rate"},
        )

        assert response.status_code == 400


class TestTerminologyMap:
    async def test_a_translation_returns_the_servers_matches(
        self, client: httpx.AsyncClient, mock_http: respx.MockRouter
    ) -> None:
        mock_http.post(TRANSLATE).mock(return_value=fhir_json(concept_map_result(result=True)))

        response = await client.post(
            "/v1/terminology/map",
            json={
                "system": LOINC,
                "code": HEART_RATE,
                "target_system": "http://snomed.info/sct",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["result"] is True
        assert body["matches"][0]["code"] == "364075005"
        assert body["matches"][0]["equivalence"] == "equivalent"

    async def test_no_match_is_reported_as_no_match(
        self, client: httpx.AsyncClient, mock_http: respx.MockRouter
    ) -> None:
        mock_http.post(TRANSLATE).mock(
            return_value=fhir_json(parameters(result=False, message="No matches"))
        )

        response = await client.post(
            "/v1/terminology/map",
            json={"system": LOINC, "code": HEART_RATE, "target_system": "http://snomed.info/sct"},
        )

        assert response.status_code == 200
        assert response.json() == {"result": False, "matches": [], "message": "No matches"}

    async def test_a_request_naming_no_target_is_rejected(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            "/v1/terminology/map", json={"system": LOINC, "code": HEART_RATE}
        )

        assert response.status_code == 400
        assert "target_system" in response.text

    async def test_a_missing_source_system_is_rejected(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            "/v1/terminology/map",
            json={"code": HEART_RATE, "target_system": "http://snomed.info/sct"},
        )

        assert response.status_code == 400

    async def test_an_outage_fails_closed(
        self, client: httpx.AsyncClient, mock_http: respx.MockRouter
    ) -> None:
        mock_http.post(TRANSLATE).mock(return_value=httpx.Response(502, text="bad gateway"))

        response = await client.post(
            "/v1/terminology/map",
            json={"system": LOINC, "code": HEART_RATE, "target_system": "http://snomed.info/sct"},
        )

        assert response.status_code == 503

    async def test_an_operation_outcome_from_the_server_fails_closed(
        self, client: httpx.AsyncClient, mock_http: respx.MockRouter
    ) -> None:
        """A server that answers with an OperationOutcome has not answered the question."""
        mock_http.post(TRANSLATE).mock(
            return_value=fhir_json(
                operation_outcome(
                    {"severity": "error", "code": "not-found", "diagnostics": "No ConceptMap"}
                )
            )
        )

        response = await client.post(
            "/v1/terminology/map",
            json={"system": LOINC, "code": HEART_RATE, "concept_map": "http://example.org/cm"},
        )

        assert response.status_code == 503

    async def test_an_unauthenticated_caller_is_refused(
        self, anon_client: httpx.AsyncClient
    ) -> None:
        response = await anon_client.post(
            "/v1/terminology/map",
            json={"system": LOINC, "code": HEART_RATE, "target_system": "http://snomed.info/sct"},
        )

        assert response.status_code == 401

    async def test_the_answer_is_never_cached_by_an_intermediary(
        self, client: httpx.AsyncClient, mock_http: respx.MockRouter
    ) -> None:
        mock_http.post(TRANSLATE).mock(return_value=fhir_json(concept_map_result(result=True)))

        response = await client.post(
            "/v1/terminology/map",
            json={"system": LOINC, "code": HEART_RATE, "target_system": "http://snomed.info/sct"},
        )

        assert response.headers["Cache-Control"] == "no-store"


class TestNoGetEndpointTakesClinicalText:
    """AGENTS.md 3: no ``GET`` endpoint accepts clinical text as a query parameter."""

    @pytest.mark.parametrize(
        "path",
        [
            "/v1/terminology/validate-code",
            "/v1/terminology/search",
            "/v1/terminology/map",
        ],
    )
    async def test_get_is_not_allowed(self, client: httpx.AsyncClient, path: str) -> None:
        response = await client.get(path, params={"system": LOINC, "code": HEART_RATE})

        assert response.status_code == 405
