"""Secret redaction patterns (AGENTS.md 7.8, principle 2.7).

This is defence in depth: the primary control is that keys live in ``SecretStr``
and never reach a log call. This module exists because ``httpx``, ``litellm`` and
provider SDKs do not share that discipline, and because tracebacks stringify
locals.

The patterns are deliberately broad. A false positive costs a garbled log line;
a false negative leaks a customer's API key. The tests are written to that
asymmetry, so a few of them assert over-redaction as acceptable.
"""

from __future__ import annotations

import pytest

from fhirbridge.observability.redaction import (
    REDACTED,
    contains_secret_like,
    is_sensitive_key,
    redact_object,
    redact_text,
)

# Fabricated key-shaped strings. None of these are real credentials.
KEY_SHAPES = {
    "openai": "sk-abcdefghijklmnopqrstuvwxyz0123456789ABCD",
    "openai_project": "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789",
    "anthropic": "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789",
    "openrouter": "sk-or-v1-abcdefghijklmnopqrstuvwxyz0123",
    "google": "AIzaSyA1234567890abcdefghijklmnopqrstuvw",
    "aws": "AKIAIOSFODNN7EXAMPLE",
    "aws_session": "ASIAIOSFODNN7EXAMPLE",
    "google_oauth": "ya29.a0ARrdaM-abcdefghijklmnopqrstuvwxyz012345",
    "groq": "gsk_abcdefghijklmnopqrstuvwxyz0123456789",
    "xai": "xai-abcdefghijklmnopqrstuvwxyz0123456789",
    "replicate": "r8_abcdefghijklmnopqrstuvwxyz",
    "huggingface": "hf_abcdefghijklmnopqrstuvwxyz",
    "github": "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "jwt": (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        ".dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    ),
}


@pytest.mark.parametrize(("provider", "key"), sorted(KEY_SHAPES.items()))
def test_every_known_key_shape_is_redacted(provider: str, key: str) -> None:
    redacted = redact_text(f"calling provider with key {key} now")

    assert key not in redacted, provider
    assert REDACTED in redacted


@pytest.mark.parametrize(("provider", "key"), sorted(KEY_SHAPES.items()))
def test_the_security_suite_detector_recognizes_each_shape(provider: str, key: str) -> None:
    """``contains_secret_like`` is what the §16.6 assertions are built on, so it
    must agree with the redactor about what a secret looks like."""
    assert contains_secret_like(f"prefix {key} suffix"), provider


def test_a_bearer_token_in_a_header_is_redacted() -> None:
    redacted = redact_text("Authorization: Bearer abcdef1234567890xyz")

    assert "abcdef1234567890xyz" not in redacted


def test_credentials_embedded_in_a_url_are_redacted() -> None:
    redacted = redact_text("postgresql+asyncpg://fhirbridge:s3cr3tp4ss@db:5432/fhirbridge")

    assert "s3cr3tp4ss" not in redacted
    assert "db:5432/fhirbridge" in redacted


@pytest.mark.parametrize(
    "text",
    [
        'api_key="mysecretvalue"',
        "api-key: mysecretvalue",
        "apiKey=mysecretvalue",
        "{'password': 'mysecretvalue'}",
        "client_secret => mysecretvalue",
        "X-LLM-Api-Key: mysecretvalue",
        "anthropic-api-key: mysecretvalue",
        "FHIRBRIDGE_MASTER_KEY=mysecretvalue",
        "aws_secret_access_key=mysecretvalue",
    ],
)
def test_a_keyed_assignment_is_redacted_whatever_the_syntax(text: str) -> None:
    """Providers, config files and kwargs all spell this differently, and a
    traceback may show any of them."""
    assert "mysecretvalue" not in redact_text(text)


def test_the_key_name_survives_so_the_log_line_stays_useful() -> None:
    redacted = redact_text('api_key="mysecretvalue"')

    assert "api_key" in redacted
    assert REDACTED in redacted


