"""The 501 translation stubs (AGENTS.md 3, 11.4).

HL7 v2, C-CDA and tabular conversion are out of scope for v1 on purpose: they
have published grammars and deterministic converters, and an LLM is the wrong
tool for a format with a spec. These tests pin two things: that the endpoints
exist so clients can code against the shape, and that the 501 body names the
right tool instead of just saying "no".
"""

from __future__ import annotations

import httpx
import pytest

FORMATS = ["hl7v2", "cda", "tabular"]


@pytest.mark.parametrize("fmt", FORMATS)
class TestTranslationStubs:
    async def test_it_returns_501(self, client: httpx.AsyncClient, fmt: str) -> None:
        response = await client.post(f"/v1/translate/{fmt}", json={})

        assert response.status_code == 501
        assert response.json()["error"]["code"] == "not-implemented"

    async def test_it_names_a_deterministic_alternative(
        self, client: httpx.AsyncClient, fmt: str
    ) -> None:
        message = (await client.post(f"/v1/translate/{fmt}", json={})).json()["error"]["message"]

        assert "deterministic" in message
        assert "/v1/validate" in message

    async def test_it_carries_a_trace_id_for_support(
        self, client: httpx.AsyncClient, fmt: str
    ) -> None:
        body = (await client.post(f"/v1/translate/{fmt}", json={})).json()

        assert body["error"]["trace_id"]

    async def test_it_still_requires_authentication(
        self, anon_client: httpx.AsyncClient, fmt: str
    ) -> None:
        response = await anon_client.post(f"/v1/translate/{fmt}", json={})

        assert response.status_code == 401

    async def test_get_is_not_offered(self, client: httpx.AsyncClient, fmt: str) -> None:
        """AGENTS.md 3: no GET endpoint takes clinical text."""
        response = await client.get(f"/v1/translate/{fmt}")

        assert response.status_code == 405


async def test_the_hl7v2_stub_names_the_hl7v2_tooling(client: httpx.AsyncClient) -> None:
    message = (await client.post("/v1/translate/hl7v2", json={})).json()["error"]["message"]

    assert "Microsoft FHIR Converter" in message


async def test_the_cda_stub_explains_the_narrative_route(client: httpx.AsyncClient) -> None:
    """A C-CDA's narrative sections *are* in scope, once the pipeline ships."""
    message = (await client.post("/v1/translate/cda", json={})).json()["error"]["message"]

    assert "/v1/conversions" in message


async def test_no_chat_or_prompt_passthrough_endpoint_exists(client: httpx.AsyncClient) -> None:
    """AGENTS.md 3 forbids this outright: it destroys reproducibility.

    Asserted as a test, not just a policy, so that adding one breaks the build.
    """
    for path in ("/v1/chat", "/chat", "/v1/completions", "/v1/prompt", "/v1/llm/chat"):
        assert (await client.post(path, json={})).status_code == 404
