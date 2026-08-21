"""The cascade orchestrator (AGENTS.md 10).

The invariant worth the most here is negative: **a layer that did not run must
never look like a layer that passed.** A report is a conformance claim, and the
easiest way to publish a false one is to omit a layer and let the reader assume.
So every layer appears in every report, skips carry a reason, and a skipped
blocking layer is enough on its own to deny the ``auto`` decision.
"""

from __future__ import annotations

from typing import Any

import pytest

from fhirbridge.config import Settings
from fhirbridge.domain.errors import TerminologyUnavailableError, ValidatorUnavailableError
from fhirbridge.validation.cascade import CRITICAL_DOMAINS, ValidationCascade, ValidationSpec
from fhirbridge.validation.models import (
    CASCADE_ORDER,
    LayerStatus,
    RoutingDecision,
    ValidationLayer,
)
from fhirbridge.version import CODE_VERSION, TYPED_MODEL_FHIR_VERSION
from tests.fakes import FakeTerminologyClient, FakeValidatorClient, issue
from tests.helpers import OBSERVATION, US_CORE_PATIENT


@pytest.fixture
def cascade(settings: Settings) -> ValidationCascade:
    return ValidationCascade(
        validator=FakeValidatorClient(),  # type: ignore[arg-type]  # structural double
        terminology=FakeTerminologyClient(),
        settings=settings,
        terminology_versions={"snomed": "INT-20260501", "loinc": "2.79"},
    )


def build(
    settings: Settings,
    *,
    validator: FakeValidatorClient | None = None,
    terminology: FakeTerminologyClient | None = None,
) -> ValidationCascade:
    return ValidationCascade(
        validator=validator or FakeValidatorClient(),  # type: ignore[arg-type]
        terminology=terminology or FakeTerminologyClient(),
        settings=settings,
        terminology_versions={"loinc": "2.79"},
    )


# --- Completeness of the report -------------------------------------------


async def test_every_layer_appears_in_every_report(cascade: ValidationCascade) -> None:
    report = await cascade.run(OBSERVATION)

    assert [result.layer for result in report.layers] == list(CASCADE_ORDER)


async def test_layer_numbers_match_the_cascade_table(cascade: ValidationCascade) -> None:
    report = await cascade.run(OBSERVATION)

    assert [result.layer_number for result in report.layers] == [1, 2, 3, 4, 5, 6, 7, 8]


async def test_a_conformant_resource_routes_to_auto(cascade: ValidationCascade) -> None:
    report = await cascade.run(OBSERVATION)

    assert report.status is RoutingDecision.AUTO
    assert report.conformant is True
    assert report.scores.conformance == 1.0
    assert report.resource_type == "Observation"
    assert report.resource_count == 1


async def test_the_unmeasured_scores_are_none_not_perfect(cascade: ValidationCascade) -> None:
    """``fidelity: null`` says "not measured"; ``fidelity: 1.0`` would be a lie."""
    report = await cascade.run(OBSERVATION)

    assert report.scores.fidelity is None
    assert report.scores.coverage is None
    assert report.scores.mean_confidence is None


async def test_layers_needing_a_source_document_are_not_applicable(
    cascade: ValidationCascade,
) -> None:
    report = await cascade.run(OBSERVATION)

    for layer in (ValidationLayer.FIDELITY, ValidationLayer.COVERAGE):
        result = report.layer(layer)
        assert result is not None
        assert result.status is LayerStatus.NOT_APPLICABLE
        assert result.blocking is False
        assert result.skipped_reason is not None
        assert "POST /v1/verify" in result.skipped_reason


# --- Version stamping (principle 2.8) -------------------------------------