@pytest.mark.parametrize(
    "text",
    [
        "conversion cnv_01ARZ3NDEKTSV4RRFFQ69G5FAV completed",
        "validated 8867-4 against http://loinc.org",
        "POST /v1/validate 200 in 42ms",
        "hl7.fhir.us.core#9.0.0",
        "Bundle.entry[4].resource.category",
    ],
)
def test_ordinary_log_lines_pass_through_unchanged(text: str) -> None:
    assert redact_text(text) == text


# --- Structured redaction --------------------------------------------------


def test_a_sensitive_key_has_its_whole_value_dropped() -> None:
    """Not pattern-matched — dropped. A key we cannot recognize by shape is
    exactly the case where the field name is the only signal we have."""
    result = redact_object({"api_key": "an-unrecognizable-key-format", "model": "qwen3:32b"})

    assert result == {"api_key": REDACTED, "model": "qwen3:32b"}


def test_nested_structures_are_redacted_throughout() -> None:
    payload = {
        "llm": {
            "stages": [
                {"stage": "extract", "api_key": "secret-one"},
                {"stage": "repair", "headers": {"Authorization": "Bearer secret-two"}},
            ]
        }
    }

    result = redact_object(payload)

    assert "secret-one" not in str(result)
    assert "secret-two" not in str(result)
    assert "extract" in str(result)


def test_extra_headers_are_dropped_wholesale() -> None:
    """``X-LLM-Extra-Headers`` is caller-controlled JSON, so its contents cannot
    be reasoned about. Dropping it is the only safe option."""
    result = redact_object({"extra_headers": {"X-Custom-Auth": "whatever-shape-this-is"}})

    assert result == {"extra_headers": REDACTED}


def test_scalars_pass_through_untouched() -> None:
    assert redact_object(42) == 42
    assert redact_object(1.5) == 1.5
    assert redact_object(True) is True
    assert redact_object(None) is None


def test_collections_are_normalized_to_lists() -> None:
    assert redact_object(("a", "b")) == ["a", "b"]
    assert redact_object(frozenset({"a"})) == ["a"]


def test_an_arbitrary_object_is_reduced_to_a_redacted_repr() -> None:
    class Holder:
        def __repr__(self) -> str:
            return "Holder(key='sk-abcdefghijklmnopqrstuvwxyz0123')"

    result = redact_object(Holder())

    assert isinstance(result, str)
    assert "sk-abcdef" not in result


def test_a_self_referential_structure_cannot_hang_a_log_call() -> None:
    payload: dict[str, object] = {}
    payload["self"] = payload

    assert "[TRUNCATED]" in str(redact_object(payload))


def test_non_string_keys_are_stringified() -> None:
    assert redact_object({1: "a"}) == {"1": "a"}


# --- Key-name recognition --------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "api_key",
        "API_KEY",
        "  Authorization  ",
        "x-llm-api-key",
        "client_secret",
        "password",
        "fhirbridge_master_key",
        "aws_secret_access_key",
    ],
)
def test_sensitive_key_names_are_recognized_case_and_space_insensitively(name: str) -> None:
    assert is_sensitive_key(name)


@pytest.mark.parametrize(
    "name", ["model", "provider", "base_url", "tenant_id", "conversion_id", "key_fingerprint"]
)
def test_harmless_key_names_are_not_flagged(name: str) -> None:
    """``key_fingerprint`` and ``last4`` are what we deliberately *do* store and
    report, so flagging them would hide the audit trail."""
    assert not is_sensitive_key(name)


def test_the_detector_accepts_extra_literals_for_a_known_test_secret() -> None:
    """The security suite injects a known key and asserts it never reappears,
    even in a shape none of the patterns would match."""
    assert contains_secret_like("value is zzz-unusual", extra=["zzz-unusual"])
    assert not contains_secret_like("value is ordinary", extra=["zzz-unusual"])
    assert not contains_secret_like("nothing here", extra=[""])
