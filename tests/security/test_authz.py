"""Every endpoint's authentication and transport requirements (AGENTS.md 21).

The endpoint inventory is derived from the app itself, so a new route is covered
the moment it is added: it either appears in ``PUBLIC_PATHS`` with a stated
reason, or it must demand a credential.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from fhirbridge.api.deps import get_principal
from tests.helpers import api_routes

pytestmark = pytest.mark.security

PUBLIC_PATHS: dict[str, str] = {
    "/livez": "liveness must not depend on anything, including auth",
    "/readyz": "orchestrators probe this before a credential exists",
    "/version": "build metadata, no tenant data",
    "/metrics": "scraped by Prometheus; protect at the network layer",
    "/openapi.json": "the contract clients generate SDKs from",
    "/docs": "interactive docs for the contract",
    "/docs/oauth2-redirect": "part of the docs UI",
    "/v1/error-codes": "clients need the code list before they have a key",
    "/fhir/R4/metadata": "FHIR clients read CapabilityStatement unauthenticated",
}


def _authenticated_routes(app: FastAPI) -> list[APIRoute]:
    return [route for route in api_routes(app) if route.path not in PUBLIC_PATHS]


def test_every_non_public_route_requires_authentication(app: FastAPI) -> None:
    """Route-level check: the principal dependency must actually be wired in."""
    routes = _authenticated_routes(app)
    assert len(routes) >= 6, "route discovery found nothing; the check would pass vacuously"

    offenders = [
        f"{sorted(route.methods)} {route.path}"
        for route in routes
        if not _depends_on_principal(route)
    ]

    assert not offenders, "these routes do not require a principal: " + ", ".join(offenders)


def _depends_on_principal(route: APIRoute) -> bool:
    """Walk the dependency tree, since the principal may be reached indirectly."""
    stack = list(route.dependant.dependencies)
    while stack:
        dependency = stack.pop()
        if dependency.call is get_principal:
            return True
        stack.extend(dependency.dependencies)
    return False


async def test_missing_credential_is_401(anon_client: httpx.AsyncClient) -> None:
    response = await anon_client.post(
        "/v1/validate", json={"resource": {"resourceType": "Patient"}}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"
    assert "trace_id" in response.json()["error"]


async def test_non_bearer_credential_is_401(anon_client: httpx.AsyncClient) -> None:
    response = await anon_client.post(
        "/v1/validate",
        json={"resource": {"resourceType": "Patient"}},
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )

    assert response.status_code == 401


@pytest.mark.parametrize(
    "path",
    ["/livez", "/readyz", "/version", "/metrics", "/v1/error-codes", "/fhir/R4/metadata"],
)
async def test_public_paths_need_no_credential(
    anon_client: httpx.AsyncClient, path: str, database_up: None, all_dependencies_healthy: None
) -> None:
    response = await anon_client.get(path)

    assert response.status_code != 401


async def test_llm_key_over_plaintext_http_is_rejected(
    plaintext_client: httpx.AsyncClient,
) -> None:
    """AGENTS.md 7.1: a provider key must never travel unencrypted."""
    response = await plaintext_client.post(
        "/v1/validate",
        json={"resource": {"resourceType": "Patient"}},
        headers={"X-LLM-Api-Key": "sk-would-be-leaked-on-the-wire"},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["issue"][0]["details"]["coding"][0]["code"] == "insecure-transport"
    assert "sk-would-be-leaked-on-the-wire" not in response.text


async def test_stt_key_over_plaintext_http_is_rejected(
    plaintext_client: httpx.AsyncClient,
) -> None:
    """A dictation key is a credential too, so the transport guard must cover it."""
    response = await plaintext_client.post(
        "/v1/validate",
        json={"resource": {"resourceType": "Patient"}},
        headers={"X-STT-Api-Key": "gk-would-be-leaked-on-the-wire"},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["issue"][0]["details"]["coding"][0]["code"] == "insecure-transport"
    assert "gk-would-be-leaked-on-the-wire" not in response.text


async def test_forwarded_proto_https_is_honoured(
    plaintext_client: httpx.AsyncClient, all_dependencies_healthy: None
) -> None:
    """TLS terminating at an ingress is the normal deployment, not an exception."""
    response = await plaintext_client.post(
        "/v1/validate",
        json={"resource": {"resourceType": "Patient"}},
        headers={
            "X-LLM-Api-Key": "sk-test",
            "X-Forwarded-Proto": "https",
        },
    )

    assert response.status_code != 400


async def test_plaintext_without_a_key_is_allowed(
    plaintext_client: httpx.AsyncClient, all_dependencies_healthy: None
) -> None:
    """The guard is about credentials in transit, not about refusing HTTP outright."""
    response = await plaintext_client.post(
        "/v1/validate", json={"resource": {"resourceType": "Patient"}}
    )

    assert response.status_code == 200


async def test_oversized_body_is_rejected_before_it_is_read(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/v1/validate",
        content=b"x",
        headers={"Content-Type": "application/json", "Content-Length": str(64 * 1024 * 1024)},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload-too-large"


async def test_chat_style_endpoint_does_not_exist(client: httpx.AsyncClient) -> None:
    """AGENTS.md 3: prompt passthrough is refused as a matter of design."""
    for path in ("/v1/chat", "/chat", "/v1/completions", "/v1/prompt"):
        response = await client.post(path, json={"prompt": "hello"})
        assert response.status_code == 404
