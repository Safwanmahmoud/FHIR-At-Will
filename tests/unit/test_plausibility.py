"""L5 plausibility rules (AGENTS.md 10).

Two things are asserted throughout: that impossible values are caught, and that
merely *abnormal* values are not. The second is the one that keeps this layer on
the right side of principle 2.9 — a rule pack that flags a hypertensive blood
pressure has become clinical decision support.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from fhirbridge.validation.models import IssueSeverity, LayerStatus
from fhirbridge.validation.plausibility import (
    PlausibilityPack,
    RuleKind,
    load_plausibility_rules,
    skipped,
    validate_plausibility,
)

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def observation(code: str, value: float, unit: str, **extra: Any) -> dict[str, Any]:
    return {
        "resourceType": "Observation",
        "status": "preliminary",
        "code": {"coding": [{"system": "http://loinc.org", "code": code}]},
        "valueQuantity": {"value": value, "system": "http://unitsofmeasure.org", "code": unit},
        **extra,
    }


def run(payload: dict[str, Any], **kwargs: Any) -> Any:
    return validate_plausibility(
        payload, resource_type=str(payload["resourceType"]), now=NOW, **kwargs
    )


def rule_ids(result: Any) -> set[str]:
    return {issue.rule_id for issue in result.issues}


# --- The impossible / abnormal line ----------------------------------------


@pytest.mark.parametrize(
    ("code", "value", "unit", "rule"),
    [
        ("8867-4", 1900, "/min", "fb-plaus-heart-rate"),
        ("8480-6", 900, "mm[Hg]", "fb-plaus-systolic-bp"),
        ("8462-4", 0, "mm[Hg]", "fb-plaus-diastolic-bp"),
        ("8310-5", 98.6, "Cel", "fb-plaus-body-temperature"),
        ("9279-1", 900, "/min", "fb-plaus-respiratory-rate"),
        ("2708-6", 140, "%", "fb-plaus-oxygen-saturation"),
        ("29463-7", 1800, "kg", "fb-plaus-body-weight"),
        ("8302-2", 1.75, "cm", "fb-plaus-body-height"),
    ],
)
def test_physiologically_impossible_vitals_are_flagged(
    code: str, value: float, unit: str, rule: str
) -> None:
    result = run(observation(code, value, unit))

    assert rule in rule_ids(result)
    assert result.status is LayerStatus.FAILED


@pytest.mark.parametrize(
    ("code", "value", "unit"),
    [
        ("8867-4", 185, "/min"),  # tachycardia
        ("8480-6", 210, "mm[Hg]"),  # hypertensive crisis
        ("8310-5", 41.5, "Cel"),  # high fever
        ("2708-6", 78, "%"),  # severe hypoxia
        ("29463-7", 250, "kg"),  # severe obesity
    ],
)
def test_abnormal_but_possible_values_are_not_flagged(code: str, value: float, unit: str) -> None:
    """Abnormal is a clinician's call, not this layer's. See principle 2.9."""
    result = run(observation(code, value, unit))

    assert result.issues == []
    assert result.status is LayerStatus.PASSED


def test_the_notes_state_the_scope_of_the_layer() -> None:
    result = run(observation("8867-4", 72, "/min"))

    assert any("does not interpret" in note for note in result.notes)


# --- Units -----------------------------------------------------------------


def test_a_range_check_with_the_wrong_unit_warns_instead_of_guessing() -> None:
    """98.6 degF is normal; 98.6 Cel is not. With the wrong unit we cannot tell."""
    result = run(observation("8310-5", 98.6, "[degF]"))

    issues = [i for i in result.issues if i.rule_id == "fb-plaus-body-temperature"]
    assert len(issues) == 1
    assert issues[0].severity is IssueSeverity.WARNING
    assert "could not be applied" in issues[0].message
    assert issues[0].expression.endswith(".code")


def test_a_quantity_with_no_unit_at_all_is_still_range_checked() -> None:
    payload = {
        "resourceType": "Observation",
        "status": "preliminary",
        "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]},
        "valueQuantity": {"value": 1900},
    }

    assert "fb-plaus-heart-rate" in rule_ids(run(payload))


def test_a_negative_magnitude_is_flagged() -> None:
    result = run(observation("8867-4", -72, "/min"))

    assert "fb-plaus-no-negative-magnitudes" in rule_ids(result)


def test_component_quantities_are_checked_with_their_own_codes() -> None:
    """A blood pressure panel carries its values in components, not at the root."""
    payload = {
        "resourceType": "Observation",
        "status": "preliminary",
        "code": {"coding": [{"system": "http://loinc.org", "code": "85354-9"}]},
        "component": [
            {
                "code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
                "valueQuantity": {"value": 900, "code": "mm[Hg]"},
            },
            {
                "code": {"coding": [{"system": "http://loinc.org", "code": "8462-4"}]},
                "valueQuantity": {"value": 80, "code": "mm[Hg]"},
            },
        ],
    }

    result = run(payload)

    assert "fb-plaus-systolic-bp" in rule_ids(result)
    expressions = {issue.expression for issue in result.issues}
    assert "Observation.component[0].valueQuantity.value" in expressions


# --- Dates -----------------------------------------------------------------


def test_a_future_clinical_date_is_flagged() -> None:
    result = run(observation("8867-4", 72, "/min", effectiveDateTime="2030-01-01T00:00:00Z"))

    assert "fb-plaus-no-future-clinical-dates" in rule_ids(result)


def test_a_date_inside_the_tolerance_window_is_not_flagged() -> None:
    """A clock skew of a few hours is not a data-quality finding."""
    result = run(observation("8867-4", 72, "/min", effectiveDateTime="2026-06-01T23:00:00Z"))

    assert result.issues == []


def test_a_condition_cannot_resolve_before_it_began() -> None:
    payload = {
        "resourceType": "Condition",
        "subject": {"reference": "Patient/p1"},
        "onsetDateTime": "2025-05-01",
        "abatementDateTime": "2024-01-01",
    }

    result = run(payload)

    assert "fb-plaus-abatement-after-onset" in rule_ids(result)
    assert result.status is LayerStatus.FAILED


def test_onset_after_the_recorded_date_only_warns() -> None:
    payload = {
        "resourceType": "Condition",
        "subject": {"reference": "Patient/p1"},
        "onsetDateTime": "2025-05-01",
        "recordedDate": "2024-01-01",
    }

    issues = [i for i in run(payload).issues if i.rule_id == "fb-plaus-onset-before-recorded"]
    assert issues[0].severity is IssueSeverity.WARNING


def test_partial_dates_are_anchored_at_the_start_of_the_period() -> None:
    """``2024`` means "some time in 2024", so it cannot be *proven* to be after
    ``2024-06-01``. Anchoring at the start avoids manufacturing a violation."""
    payload = {
        "resourceType": "Condition",
        "subject": {"reference": "Patient/p1"},
        "onsetDateTime": "2024",
        "abatementDateTime": "2024-06-01",
    }

    assert run(payload).issues == []


class TestBirthDateOrdering:
    def bundle(self, birth_date: str, effective: str) -> dict[str, Any]:
        return {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [
                {
                    "fullUrl": "urn:uuid:p1",
                    "resource": {"resourceType": "Patient", "id": "p1", "birthDate": birth_date},
                },
                {
                    "resource": {
                        "resourceType": "Observation",
                        "status": "preliminary",
                        "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]},
                        "subject": {"reference": "Patient/p1"},
                        "effectiveDateTime": effective,
                    }
                },
            ],
        }

    def test_an_event_before_birth_is_flagged(self) -> None:
        result = run(self.bundle("1990-01-01", "1985-01-01"))

        assert "fb-plaus-birth-before-clinical-events" in rule_ids(result)
        assert result.status is LayerStatus.FAILED

    def test_an_event_after_birth_is_not_flagged(self) -> None:
        assert run(self.bundle("1990-01-01", "2020-01-01")).issues == []

    def test_the_check_is_skipped_when_the_subject_cannot_be_resolved(self) -> None:
        """Without a Patient in the payload there is no birth date to compare to,
        and inventing one would be exactly the inference principle 2.9 forbids."""
        payload = observation("8867-4", 72, "/min", subject={"reference": "Patient/elsewhere"}) | {
            "effectiveDateTime": "1900-01-01"
        }

        assert "fb-plaus-birth-before-clinical-events" not in rule_ids(run(payload))


# --- Dose magnitude (a critical domain) ------------------------------------


def test_an_implausible_dose_magnitude_is_flagged() -> None:
    payload = {
        "resourceType": "MedicationStatement",
        "status": "unknown",
        "subject": {"reference": "Patient/p1"},
        "dosage": [
            {
                "text": "5 g PO daily",
                "doseAndRate": [{"doseQuantity": {"value": 5000, "code": "g"}}],
            }
        ],
    }

    result = run(payload)

    assert "fb-plaus-dose-magnitude" in rule_ids(result)
    assert result.status is LayerStatus.FAILED
    expressions = {issue.expression for issue in result.issues}
    assert "MedicationStatement.dosage[0].doseAndRate[0].doseQuantity.value" in expressions


def test_an_ordinary_dose_is_not_flagged() -> None:
    payload = {
        "resourceType": "MedicationStatement",
        "status": "unknown",
        "subject": {"reference": "Patient/p1"},
        "dosage": [{"doseAndRate": [{"doseQuantity": {"value": 500, "code": "mg"}}]}],
    }

    assert run(payload).issues == []


def test_a_dose_in_an_unlisted_unit_is_not_guessed_at() -> None:
    payload = {
        "resourceType": "MedicationStatement",
        "status": "unknown",
        "subject": {"reference": "Patient/p1"},
        "dosage": [{"doseAndRate": [{"doseQuantity": {"value": 99999, "code": "puff"}}]}],
    }

    assert run(payload).issues == []


# --- Severity overrides and disabled rules ---------------------------------


def test_severity_can_be_overridden_per_deployment() -> None:
    result = run(
        observation("8867-4", 1900, "/min"),
        severity_overrides={"fb-plaus-heart-rate": "warning"},
    )

    issue = next(i for i in result.issues if i.rule_id == "fb-plaus-heart-rate")
    assert issue.severity is IssueSeverity.WARNING
    assert result.status is LayerStatus.PASSED


def test_the_sex_anatomy_rule_is_disabled_by_default() -> None:
    """Administrative gender is not a reliable proxy for anatomy.

    See OPEN_QUESTIONS.md#Q3: a false conflict shown to a reviewer is a harm.
    """
    pack = load_plausibility_rules()

    rule = next(r for r in pack.rules if r.kind is RuleKind.SEX_ANATOMY_CONFLICT)
    assert rule.enabled is False
    assert rule.id not in {r.id for r in pack.for_resource("Condition")}


def test_disabled_rules_are_named_in_the_notes() -> None:
    result = run(observation("8867-4", 72, "/min"))

    assert any("fb-plaus-sex-anatomy-conflict" in note for note in result.notes)


def test_every_rule_kind_in_the_pack_is_implemented() -> None:
    """A typo in ``kind`` must fail loudly at load time, not silently no-op."""
    pack = load_plausibility_rules()

    assert {rule.kind for rule in pack.rules} <= set(RuleKind)
    assert len(pack.rules) > 10


class TestSexAnatomyWhenExplicitlyEnabled:
    """The rule ships disabled, but a deployment may turn it on (OPEN_QUESTIONS Q3).

    These tests exist so that the enabled path is not dead code: an operator who
    flips it on should get a reviewable *information* finding, not an error, and
    not a silent no-op.
    """

    def pack(self) -> PlausibilityPack:
        source = load_plausibility_rules()
        rule = next(r for r in source.rules if r.kind is RuleKind.SEX_ANATOMY_CONFLICT)
        enabled = replace(rule, enabled=True)
        return PlausibilityPack(
            rules=(enabled,),
            future_date_tolerance_days=source.future_date_tolerance_days,
        )

    def bundle(self, gender: str, code: str) -> dict[str, Any]:
        return {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [
                {"resource": {"resourceType": "Patient", "id": "p1", "gender": gender}},
                {
                    "resource": {
                        "resourceType": "Condition",
                        "subject": {"reference": "Patient/p1"},
                        "code": {"coding": [{"system": "http://snomed.info/sct", "code": code}]},
                    }
                },
            ],
        }

    def test_a_conflict_is_surfaced_for_review_and_never_blocks(self) -> None:
        result = run(self.bundle("male", "77386006"), pack=self.pack())

        issue = next(i for i in result.issues if i.rule_id == "fb-plaus-sex-anatomy-conflict")
        assert issue.severity is IssueSeverity.INFORMATION
        assert "never as a correction" in issue.message
        assert result.status is LayerStatus.PASSED

    def test_a_matching_gender_is_not_a_conflict(self) -> None:
        assert run(self.bundle("female", "77386006"), pack=self.pack()).issues == []

    def test_an_unlisted_code_is_not_a_conflict(self) -> None:
        assert run(self.bundle("male", "44054006"), pack=self.pack()).issues == []

    def test_a_resource_with_no_resolvable_patient_is_left_alone(self) -> None:
        payload = {
            "resourceType": "Condition",
            "code": {"coding": [{"system": "http://snomed.info/sct", "code": "77386006"}]},
        }

        assert run(payload, pack=self.pack()).issues == []


class TestMalformedInput:
    """L5 runs on payloads L1 has already rejected as well as ones it accepted.

    The cascade reports every layer it can, so this layer must not raise on
    structurally wrong input — a ``TypeError`` here would turn a 422 validation
    finding into a 500.
    """

    @pytest.mark.parametrize(
        "value",
        [True, False, None, "not-a-number", [], {}, float("nan")],
    )
    def test_a_non_numeric_quantity_value_does_not_raise(self, value: Any) -> None:
        payload = {
            "resourceType": "Observation",
            "status": "preliminary",
            "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]},
            "valueQuantity": {"value": value, "code": "/min"},
        }

        assert run(payload).status is LayerStatus.PASSED

    def test_a_numeric_string_value_is_still_checked(self) -> None:
        """Some senders quote numbers. The value is unambiguous, so check it."""
        payload = {
            "resourceType": "Observation",
            "status": "preliminary",
            "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]},
            "valueQuantity": {"value": "1900", "code": "/min"},
        }

        assert "fb-plaus-heart-rate" in rule_ids(run(payload))

    @pytest.mark.parametrize(
        "value", ["", "   ", "not-a-date", "2026-13-45", "2026-02-30", 20260101, None, []]
    )
    def test_an_unparseable_date_is_ignored_rather_than_guessed(self, value: Any) -> None:
        payload = {
            "resourceType": "Condition",
            "subject": {"reference": "Patient/p1"},
            "onsetDateTime": value,
            "abatementDateTime": "2020-01-01",
        }

        assert run(payload).issues == []

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("code", "a bare string"),
            ("code", {"coding": "not-a-list"}),
            ("code", {"coding": [None, 7, {"code": "8867-4"}, {"system": "http://loinc.org"}]}),
            ("component", "not-a-list"),
            ("component", [None, "x", {"valueQuantity": "not-a-dict"}]),
            ("valueQuantity", "not-a-dict"),
            ("subject", "not-a-dict"),
            ("subject", {"reference": 7}),
        ],
    )
    def test_a_wrongly_typed_element_does_not_raise(self, key: str, value: Any) -> None:
        payload = {"resourceType": "Observation", "status": "preliminary", key: value}

        assert run(payload).issues == []

    def test_a_bundle_with_junk_entries_checks_the_entries_it_can_read(self) -> None:
        payload = {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [
                None,
                "a string",
                {},
                {"resource": "not-a-dict"},
                {"resource": {"id": "no-resource-type"}},
                {"resource": {"resourceType": 7}},
                {
                    "resource": {
                        "resourceType": "Observation",
                        "status": "preliminary",
                        "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]},
                        "valueQuantity": {"value": 1900, "code": "/min"},
                    }
                },
            ],
        }

        result = run(payload)

        assert "fb-plaus-heart-rate" in rule_ids(result)
        expressions = {issue.expression for issue in result.issues}
        assert "Bundle.entry[6].resource.valueQuantity.value" in expressions

    def test_a_bundle_with_a_non_list_entry_is_treated_as_empty(self) -> None:
        assert run({"resourceType": "Bundle", "type": "collection", "entry": {}}).issues == []

    def test_a_dosage_of_the_wrong_shape_does_not_raise(self) -> None:
        payload = {
            "resourceType": "MedicationStatement",
            "status": "unknown",
            "dosage": [
                None,
                "text only",
                {"doseAndRate": "not-a-list"},
                {"doseAndRate": [None, "x", {"doseQuantity": "not-a-dict"}]},
                {"doseAndRate": [{"doseQuantity": {"value": 5000}}]},  # no unit
                {"doseAndRate": [{"doseQuantity": {"code": "mg"}}]},  # no value
            ],
        }

        assert run(payload).issues == []


def test_a_skipped_layer_reports_why() -> None:
    result = skipped("L1 could not parse the resource, so L5 has nothing to check.")

    assert result.status is LayerStatus.SKIPPED
    assert result.blocking is True
    assert result.skipped_reason is not None
    assert result.issues == []


def test_multiple_patients_with_unresolvable_references_are_not_guessed_between() -> None:
    """Picking one of two candidate subjects would be inference, not validation."""
    payload = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [
            {"resource": {"resourceType": "Patient", "id": "p1", "birthDate": "1990-01-01"}},
            {"resource": {"resourceType": "Patient", "id": "p2", "birthDate": "2010-01-01"}},
            {
                "resource": {
                    "resourceType": "Observation",
                    "status": "preliminary",
                    "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]},
                    "subject": {"reference": "Patient/unknown"},
                    "effectiveDateTime": "1985-01-01",
                }
            },
        ],
    }

    assert "fb-plaus-birth-before-clinical-events" not in rule_ids(run(payload))


def test_a_patient_is_resolved_by_full_url() -> None:
    """``urn:uuid:`` references are how assembled bundles point at each other."""
    payload = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [
            {
                "fullUrl": "urn:uuid:1234",
                "resource": {"resourceType": "Patient", "id": "p1", "birthDate": "1990-01-01"},
            },
            {"resource": {"resourceType": "Patient", "id": "p2", "birthDate": "1990-01-01"}},
            {
                "resource": {
                    "resourceType": "Observation",
                    "status": "preliminary",
                    "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]},
                    "subject": {"reference": "urn:uuid:1234"},
                    "effectiveDateTime": "1985-01-01",
                }
            },
        ],
    }

    assert "fb-plaus-birth-before-clinical-events" in rule_ids(run(payload))


def test_a_patient_resource_is_not_compared_against_its_own_birth_date() -> None:
    payload = {"resourceType": "Patient", "id": "p1", "birthDate": "1990-01-01"}

    assert run(payload).issues == []


def test_a_patient_with_no_birth_date_yields_no_ordering_findings() -> None:
    payload = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [
            {"resource": {"resourceType": "Patient", "id": "p1"}},
            {
                "resource": {
                    "resourceType": "Observation",
                    "status": "preliminary",
                    "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]},
                    "subject": {"reference": "Patient/p1"},
                    "effectiveDateTime": "1900-01-01",
                }
            },
        ],
    }

    assert "fb-plaus-birth-before-clinical-events" not in rule_ids(run(payload))


def test_the_rule_pack_is_cached() -> None:
    """Re-reading and re-parsing YAML on every request would be a silly cost."""
    assert load_plausibility_rules() is load_plausibility_rules()
