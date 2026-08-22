"""The gateway's policy gates, exception mapping and output parsing.

These are the safety-relevant parts of the harness and they are all pure: no
network, no litellm. The provider call itself is exercised end-to-end through the
router tests with a scripted gateway.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from fhirbridge.config import QualificationTier, Settings
from fhirbridge.domain.errors import (
    EgressBlockedError,
    LlmAuthFailedError,
    LlmContentFilteredError,
    LlmContextExceededError,
    LlmQuotaExhaustedError,
    LlmRateLimitedError,
    LlmSchemaViolationError,
    ModelNotQualifiedError,
    PhiEgressNotAcknowledgedError,
)
from fhirbridge.llm.gateway import (
    LlmGateway,
    _extract_json_object,
    _map_llm_exception,
)
from fhirbridge.llm.invocation import LlmInvocation


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "FHIRBRIDGE_ENV": "development",
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/test",
        "REDIS_URL": "redis://localhost:6379/0",
        "VALIDATOR_URL": "http://validator.test",
        "TERMINOLOGY_URL": "http://terminology.test",
        "LLM_EGRESS_ALLOWLIST": "openrouter.ai",
    }
    base.update(overrides)
    return Settings.model_validate(base)


def _inv(
    *,
    model: str = "openai/gpt-4o-mini",
    provider: str = "openrouter",
    base_url: str | None = None,
    ack: bool = True,
) -> LlmInvocation:
    return LlmInvocation(
        provider=provider,
        model=model,
        api_key=SecretStr("sk-test"),
        base_url=base_url,
        phi_egress_acknowledged=ack,
    )


class TestAuthorize:
    def test_a_permitted_qualified_acknowledged_call_returns_its_tier(self) -> None:
        gateway = LlmGateway(settings=_settings())

        tier = gateway.authorize(_inv(), sending_phi=True)

        assert tier is QualificationTier.SILVER

    def test_a_disallowed_provider_is_blocked(self) -> None:
        gateway = LlmGateway(settings=_settings(LLM_ALLOWED_PROVIDERS="anthropic"))

        with pytest.raises(EgressBlockedError):
            gateway.authorize(_inv(provider="openrouter"), sending_phi=True)

    def test_a_host_off_the_allowlist_is_blocked(self) -> None:
        gateway = LlmGateway(settings=_settings(LLM_EGRESS_ALLOWLIST=""))

        with pytest.raises(EgressBlockedError):
            gateway.authorize(_inv(), sending_phi=True)

    def test_local_only_mode_blocks_external_hosts(self) -> None:
        gateway = LlmGateway(settings=_settings(LOCAL_ONLY_MODE=True))

        with pytest.raises(EgressBlockedError):
            gateway.authorize(_inv(), sending_phi=True)

    def test_local_only_mode_permits_loopback(self) -> None:
        gateway = LlmGateway(settings=_settings(LOCAL_ONLY_MODE=True))

        tier = gateway.authorize(
            _inv(provider="ollama", base_url="http://localhost:11434/v1", model="openai/gpt-4o"),
            sending_phi=True,
        )

        assert tier is QualificationTier.GOLD

    def test_unacknowledged_phi_egress_is_refused(self) -> None:
        gateway = LlmGateway(settings=_settings())

        with pytest.raises(PhiEgressNotAcknowledgedError):
            gateway.authorize(_inv(ack=False), sending_phi=True)

    def test_a_probe_does_not_require_the_phi_acknowledgement(self) -> None:
        """A probe sends no clinical content, so the ack gate does not apply."""
        gateway = LlmGateway(settings=_settings())

        tier = gateway.authorize(_inv(ack=False), sending_phi=False)

        assert tier is QualificationTier.SILVER

    def test_a_model_below_the_minimum_tier_is_refused(self) -> None:
        gateway = LlmGateway(settings=_settings(MIN_QUALIFICATION_TIER="gold"))

        with pytest.raises(ModelNotQualifiedError):
            gateway.authorize(_inv(model="openai/gpt-4o-mini"), sending_phi=True)

    def test_an_unknown_model_is_unqualified_and_refused_by_default(self) -> None:
        gateway = LlmGateway(settings=_settings())

        with pytest.raises(ModelNotQualifiedError):
            gateway.authorize(_inv(model="some/unknown-model"), sending_phi=True)

    def test_lowering_the_tier_lets_an_unknown_model_through(self) -> None:
        gateway = LlmGateway(settings=_settings(MIN_QUALIFICATION_TIER="unqualified"))

        tier = gateway.authorize(_inv(model="some/unknown-model"), sending_phi=True)

        assert tier is QualificationTier.UNQUALIFIED


class TestExceptionMapping:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("AuthenticationError", LlmAuthFailedError),
            ("PermissionDeniedError", LlmAuthFailedError),
            ("ContextWindowExceededError", LlmContextExceededError),
            ("ContentPolicyViolationError", LlmContentFilteredError),
            ("Timeout", LlmRateLimitedError),
            ("APIConnectionError", LlmRateLimitedError),
            ("ServiceUnavailableError", LlmRateLimitedError),
        ],
    )
    def test_known_provider_errors_map_to_the_catalogue(
        self, name: str, expected: type[Exception]
    ) -> None:
        exc = type(name, (Exception,), {})("boom")

        mapped = _map_llm_exception(exc)

        assert isinstance(mapped, expected)

    def test_a_plain_rate_limit_is_rate_limited(self) -> None:
        exc = type("RateLimitError", (Exception,), {})("slow down")

        mapped = _map_llm_exception(exc)

        assert isinstance(mapped, LlmRateLimitedError)
        assert mapped.retry_after_s is not None

    def test_a_quota_rate_limit_is_reported_as_quota_exhausted(self) -> None:
        exc = type("RateLimitError", (Exception,), {})("insufficient_quota: add billing")

        assert isinstance(_map_llm_exception(exc), LlmQuotaExhaustedError)

    def test_an_unrecognized_error_is_left_to_surface_generically(self) -> None:
        assert _map_llm_exception(ValueError("who knows")) is None


class TestJsonExtraction:
    def test_a_bare_object_parses(self) -> None:
        assert _extract_json_object('{"resourceType": "Bundle"}') == {"resourceType": "Bundle"}

    def test_a_fenced_object_parses(self) -> None:
        fenced = '```json\n{"resourceType": "Bundle"}\n```'

        assert _extract_json_object(fenced) == {"resourceType": "Bundle"}

    def test_invalid_json_is_a_schema_violation(self) -> None:
        with pytest.raises(LlmSchemaViolationError):
            _extract_json_object("not json at all")

    def test_a_non_object_is_a_schema_violation(self) -> None:
        with pytest.raises(LlmSchemaViolationError):
            _extract_json_object("[1, 2, 3]")

    def test_empty_output_is_a_schema_violation(self) -> None:
        with pytest.raises(LlmSchemaViolationError):
            _extract_json_object("   ")