async def test_the_report_carries_the_full_version_set(cascade: ValidationCascade) -> None:
    report = await cascade.run(OBSERVATION)

    versions = report.versions
    assert versions.code == CODE_VERSION
    assert versions.fhir == "4.0.1"
    assert versions.typed_models == TYPED_MODEL_FHIR_VERSION
    assert versions.validator == "6.9.8"
    assert versions.ig == ["hl7.fhir.us.core#9.0.0"]
    assert versions.terminology == {"snomed": "INT-20260501", "loinc": "2.79"}


async def test_caller_supplied_ig_packages_override_the_configured_default(
    cascade: ValidationCascade,
) -> None:
    report = await cascade.run(OBSERVATION, ValidationSpec(ig_packages=("hl7.fhir.uv.ips#2.0.0",)))

    assert report.versions.ig == ["hl7.fhir.uv.ips#2.0.0"]


async def test_a_validate_only_report_claims_no_model_and_no_prompt_set(
    cascade: ValidationCascade,
) -> None:
    report = await cascade.run(OBSERVATION)

    assert report.versions.model == {}
    assert report.versions.prompt_set is None
    assert report.nondeterminism_risk is False


# --- Unparseable input short-circuits, honestly ---------------------------


async def test_an_unparseable_payload_skips_every_downstream_layer_with_a_reason(
    cascade: ValidationCascade,
) -> None:
    report = await cascade.run({"not": "a resource"})

    assert report.status is RoutingDecision.REJECT
    for layer in (
        ValidationLayer.PROFILE,
        ValidationLayer.TERMINOLOGY,
        ValidationLayer.INVARIANTS,
        ValidationLayer.PLAUSIBILITY,
    ):
        result = report.layer(layer)
        assert result is not None
        assert result.status is LayerStatus.SKIPPED
        assert result.skipped_reason is not None
        assert "could not parse" in result.skipped_reason


async def test_no_dependency_is_called_for_an_unparseable_payload(settings: Settings) -> None:
    validator = FakeValidatorClient()
    terminology = FakeTerminologyClient()

    await build(settings, validator=validator, terminology=terminology).run("nonsense")

    assert validator.validate_calls == []
    assert terminology.calls == []


async def test_a_conformance_score_is_none_when_there_is_nothing_to_score(
    cascade: ValidationCascade,
) -> None:
    report = await cascade.run({"resourceType": "Bundle", "type": "collection"})

    assert report.resource_count == 0
    assert report.scores.conformance is None


# --- Opting a layer out is recorded as a skip, not a pass -----------------


async def test_an_opted_out_layer_is_skipped_and_denies_auto(cascade: ValidationCascade) -> None:
    spec = ValidationSpec(
        layers=frozenset(
            {
                ValidationLayer.STRUCTURAL,
                ValidationLayer.TERMINOLOGY,
                ValidationLayer.INVARIANTS,
                ValidationLayer.PLAUSIBILITY,
            }
        )
    )

    report = await cascade.run(OBSERVATION, spec)

    profile = report.layer(ValidationLayer.PROFILE)
    assert profile is not None
    assert profile.status is LayerStatus.SKIPPED
    assert profile.blocking is True
    assert profile.skipped_reason == "not requested by the caller"
    assert report.status is RoutingDecision.NEEDS_REVIEW
    assert report.conformant is True  # nothing failed...


async def test_the_routing_rationale_names_the_layer_that_did_not_run(
    cascade: ValidationCascade,
) -> None:
    spec = ValidationSpec(layers=frozenset({ValidationLayer.STRUCTURAL}))

    report = await cascade.run(OBSERVATION, spec)
    routing = report.layer(ValidationLayer.ROUTING)

    assert routing is not None
    message = routing.issues[0].message
    assert "did not run, so conformance is unproven" in message
    assert "profile" in message


async def test_terminology_is_skipped_when_l1_produced_no_typed_model(
    cascade: ValidationCascade,
) -> None:
    """An R4-only type has no R4B model, so the coded elements cannot be located."""
    report = await cascade.run({"resourceType": "MedicinalProduct", "id": "mp-1"})

    result = report.layer(ValidationLayer.TERMINOLOGY)
    assert result is not None
    assert result.status is LayerStatus.SKIPPED
    assert result.skipped_reason is not None
    assert "no typed model" in result.skipped_reason
    assert report.status is RoutingDecision.NEEDS_REVIEW


