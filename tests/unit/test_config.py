"""Configuration validation (AGENTS.md 6).

The behaviour under test is the *fatal startup report*: a misconfigured process
must refuse to start and must name every problem at once, because an operator
who fixes one variable per restart cycle will give up before the fifth.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from fhirbridge.config import (
    ConfigurationError,
    CredentialStorage,
    Environment,
    IgPackage,
    LlmMode,
    QualificationTier,
    Settings,
    TerminologyAuthMode,
    load_settings,
)

MINIMUM = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/fhirbridge",
    "REDIS_URL": "redis://localhost:6379/0",
    "VALIDATOR_URL": "http://validator:8080",
    "TERMINOLOGY_URL": "http://hapi-tx:8080/fhir",
}

_KEY_32B = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
"""32 zero bytes, base64. Not a secret; it is a fixed test vector."""


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every variable Settings reads, so the host environment cannot leak in."""
    for field in Settings.model_fields.values():
        alias = field.validation_alias
        names = (
            [alias]
            if isinstance(alias, str)
            else [c for c in getattr(alias, "choices", []) if isinstance(c, str)]
        )
        for name in names:
            monkeypatch.delenv(name, raising=False)


def build(**overrides: object) -> Settings:
    """Load settings from explicit values only, ignoring any local ``.env``."""
    return load_settings(_env_file=None, **(MINIMUM | overrides))


def from_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> Settings:
    """Load settings the way a deployment does: through the process environment.

    Distinct from :func:`build` on purpose. ``build`` passes values as init
    kwargs, which skips pydantic-settings' env source entirely — and that source
    is where comma-separated values are decoded. A setting can therefore work in
    every ``build`` test and still be impossible to set in a container.
    """
    for key, value in (MINIMUM | overrides).items():
        monkeypatch.setenv(key, value)
    return load_settings(_env_file=None)


class TestSettingsThatArriveThroughTheEnvironment:
    """Guards the env source specifically.

    pydantic-settings JSON-decodes complex fields (lists, dicts) before any
    validator runs, so a list field reachable from the environment needs
    ``NoDecode`` or it raises SettingsError on the comma-separated form that
    .env.example documents.
    """

    def test_ig_packages_parse_from_a_comma_separated_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = from_env(
            monkeypatch, DEFAULT_IG_PACKAGES="hl7.fhir.us.core#9.0.0,hl7.fhir.uv.ips#2.0.0"
        )

        assert settings.ig_coordinates == ("hl7.fhir.us.core#9.0.0", "hl7.fhir.uv.ips#2.0.0")

    def test_a_single_ig_package_parses_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = from_env(monkeypatch, DEFAULT_IG_PACKAGES="hl7.fhir.us.core#9.0.0")

        assert settings.default_ig_packages == [IgPackage("hl7.fhir.us.core", "9.0.0")]

    def test_the_egress_allowlist_parses_from_a_comma_separated_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = from_env(monkeypatch, LLM_EGRESS_ALLOWLIST="api.openai.com,localhost")

        assert settings.llm_egress_allowlist == ["api.openai.com", "localhost"]

    def test_the_provider_allowlist_parses_from_a_comma_separated_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = from_env(monkeypatch, LLM_ALLOWED_PROVIDERS="ollama,openai")

        assert settings.llm_allowed_providers == ["ollama", "openai"]

    def test_a_malformed_ig_package_from_the_environment_is_still_fatal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(ConfigurationError) as caught:
            from_env(monkeypatch, DEFAULT_IG_PACKAGES="hl7.fhir.us.core")

        assert "name#version" in str(caught.value)


# --- The fatal report ------------------------------------------------------


def test_missing_required_variables_are_all_reported_at_once() -> None:
    with pytest.raises(ConfigurationError) as caught:
        load_settings(_env_file=None)

    report = str(caught.value)
    for name in MINIMUM:
        assert f"{name}: required but not set" in report, report
    assert "docs/deployment.md" in report


