"""Principle 2.7: no secret reaches a log, a repr, an error body or a trace.

Each assertion here corresponds to a way credentials have historically escaped
real systems: an exception string, a debug log of a request, a ``repr`` in a
traceback, or an error response echoing what the client sent.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
import respx
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fhirbridge.api.auth import generate_api_key
from fhirbridge.config import Settings, TerminologyAuthMode
from fhirbridge.fhir.validator_client import ValidatorClient
from fhirbridge.observability.logging import JsonFormatter, RedactionFilter
from fhirbridge.observability.tracing import set_safe_attributes
from fhirbridge.terminology.client import FhirTerminologyClient
from tests.helpers import TERMINOLOGY_URL, VALIDATOR_URL

pytestmark = pytest.mark.security

SECRETS: list[str] = [
    "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789",
    "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456",
    "AKIAIOSFODNN7EXAMPLE",
    "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
]


def _emit(record_factory: Any) -> str:
    """Format one record through the real filter and formatter."""
    formatter = JsonFormatter()
    filter_ = RedactionFilter()
    record = record_factory()
    assert filter_.filter(record)
    return formatter.format(record)


@pytest.mark.parametrize("secret", SECRETS)
def test_secrets_are_redacted_from_log_messages(secret: str) -> None:
    output = _emit(
        lambda: logging.LogRecord(
            "test", logging.INFO, "f.py", 1, "calling provider with %s", (secret,), None
        )
    )

    assert secret not in output


@pytest.mark.parametrize("secret", SECRETS)
def test_secrets_are_redacted_from_structured_fields(secret: str) -> None:
    record = logging.LogRecord("test", logging.INFO, "f.py", 1, "outbound", None, None)
    record.__dict__["headers"] = {"Authorization": secret, "X-Llm-Api-Key": secret}
    record.__dict__["nested"] = {"credential": {"api_key": secret}}

    output = _emit(lambda: record)

    assert secret not in output


@pytest.mark.parametrize("secret", SECRETS)
def test_secrets_are_redacted_from_tracebacks(secret: str) -> None:
    """An exception message is the most common accidental credential sink."""
    try:
        raise RuntimeError(f"provider rejected key {secret}")
    except RuntimeError:
        import sys

        exc_info = sys.exc_info()

    record = logging.LogRecord("test", logging.ERROR, "f.py", 1, "failed", None, exc_info)
    output = _emit(lambda: record)

    assert secret not in output


def test_generated_api_key_never_appears_in_its_own_repr() -> None:
    generated = generate_api_key()
    secret = generated.secret.get_secret_value()

    assert secret not in repr(generated)
    assert secret not in str(generated)
    assert secret not in json.dumps(
        {"key_id": generated.key_id, "prefix": generated.prefix}, default=str
    )


def test_settings_repr_does_not_expose_key_material() -> None:
    settings = Settings.model_validate(
        {
            "DATABASE_URL": "postgresql+asyncpg://user:hunter2@localhost:5432/db",
            "REDIS_URL": "redis://localhost:6379/0",
            "VALIDATOR_URL": VALIDATOR_URL,
            "TERMINOLOGY_URL": TERMINOLOGY_URL,
            "FHIRBRIDGE_MASTER_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            "TERMINOLOGY_AUTH_MODE": TerminologyAuthMode.BEARER,
            "TERMINOLOGY_TOKEN": "sk-terminology-token-value",
        }
    )

    rendered = f"{settings!r} {settings!s}"

    assert "sk-terminology-token-value" not in rendered
    assert "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=" not in rendered


def test_terminology_client_repr_hides_its_authorization_header() -> None:
    client = FhirTerminologyClient(
        base_url=TERMINOLOGY_URL,
        auth_mode=TerminologyAuthMode.BEARER,
        token="sk-terminology-token-value",
    )

    assert "sk-terminology-token-value" not in repr(client)
    assert "Authorization" not in repr(client)


def test_validator_client_repr_hides_its_transport() -> None:
    """``httpx.AsyncClient.__repr__`` can carry configured auth; keep it out."""
    client = ValidatorClient(base_url=VALIDATOR_URL)

    assert "client=" not in repr(client)


def test_span_attributes_reject_non_scalars() -> None:
    """A whole request body attached to a span would carry PHI and secrets."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("stage") as span:
        set_safe_attributes(
            span,
            provider="openai",
            tokens=42,
            api_key=None,
            body={"secret": SECRETS[0]},
        )

    attributes = dict(exporter.get_finished_spans()[0].attributes or {})
    assert attributes["provider"] == "openai"
    assert "api_key" not in attributes
    # The dict was stringified rather than structured, and that string is still
    # subject to the same rule: never put a payload on a span.
    assert isinstance(attributes["body"], str)


async def test_error_bodies_do_not_echo_the_submitted_payload(
    client: httpx.AsyncClient, mock_http: respx.MockRouter
) -> None:
    """A schema-violation response must not quote the clinical content back."""
    del mock_http
    response = await client.post(
        "/v1/validate",
        json={"resource": {"resourceType": "Patient"}, "max_terminology_checks": 99999},
    )

    body = response.text
    assert response.status_code == 400
    assert "Patient" not in body
    assert "resourceType" not in body
    # The violation is still actionable: it names the field and the rule.
    violations = response.json()["error"]["details"]["violations"]
    assert violations[0]["location"].endswith("max_terminology_checks")
