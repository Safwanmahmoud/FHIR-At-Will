"""``POST /v1/convert`` and ``POST /v1/llm/probe``.

The gateway is scripted here (``FakeLlmGateway``): the point is what the endpoint
does with the model's output, above all that it runs it through the real cascade
and stamps the generation provenance, rather than how the bytes were fetched.
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI

from fhirbridge.api.auth import Principal
from fhirbridge.api.deps import get_llm_gateway, get_principal
from fhirbridge.domain.errors import EgressBlockedError, LlmSchemaViolationError
from tests.fakes import FakeLlmGateway
from tests.helpers import OBSERVATION

BYOK_HEADERS = {
    "X-LLM-Provider": "openrouter",
    "X-LLM-Model": "openai/gpt-4o-mini",
    "X-LLM-API-Key": "sk-test",
    "X-PHI-Egress-Acknowledged": "true",
}


class TestConvert:
    async def test_the_generated_bundle_is_run_through_the_cascade(
        self, app: FastAPI, client: httpx.AsyncClient, all_dependencies_healthy: None
    ) -> None:
        gateway = FakeLlmGateway(resource=dict(OBSERVATION))
        app.dependency_overrides[get_llm_gateway] = lambda: gateway

        response = await client.post("/v1/convert", json={"text": "HR 72"}, headers=BYOK_HEADERS)

        assert response.status_code == 200
        body = response.json()
        assert body["bundle"]["resourceType"] == "Observation"
        assert body["report"]["resource_type"] == "Observation"
        # The output was actually generated and actually measured.
        assert gateway.complete_calls
        assert body["report"]["layers"], "the cascade did not run over the generated output"

    async def test_it_stamps_the_generation_provenance(
        self, app: FastAPI, client: httpx.AsyncClient, all_dependencies_healthy: None
    ) -> None:
        """A validate-only report leaves model/prompt_set empty; a conversion must not."""
        gateway = FakeLlmGateway(resource=dict(OBSERVATION))
        app.dependency_overrides[get_llm_gateway] = lambda: gateway

        body = (
            await client.post("/v1/convert", json={"text": "HR 72"}, headers=BYOK_HEADERS)
        ).json()

        assert body["conversion_id"].startswith("cnv_")
        assert body["report"]["conversion_id"] == body["conversion_id"]
        assert body["report"]["versions"]["model"] == {"convert": gateway.model}
        assert body["report"]["versions"]["prompt_set"]
        assert body["llm"]["model"] == gateway.model
        assert body["llm"]["qualification_tier"] == "silver"

    async def test_it_requires_the_conversions_write_scope(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        app.dependency_overrides[get_principal] = lambda: Principal(
            tenant_id="ten_x", actor_type="api_key", actor_id="key_x", scopes=frozenset()
        )
        app.dependency_overrides[get_llm_gateway] = lambda: FakeLlmGateway(
            resource=dict(OBSERVATION)
        )

        response = await client.post("/v1/convert", json={"text": "x"}, headers=BYOK_HEADERS)

        assert response.status_code == 403

    async def test_a_missing_llm_key_is_a_credentials_error(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.post(
            "/v1/convert",
            json={"text": "x"},
            headers={"X-LLM-Model": "openai/gpt-4o-mini"},
        )

        assert response.status_code == 400
        assert (
            response.json()["issue"][0]["details"]["coding"][0]["code"]
            == "llm-credentials-required"
        )

    async def test_an_egress_block_from_the_gateway_surfaces_as_451(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        gateway = FakeLlmGateway(error=EgressBlockedError("blocked", safe_context={"host": "x"}))
        app.dependency_overrides[get_llm_gateway] = lambda: gateway

        response = await client.post("/v1/convert", json={"text": "x"}, headers=BYOK_HEADERS)

        assert response.status_code == 451

    async def test_a_schema_violation_from_the_model_surfaces_as_422(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        gateway = FakeLlmGateway(error=LlmSchemaViolationError("not an object"))
        app.dependency_overrides[get_llm_gateway] = lambda: gateway

        response = await client.post("/v1/convert", json={"text": "x"}, headers=BYOK_HEADERS)

        assert response.status_code == 422

    async def test_the_narrative_is_never_echoed_in_an_error_body(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        gateway = FakeLlmGateway(error=LlmSchemaViolationError("not an object"))
        app.dependency_overrides[get_llm_gateway] = lambda: gateway
        secret_note = "PATIENT-NAME-DO-NOT-LEAK"

        response = await client.post(
            "/v1/convert", json={"text": secret_note}, headers=BYOK_HEADERS
        )

        assert secret_note not in response.text


class TestProbe:
    async def test_it_reports_connectivity_and_tier(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        gateway = FakeLlmGateway()
        app.dependency_overrides[get_llm_gateway] = lambda: gateway

        response = await client.post("/v1/llm/probe", headers=BYOK_HEADERS)

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["model"] == gateway.model
        assert body["qualification_tier"] == "silver"
        assert gateway.probe_calls

    async def test_it_requires_authentication(self, anon_client: httpx.AsyncClient) -> None:
        response = await anon_client.post("/v1/llm/probe", headers=BYOK_HEADERS)

        assert response.status_code == 401
