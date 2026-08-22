"""Parsing the BYOK ``X-LLM-*`` headers into a validated invocation."""

from __future__ import annotations

import pytest

from fhirbridge.domain.errors import InvalidRequestError, LlmCredentialsRequiredError
from fhirbridge.llm.invocation import LlmInvocation


def _parse(**overrides: str | None) -> LlmInvocation:
    kwargs: dict[str, str | None] = {
        "provider": "openrouter",
        "model": "openai/gpt-4o-mini",
        "api_key": "sk-test",
        "base_url": None,
        "extra_headers": None,
        "phi_ack": None,
    }
    kwargs.update(overrides)
    return LlmInvocation.from_headers(**kwargs)  # type: ignore[arg-type]


class TestFromHeaders:
    def test_a_missing_key_is_a_credentials_error_not_a_generic_one(self) -> None:
        """BYOK means the absence of a key is the documented failure, not a 422."""
        with pytest.raises(LlmCredentialsRequiredError):
            _parse(api_key=None)

    def test_a_blank_key_is_also_refused(self) -> None:
        with pytest.raises(LlmCredentialsRequiredError):
            _parse(api_key="   ")

    def test_a_missing_model_is_an_invalid_request(self) -> None:
        with pytest.raises(InvalidRequestError):
            _parse(model=None)

    def test_the_key_is_wrapped_so_it_cannot_leak(self) -> None:
        invocation = _parse(api_key="sk-secret")

        assert "sk-secret" not in repr(invocation)
        assert invocation.api_key.get_secret_value() == "sk-secret"

    def test_the_provider_defaults_to_openrouter_and_is_lowercased(self) -> None:
        assert _parse(provider=None).provider == "openrouter"
        assert _parse(provider="OpenRouter").provider == "openrouter"

    def test_the_phi_acknowledgement_parses_common_truthy_tokens(self) -> None:
        assert _parse(phi_ack="true").phi_egress_acknowledged is True
        assert _parse(phi_ack="1").phi_egress_acknowledged is True
        assert _parse(phi_ack="no").phi_egress_acknowledged is False
        assert _parse(phi_ack=None).phi_egress_acknowledged is False


class TestExtraHeaders:
    def test_a_json_object_of_strings_is_accepted(self) -> None:
        invocation = _parse(extra_headers='{"HTTP-Referer": "https://x", "X-Title": "fhirbridge"}')

        assert invocation.extra_headers == {
            "HTTP-Referer": "https://x",
            "X-Title": "fhirbridge",
        }

    def test_invalid_json_is_rejected(self) -> None:
        with pytest.raises(InvalidRequestError):
            _parse(extra_headers="{not json}")

    def test_a_json_array_is_rejected(self) -> None:
        with pytest.raises(InvalidRequestError):
            _parse(extra_headers='["a", "b"]')

    def test_non_string_values_are_rejected(self) -> None:
        with pytest.raises(InvalidRequestError):
            _parse(extra_headers='{"n": 1}')


class TestEgressHost:
    def test_the_openrouter_default_resolves_to_its_host(self) -> None:
        assert _parse().egress_host == "openrouter.ai"

    def test_an_explicit_base_url_wins(self) -> None:
        assert (
            _parse(base_url="https://my-proxy.internal:8443/v1").egress_host == "my-proxy.internal"
        )

    def test_an_unknown_provider_without_a_base_url_resolves_to_nothing(self) -> None:
        """An unresolvable host must not accidentally read as allowed downstream."""
        assert _parse(provider="mystery", base_url=None).egress_host == ""