# --- Routing decisions ----------------------------------------------------


async def test_a_blocking_issue_routes_to_reject(settings: Settings) -> None:
    validator = FakeValidatorClient(issues=(issue(severity="error", message="broken"),))

    report = await build(settings, validator=validator).run(OBSERVATION)

    assert report.status is RoutingDecision.REJECT
    assert report.conformant is False
    assert report.blocking_issues


async def test_the_rejection_rationale_counts_issues_and_names_layers(
    settings: Settings,
) -> None:
    validator = FakeValidatorClient(issues=(issue(severity="error", message="broken"),))

    report = await build(settings, validator=validator).run(OBSERVATION)
    routing = report.layer(ValidationLayer.ROUTING)

    assert routing is not None
    assert routing.issues[0].message == "Rejected: 1 blocking issue(s) in profile."


async def test_a_warning_alone_routes_to_needs_review(settings: Settings) -> None:
    validator = FakeValidatorClient(issues=(issue(severity="warning", message="odd"),))

    report = await build(settings, validator=validator).run(OBSERVATION)

    assert report.status is RoutingDecision.NEEDS_REVIEW
    assert report.conformant is True


async def test_routing_is_never_itself_blocking(cascade: ValidationCascade) -> None:
    report = await cascade.run(OBSERVATION)
    routing = report.layer(ValidationLayer.ROUTING)

    assert routing is not None
    assert routing.blocking is False
    assert routing.status is LayerStatus.PASSED
    assert any("added from M3" in note for note in routing.notes)


# --- Critical domains are reported separately -----------------------------


async def test_an_issue_in_a_critical_domain_is_flagged_separately(
    settings: Settings,
) -> None:
    validator = FakeValidatorClient(
        issues=(
            issue(
                severity="warning",
                message="dose is ambiguous",
                expression="MedicationStatement.dosage[0].doseQuantity",
            ),
        )
    )

    report = await build(settings, validator=validator).run(OBSERVATION)

    assert [flag.domain for flag in report.critical_flags] == ["medication_dose"]
    assert report.status is RoutingDecision.NEEDS_REVIEW


async def test_critical_flags_are_deduplicated_per_domain_and_location(
    settings: Settings,
) -> None:
    duplicate = issue(
        severity="warning", message="same place", expression="AllergyIntolerance.code"
    )
    validator = FakeValidatorClient(issues=(duplicate, duplicate))

    report = await build(settings, validator=validator).run(OBSERVATION)

    assert len(report.critical_flags) == 1
    assert report.critical_flags[0].domain == "allergy"


async def test_informational_issues_never_raise_a_critical_flag(settings: Settings) -> None:
    validator = FakeValidatorClient(
        issues=(issue(severity="information", expression="AllergyIntolerance.code"),)
    )

    report = await build(settings, validator=validator).run(OBSERVATION)

    assert report.critical_flags == []
    assert report.status is RoutingDecision.AUTO


def test_the_critical_domains_match_the_specification() -> None:
    """``negation`` and ``experiencer`` need facts, so they arrive with M3."""
    assert set(CRITICAL_DOMAINS) == {"allergy", "medication_dose", "laterality"}


# --- Conformance scoring over bundles -------------------------------------