def test_the_report_names_environment_variables_not_python_fields() -> None:
    with pytest.raises(ConfigurationError) as caught:
        load_settings(_env_file=None)

    report = str(caught.value)
    assert "FHIRBRIDGE_MASTER_KEY" not in report  # optional, so not reported
    assert "database_url" not in report
    assert "DATABASE_URL" in report


def test_invalid_key_material_is_rejected_without_echoing_the_key() -> None:
    with pytest.raises(ConfigurationError) as caught:
        build(FHIRBRIDGE_MASTER_KEY="hunter2-not-base64-and-far-too-short")

    report = str(caught.value)
    assert "hunter2" not in report
    assert "base64" in report


def test_a_key_of_the_wrong_length_is_rejected() -> None:
    with pytest.raises(ConfigurationError) as caught:
        build(FHIRBRIDGE_MASTER_KEY="AAAA")  # 3 bytes

    assert "32 bytes" in str(caught.value)


# --- Individual field rules ------------------------------------------------


def test_a_synchronous_database_url_is_rejected() -> None:
    with pytest.raises(ConfigurationError) as caught:
        build(DATABASE_URL="postgresql://u:p@localhost:5432/fhirbridge")

    assert "async driver" in str(caught.value)


def test_a_non_redis_queue_url_is_rejected() -> None:
    with pytest.raises(ConfigurationError) as caught:
        build(REDIS_URL="amqp://localhost")

    assert "REDIS_URL" in str(caught.value)


def test_an_unsupported_fhir_version_is_rejected() -> None:
    with pytest.raises(ConfigurationError) as caught:
        build(DEFAULT_FHIR_VERSION="5.0.0")

    assert "not supported by this build" in str(caught.value)


def test_an_unknown_log_level_is_rejected() -> None:
    with pytest.raises(ConfigurationError):
        build(LOG_LEVEL="chatty")


def test_log_level_is_normalized_to_upper_case() -> None:
    assert build(LOG_LEVEL="debug").log_level == "DEBUG"


def test_ig_packages_parse_from_csv() -> None:
    settings = build(DEFAULT_IG_PACKAGES="hl7.fhir.us.core#9.0.0, hl7.fhir.uv.ips#2.0.0")

    assert settings.default_ig_packages == [
        IgPackage("hl7.fhir.us.core", "9.0.0"),
        IgPackage("hl7.fhir.uv.ips", "2.0.0"),
    ]
    assert settings.ig_coordinates == ("hl7.fhir.us.core#9.0.0", "hl7.fhir.uv.ips#2.0.0")


def test_an_ig_package_without_a_version_is_rejected() -> None:
    with pytest.raises(ConfigurationError) as caught:
        build(DEFAULT_IG_PACKAGES="hl7.fhir.us.core")

    assert "name#version" in str(caught.value)


def test_compact_durations_parse() -> None:
    settings = build(RETENTION_DEFAULT="7d", TERMINOLOGY_CACHE_TTL="30m")

    assert settings.retention_default == timedelta(days=7)
    assert settings.terminology_cache_ttl == timedelta(minutes=30)


def test_an_unparseable_duration_is_rejected() -> None:
    with pytest.raises(ConfigurationError) as caught:
        build(RETENTION_DEFAULT="a fortnight")

    assert "invalid duration" in str(caught.value)


def test_defaults_match_the_documented_configuration() -> None:
    settings = build()

    assert settings.llm_mode is LlmMode.BYOK
    assert settings.credential_storage is CredentialStorage.DISABLED
    assert settings.min_qualification_tier is QualificationTier.BRONZE
    assert settings.require_phi_egress_ack is True
    assert settings.allow_insecure_transport is False
    assert settings.debug_capture_llm_io is False
    assert settings.max_cost_usd_per_conversion == Decimal("1.00")
    assert settings.retention_default == timedelta(days=30)
    assert settings.default_fhir_version == "4.0.1"
    assert settings.ig_coordinates == ("hl7.fhir.us.core#9.0.0",)


# --- Cross-field rules -----------------------------------------------------


def test_credential_storage_requires_a_master_key() -> None:
    with pytest.raises(ConfigurationError) as caught:
        build(CREDENTIAL_STORAGE="encrypted_db")

    assert "FHIRBRIDGE_MASTER_KEY is required" in str(caught.value)


def test_credential_storage_is_accepted_once_a_master_key_exists() -> None:
    settings = build(CREDENTIAL_STORAGE="encrypted_db", FHIRBRIDGE_MASTER_KEY=_KEY_32B)

    assert settings.credential_storage is CredentialStorage.ENCRYPTED_DB
    assert settings.master_key_bytes == b"\x00" * 32


def test_basic_terminology_auth_requires_a_username_and_password() -> None:
    with pytest.raises(ConfigurationError) as caught:
        build(TERMINOLOGY_AUTH_MODE="basic")

    assert "TERMINOLOGY_USERNAME" in str(caught.value)


def test_bearer_terminology_auth_requires_a_token() -> None:
    with pytest.raises(ConfigurationError) as caught:
        build(TERMINOLOGY_AUTH_MODE="bearer")

    assert "TERMINOLOGY_TOKEN" in str(caught.value)


def test_bearer_terminology_auth_is_accepted_with_a_token() -> None:
    settings = build(TERMINOLOGY_AUTH_MODE="bearer", TERMINOLOGY_TOKEN="t0ken-value")

    assert settings.terminology_auth_mode is TerminologyAuthMode.BEARER


# --- Production guards -----------------------------------------------------


PRODUCTION = {
    "FHIRBRIDGE_ENV": "production",
    "FHIRBRIDGE_EPHEMERAL_KEY": _KEY_32B,
    "VALIDATOR_URL": "https://validator.internal",
    "TERMINOLOGY_URL": "https://tx.internal/fhir",
}


def test_production_rejects_plaintext_transport_for_api_keys() -> None:
    with pytest.raises(ConfigurationError) as caught:
        build(**PRODUCTION, ALLOW_INSECURE_TRANSPORT=True)

    assert "ALLOW_INSECURE_TRANSPORT=true is forbidden in production" in str(caught.value)


def test_production_rejects_llm_io_capture() -> None:
    with pytest.raises(ConfigurationError) as caught:
        build(**PRODUCTION, DEBUG_CAPTURE_LLM_IO=True)

    assert "DEBUG_CAPTURE_LLM_IO=true is forbidden in production" in str(caught.value)


def test_production_requires_an_ephemeral_key_for_byok_jobs() -> None:
    without_key = {k: v for k, v in PRODUCTION.items() if k != "FHIRBRIDGE_EPHEMERAL_KEY"}
    with pytest.raises(ConfigurationError) as caught:
        build(**without_key)

    assert "FHIRBRIDGE_EPHEMERAL_KEY is required in production" in str(caught.value)


def test_production_rejects_the_public_test_terminology_server() -> None:
    with pytest.raises(ConfigurationError) as caught:
        build(**PRODUCTION | {"TERMINOLOGY_URL": "https://tx.fhir.org/r4"})

    assert "not provisioned for production use" in str(caught.value)


def test_production_rejects_a_non_loopback_plaintext_validator() -> None:
    with pytest.raises(ConfigurationError) as caught:
        build(**PRODUCTION | {"VALIDATOR_URL": "http://validator.internal"})

    assert "VALIDATOR_URL must use https" in str(caught.value)


def test_production_allows_a_loopback_plaintext_validator() -> None:
    settings = build(**PRODUCTION | {"VALIDATOR_URL": "http://localhost:8081"})

    assert settings.environment is Environment.PRODUCTION


def test_every_production_problem_is_reported_in_one_pass() -> None:
    with pytest.raises(ConfigurationError) as caught:
        build(
            FHIRBRIDGE_ENV="production",
            ALLOW_INSECURE_TRANSPORT=True,
            DEBUG_CAPTURE_LLM_IO=True,
            TERMINOLOGY_URL="https://tx.fhir.org/r4",
        )

    report = str(caught.value)
    assert "ALLOW_INSECURE_TRANSPORT" in report
    assert "DEBUG_CAPTURE_LLM_IO" in report
    assert "FHIRBRIDGE_EPHEMERAL_KEY" in report
    assert "tx.fhir.org" in report


# --- Derived behaviour -----------------------------------------------------