class TestConformanceScore:
    def bundle(self, count: int) -> dict[str, Any]:
        return {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [{"resource": OBSERVATION} for _ in range(count)],
        }

    async def test_a_clean_bundle_scores_one(self, settings: Settings) -> None:
        report = await build(settings).run(self.bundle(4))

        assert report.resource_count == 4
        assert report.scores.conformance == 1.0

    async def test_one_bad_entry_costs_only_that_entry(self, settings: Settings) -> None:
        validator = FakeValidatorClient(
            issues=(
                issue(
                    severity="error",
                    message="broken",
                    expression="Bundle.entry[2].resource.status",
                ),
            )
        )

        report = await build(settings, validator=validator).run(self.bundle(4))

        assert report.scores.conformance == 0.75

    async def test_two_issues_in_one_entry_cost_that_entry_once(self, settings: Settings) -> None:
        validator = FakeValidatorClient(
            issues=(
                issue(severity="error", expression="Bundle.entry[1].resource.status"),
                issue(severity="error", expression="Bundle.entry[1].resource.code"),
            )
        )

        report = await build(settings, validator=validator).run(self.bundle(4))

        assert report.scores.conformance == 0.75

    async def test_a_bundle_level_failure_scores_zero(self, settings: Settings) -> None:
        """An unattributable error could apply to any entry, so none can be claimed."""
        validator = FakeValidatorClient(issues=(issue(severity="error", expression="Bundle.type"),))

        report = await build(settings, validator=validator).run(self.bundle(4))

        assert report.scores.conformance == 0.0

    async def test_l5_findings_do_not_affect_the_conformance_score(
        self, settings: Settings
    ) -> None:
        """Conformance means "conforms to the specification". An implausible value
        is a data-quality finding, reported separately."""
        implausible = OBSERVATION | {
            "valueQuantity": {
                "value": 1900,
                "system": "http://unitsofmeasure.org",
                "code": "/min",
            }
        }

        report = await build(settings).run(implausible)

        assert report.scores.conformance == 1.0
        assert report.status is RoutingDecision.REJECT
        assert report.conformant is False


# --- Fail closed ----------------------------------------------------------


async def test_a_validator_outage_fails_the_whole_request(settings: Settings) -> None:
    cascade = build(settings, validator=FakeValidatorClient(unavailable=True))

    with pytest.raises(ValidatorUnavailableError):
        await cascade.run(OBSERVATION)


async def test_a_terminology_outage_fails_the_whole_request(settings: Settings) -> None:
    cascade = build(settings, terminology=FakeTerminologyClient(unavailable=True))

    with pytest.raises(TerminologyUnavailableError):
        await cascade.run(OBSERVATION)


# --- Spec plumbing --------------------------------------------------------


async def test_requested_profiles_are_echoed_in_the_report(cascade: ValidationCascade) -> None:
    report = await cascade.run(
        {"resourceType": "Patient", "gender": "unknown"},
        ValidationSpec(profiles=(US_CORE_PATIENT,)),
    )

    assert report.profiles == [US_CORE_PATIENT]


async def test_the_conversion_id_is_carried_through(cascade: ValidationCascade) -> None:
    report = await cascade.run(OBSERVATION, ValidationSpec(conversion_id="cnv_01JTEST"))

    assert report.conversion_id == "cnv_01JTEST"


async def test_severity_overrides_reach_the_plausibility_layer(settings: Settings) -> None:
    implausible = OBSERVATION | {
        "valueQuantity": {"value": 1900, "system": "http://unitsofmeasure.org", "code": "/min"}
    }

    report = await build(settings).run(
        implausible,
        ValidationSpec(severity_overrides={"fb-plaus-heart-rate": "information"}),
    )

    assert report.status is RoutingDecision.AUTO
    assert report.conformant is True


async def test_the_terminology_check_budget_is_plumbed_through(settings: Settings) -> None:
    terminology = FakeTerminologyClient()

    await build(settings, terminology=terminology).run(
        OBSERVATION, ValidationSpec(max_terminology_checks=1)
    )

    assert len(terminology.calls) == 1


def test_a_spec_with_no_layer_filter_wants_every_layer() -> None:
    spec = ValidationSpec()

    assert all(spec.wants(layer) for layer in CASCADE_ORDER)