def test_provider_allowlist_defaults_to_wildcard() -> None:
    assert build().provider_allowed("anthropic") is True


def test_provider_allowlist_is_case_insensitive_and_exclusive() -> None:
    settings = build(LLM_ALLOWED_PROVIDERS="ollama, OpenAI")

    assert settings.provider_allowed("OLLAMA") is True
    assert settings.provider_allowed("openai") is True
    assert settings.provider_allowed("anthropic") is False


def test_base_urls_are_normalized_without_a_trailing_slash() -> None:
    settings = build(VALIDATOR_URL="http://validator:8080/", TERMINOLOGY_URL="http://tx:8080/fhir/")

    assert settings.validator_base_url == "http://validator:8080"
    assert settings.terminology_base_url == "http://tx:8080/fhir"


def test_missing_optional_keys_decode_to_none() -> None:
    settings = build()

    assert settings.master_key_bytes is None
    assert settings.ephemeral_key_bytes is None


class TestStartupWarnings:
    """The loud warnings required for unsafe-but-permitted settings."""

    def test_plaintext_transport_warns(self) -> None:
        warnings = build(ALLOW_INSECURE_TRANSPORT=True).emit_startup_warnings()

        assert any("ALLOW_INSECURE_TRANSPORT=true" in w for w in warnings)

    def test_llm_io_capture_warns_about_phi(self) -> None:
        warnings = build(DEBUG_CAPTURE_LLM_IO=True).emit_startup_warnings()

        assert any("including any PHI they contain" in w for w in warnings)

    def test_external_egress_warns_about_the_regulatory_position(self) -> None:
        warnings = build(LOCAL_ONLY_MODE=False).emit_startup_warnings()

        assert any("BAA/DPA" in w for w in warnings)

    def test_local_only_mode_does_not_warn_about_egress(self) -> None:
        warnings = build(LOCAL_ONLY_MODE=True).emit_startup_warnings()

        assert not any("BAA/DPA" in w for w in warnings)

    def test_an_empty_allowlist_without_local_only_mode_warns_it_blocks_everything(self) -> None:
        warnings = build(LLM_EGRESS_ALLOWLIST="", LOCAL_ONLY_MODE=False).emit_startup_warnings()

        assert any("every" in w and "base_url will be rejected" in w for w in warnings)

    def test_waiving_the_phi_acknowledgement_warns(self) -> None:
        warnings = build(REQUIRE_PHI_EGRESS_ACK=False).emit_startup_warnings()

        assert any("REQUIRE_PHI_EGRESS_ACK=false" in w for w in warnings)

    def test_a_conservative_local_deployment_warns_about_nothing(self) -> None:
        warnings = build(
            LOCAL_ONLY_MODE=True, LLM_EGRESS_ALLOWLIST="127.0.0.1"
        ).emit_startup_warnings()

        assert warnings == []


# --- Enum semantics --------------------------------------------------------


def test_qualification_tiers_are_ordered() -> None:
    assert QualificationTier.GOLD.satisfies(QualificationTier.BRONZE)
    assert QualificationTier.BRONZE.satisfies(QualificationTier.BRONZE)
    assert not QualificationTier.BRONZE.satisfies(QualificationTier.SILVER)
    assert not QualificationTier.UNQUALIFIED.satisfies(QualificationTier.BRONZE)


def test_only_the_modes_that_permit_a_server_key_say_so() -> None:
    assert not LlmMode.BYOK.allows_server_default
    assert LlmMode.SERVER_DEFAULT.allows_server_default
    assert LlmMode.BYOK_OR_DEFAULT.allows_server_default


def test_ig_package_is_hashable_and_renders_its_coordinate() -> None:
    package = IgPackage.parse("  hl7.fhir.us.core#9.0.0 ")

    assert str(package) == "hl7.fhir.us.core#9.0.0"
    assert repr(package) == "IgPackage('hl7.fhir.us.core#9.0.0')"
    assert {package, IgPackage("hl7.fhir.us.core", "9.0.0")} == {package}
    assert package != "hl7.fhir.us.core#9.0.0"
