"""Deterministic Bundle assembly.

Two themes run through these tests. The first is that grouping is driven by the
``instance`` key rather than by array order, because order-based pairing is what
turns "blood pressure" plus "74/min" into a confidently wrong clinical value. The
second is that assembly refuses rather than approximates: a value that does not fit
its declared datatype is dropped and reported, and no code, unit system, or date is
ever synthesized to make a resource look complete.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from fhirbridge.fhir.assemble import (
    AssemblyAction,
    assemble_bundle,
    resolve_datatype,
)
from fhirbridge.fhir.resource_types import typed_model_for
from fhirbridge.fhir.tags import AI_DERIVED, MACHINE_INFERRED, PROVENANCE_TAG_SYSTEM

SEED = "cnv_fixed_for_tests"


def entity(resource_type: str, instance: str, keyword: str, value: str) -> dict[str, str]:
    return {
        "resourceType": resource_type,
        "instance": instance,
        "keyword": keyword,
        "value": value,
    }


PATIENT = entity("Patient", "patient-1", "gender", "male")

# Two vital signs, interleaved exactly as a model tends to emit them. Only the
# instance key keeps the values attached to the right codes.
TWO_VITALS = [
    entity("Observation", "obs-bp", "code", "Blood pressure"),
    entity("Observation", "obs-bp", "valueString", "128/82 mmHg"),
    entity("Observation", "obs-hr", "code", "heart rate"),
    entity("Observation", "obs-hr", "valueQuantity", "74/min"),
]


def resources_of(bundle: dict[str, Any], resource_type: str) -> list[dict[str, Any]]:
    return [
        entry["resource"]
        for entry in bundle["entry"]
        if entry["resource"]["resourceType"] == resource_type
    ]


def tags_of(resource: dict[str, Any]) -> set[str]:
    return {
        coding["code"]
        for coding in resource["meta"]["tag"]
        if coding["system"] == PROVENANCE_TAG_SYSTEM
    }


class TestGrouping:
    def test_entities_sharing_an_instance_become_one_resource(self) -> None:
        assembled = assemble_bundle([PATIENT, *TWO_VITALS], seed=SEED)

        observations = resources_of(assembled.bundle, "Observation")
        assert len(observations) == 2

    def test_interleaved_measurements_keep_their_own_values(self) -> None:
        """The case flat triples cannot express: which value belongs to which code."""
        assembled = assemble_bundle([PATIENT, *TWO_VITALS], seed=SEED)

        by_code = {
            observation["code"]["text"]: observation
            for observation in resources_of(assembled.bundle, "Observation")
        }
        assert by_code["heart rate"]["valueQuantity"] == {"value": 74, "unit": "/min"}
        assert by_code["Blood pressure"]["valueString"] == "128/82 mmHg"

    def test_entry_order_does_not_depend_on_entity_order(self) -> None:
        """A model that lists facts differently must still produce the same Bundle."""
        forward = assemble_bundle([PATIENT, *TWO_VITALS], seed=SEED)
        reversed_input = assemble_bundle([*reversed(TWO_VITALS), PATIENT], seed=SEED)

        assert forward.bundle == reversed_input.bundle

    def test_repeated_values_for_a_list_element_are_appended(self) -> None:
        assembled = assemble_bundle(
            [
                PATIENT,
                entity("Encounter", "enc-1", "reasonCode", "routine follow-up"),
                entity("Encounter", "enc-1", "reasonCode", "medication review"),
            ],
            seed=SEED,
        )

        encounter = resources_of(assembled.bundle, "Encounter")[0]
        assert [concept["text"] for concept in encounter["reasonCode"]] == [
            "routine follow-up",
            "medication review",
        ]

    def test_a_second_value_for_a_scalar_element_is_a_reported_conflict(self) -> None:
        assembled = assemble_bundle(
            [
                PATIENT,
                entity("Observation", "obs-1", "code", "heart rate"),
                entity("Observation", "obs-1", "code", "pulse"),
            ],
            seed=SEED,
        )

        observation = resources_of(assembled.bundle, "Observation")[0]
        assert observation["code"] == {"text": "heart rate"}
        assert any(note.action is AssemblyAction.CONFLICT for note in assembled.notes)

    def test_no_entities_produce_an_empty_collection_bundle(self) -> None:
        assembled = assemble_bundle([], seed=SEED)

        assert assembled.bundle == {"resourceType": "Bundle", "type": "collection", "entry": []}
        assert assembled.notes == ()


class TestDatatypeResolution:
    @pytest.mark.parametrize(
        ("resource_type", "element", "expected"),
        [
            ("Patient", "name", ("HumanName", True)),
            ("Patient", "birthDate", ("Date", False)),
            ("Patient", "gender", ("Code", False)),
            ("Patient", "multipleBirthBoolean", ("bool", False)),
            ("Observation", "valueQuantity", ("Quantity", False)),
            ("Observation", "code", ("CodeableConcept", False)),
            ("Observation", "subject", ("Reference", False)),
            ("Observation", "issued", ("Instant", False)),
            ("Encounter", "class", ("Coding", False)),
            ("Encounter", "period", ("Period", False)),
            ("Encounter", "reasonCode", ("CodeableConcept", True)),
            ("MedicationRequest", "dosageInstruction", ("Dosage", True)),
        ],
    )
    def test_it_reads_the_declared_datatype_off_the_typed_model(
        self, resource_type: str, element: str, expected: tuple[str, bool]
    ) -> None:
        assert resolve_datatype(resource_type, element) == expected

    def test_an_unknown_element_is_not_treated_as_a_free_form_string(self) -> None:
        assert resolve_datatype("Patient", "notAnElement") == ("unknown", False)
        assert resolve_datatype("NotAResource", "name") == ("unknown", False)


class TestCoercion:
    def test_a_coded_concept_gets_text_and_no_invented_coding(self) -> None:
        """Principle 3: a model must not invent or silently accept a clinical code."""
        assembled = assemble_bundle(
            [PATIENT, entity("Condition", "cond-1", "code", "type 2 diabetes mellitus")],
            seed=SEED,
        )

        condition = resources_of(assembled.bundle, "Condition")[0]
        assert condition["code"] == {"text": "type 2 diabetes mellitus"}

    def test_a_quantity_carries_no_unit_system_or_code(self) -> None:
        """Asserting the unit string is valid UCUM is L3's job, not the assembler's."""
        assembled = assemble_bundle(
            [
                PATIENT,
                entity("Observation", "obs-1", "code", "heart rate"),
                entity("Observation", "obs-1", "valueQuantity", "74/min"),
            ],
            seed=SEED,
        )

        quantity = resources_of(assembled.bundle, "Observation")[0]["valueQuantity"]
        assert quantity == {"value": 74, "unit": "/min"}

    def test_a_decimal_quantity_keeps_its_precision(self) -> None:
        assembled = assemble_bundle(
            [
                PATIENT,
                entity("Observation", "obs-1", "code", "temperature"),
                entity("Observation", "obs-1", "valueQuantity", "37.2 Cel"),
            ],
            seed=SEED,
        )

        quantity = resources_of(assembled.bundle, "Observation")[0]["valueQuantity"]
        assert quantity == {"value": 37.2, "unit": "Cel"}

    def test_a_human_name_is_split_but_keeps_its_source_text(self) -> None:
        assembled = assemble_bundle(
            [entity("Patient", "patient-1", "name", "Alexandra Q Mosciski")], seed=SEED
        )

        name = resources_of(assembled.bundle, "Patient")[0]["name"][0]
        assert name == {
            "text": "Alexandra Q Mosciski",
            "family": "Mosciski",
            "given": ["Alexandra", "Q"],
        }

    def test_a_single_token_name_is_kept_as_text_only(self) -> None:
        assembled = assemble_bundle([entity("Patient", "patient-1", "name", "Prince")], seed=SEED)

        assert resources_of(assembled.bundle, "Patient")[0]["name"][0] == {"text": "Prince"}

    def test_a_period_becomes_a_start_bounded_period(self) -> None:
        assembled = assemble_bundle(
            [PATIENT, entity("Encounter", "enc-1", "period", "2024-01-15")], seed=SEED
        )

        assert resources_of(assembled.bundle, "Encounter")[0]["period"] == {"start": "2024-01-15"}


class TestRefusals:
    def test_a_narrative_age_does_not_become_a_birth_date(self) -> None:
        """The failure that motivates the whole design: a model would invent a year."""
        assembled = assemble_bundle(
            [entity("Patient", "patient-1", "birthDate", "62-year-old")], seed=SEED
        )

        patient = resources_of(assembled.bundle, "Patient")[0]
        assert "birthDate" not in patient
        assert [note.action for note in assembled.notes] == [AssemblyAction.DROPPED]

    def test_a_compound_value_is_refused_rather_than_half_parsed(self) -> None:
        """A blood pressure must not silently become a Quantity of 128."""
        assembled = assemble_bundle(
            [
                PATIENT,
                entity("Observation", "obs-1", "code", "Blood pressure"),
                entity("Observation", "obs-1", "valueQuantity", "128/82 mmHg"),
            ],
            seed=SEED,
        )

        observation = resources_of(assembled.bundle, "Observation")[0]
        assert "valueQuantity" not in observation
        assert any(
            note.element == "valueQuantity" and note.action is AssemblyAction.DROPPED
            for note in assembled.notes
        )

    def test_a_unit_beginning_with_a_solidus_is_still_a_valid_quantity(self) -> None:
        """The compound guard must not reject "74/min", where "/min" is the unit."""
        assembled = assemble_bundle(
            [
                PATIENT,
                entity("Observation", "obs-1", "code", "heart rate"),
                entity("Observation", "obs-1", "valueQuantity", "74/min"),
            ],
            seed=SEED,
        )

        assert resources_of(assembled.bundle, "Observation")[0]["valueQuantity"]["value"] == 74

    @pytest.mark.parametrize("value", ["yesterday", "2024-13-01", "not a date", "62"])
    def test_non_dates_are_dropped(self, value: str) -> None:
        assembled = assemble_bundle([entity("Patient", "patient-1", "birthDate", value)], seed=SEED)

        assert "birthDate" not in resources_of(assembled.bundle, "Patient")[0]

    def test_an_instant_without_a_timezone_is_dropped(self) -> None:
        """FHIR instant requires full precision and an offset; a date is not one."""
        assembled = assemble_bundle(
            [
                PATIENT,
                entity("Observation", "obs-1", "code", "heart rate"),
                entity("Observation", "obs-1", "issued", "2024-01-15"),
            ],
            seed=SEED,
        )

        assert "issued" not in resources_of(assembled.bundle, "Observation")[0]

    def test_a_datatype_with_no_single_string_form_is_dropped_and_reported(self) -> None:
        """Extension needs a url; inventing one would fabricate structure."""
        assembled = assemble_bundle(
            [entity("Patient", "patient-1", "extension", "race: declined")], seed=SEED
        )

        patient = resources_of(assembled.bundle, "Patient")[0]
        assert "extension" not in patient
        assert assembled.notes[0].action is AssemblyAction.DROPPED
        assert "no single-string form" in assembled.notes[0].detail


class TestReferenceWiring:
    def test_a_clinical_resource_is_wired_to_the_only_patient(self) -> None:
        assembled = assemble_bundle(
            [PATIENT, entity("Condition", "cond-1", "code", "asthma")], seed=SEED
        )

        patient_url = assembled.bundle["entry"][0]["fullUrl"]
        condition = resources_of(assembled.bundle, "Condition")[0]
        assert condition["subject"] == {"reference": patient_url}

    def test_wiring_the_subject_is_reported_but_not_called_inferred(self) -> None:
        """It cannot be wrong given the input, so it must not dilute machine-inferred."""
        assembled = assemble_bundle(
            [PATIENT, entity("Condition", "cond-1", "code", "asthma")], seed=SEED
        )

        subject_notes = [note for note in assembled.notes if note.element == "subject"]
        assert [note.action for note in subject_notes] == [AssemblyAction.WIRED]

    def test_an_ambiguous_target_degrades_to_display_text(self) -> None:
        assembled = assemble_bundle(
            [
                PATIENT,
                entity("Encounter", "enc-1", "period", "2024-01-15"),
                entity("Encounter", "enc-2", "period", "2023-06-01"),
                entity("Observation", "obs-1", "code", "heart rate"),
                entity("Observation", "obs-1", "encounter", "the follow-up visit"),
            ],
            seed=SEED,
        )

        observation = resources_of(assembled.bundle, "Observation")[0]
        assert observation["encounter"] == {"display": "the follow-up visit"}
        assert any(
            note.element == "encounter" and note.action is AssemblyAction.UNRESOLVED
            for note in assembled.notes
        )

    def test_a_missing_subject_with_two_patients_is_reported_not_guessed(self) -> None:
        assembled = assemble_bundle(
            [
                entity("Patient", "patient-1", "gender", "male"),
                entity("Patient", "patient-2", "gender", "female"),
                entity("Condition", "cond-1", "code", "asthma"),
            ],
            seed=SEED,
        )

        condition = resources_of(assembled.bundle, "Condition")[0]
        assert "subject" not in condition
        assert any(
            note.element == "subject" and note.action is AssemblyAction.UNRESOLVED
            for note in assembled.notes
        )

    def test_an_ambiguous_reference_element_is_never_auto_wired(self) -> None:
        """reasonReference can point at several types, so a singleton proves nothing."""
        assembled = assemble_bundle(
            [
                PATIENT,
                entity("Condition", "cond-1", "code", "asthma"),
                entity("MedicationRequest", "med-1", "medicationCodeableConcept", "albuterol"),
                entity("MedicationRequest", "med-1", "reasonReference", "the asthma"),
            ],
            seed=SEED,
        )

        request = resources_of(assembled.bundle, "MedicationRequest")[0]
        assert request["reasonReference"] == [{"display": "the asthma"}]


class TestRequiredDefaults:
    def test_a_required_element_absent_from_the_source_is_filled_and_reported(self) -> None:
        assembled = assemble_bundle(
            [PATIENT, entity("Observation", "obs-1", "code", "heart rate")], seed=SEED
        )

        observation = resources_of(assembled.bundle, "Observation")[0]
        assert observation["status"] == "final"
        assert any(
            note.element == "status" and note.action is AssemblyAction.INFERRED
            for note in assembled.notes
        )

    def test_an_extracted_value_is_never_overwritten_by_a_default(self) -> None:
        assembled = assemble_bundle(
            [
                PATIENT,
                entity("Observation", "obs-1", "code", "heart rate"),
                entity("Observation", "obs-1", "status", "preliminary"),
            ],
            seed=SEED,
        )

        observation = resources_of(assembled.bundle, "Observation")[0]
        assert observation["status"] == "preliminary"
        assert not any(note.action is AssemblyAction.INFERRED for note in assembled.notes)

    def test_a_resource_type_with_no_required_defaults_gets_none(self) -> None:
        """Condition has no 1..1 coded status, so filling one would be fabrication."""
        assembled = assemble_bundle(
            [PATIENT, entity("Condition", "cond-1", "code", "asthma")], seed=SEED
        )

        condition = resources_of(assembled.bundle, "Condition")[0]
        assert "clinicalStatus" not in condition
        assert "verificationStatus" not in condition


class TestProvenanceTags:
    def test_every_resource_is_marked_ai_derived(self) -> None:
        assembled = assemble_bundle([PATIENT, *TWO_VITALS], seed=SEED)

        for entry in assembled.bundle["entry"]:
            assert AI_DERIVED in tags_of(entry["resource"])

    def test_only_resources_with_a_fabricated_value_are_machine_inferred(self) -> None:
        assembled = assemble_bundle(
            [
                PATIENT,
                entity("Condition", "cond-1", "code", "asthma"),
                entity("Observation", "obs-1", "code", "heart rate"),
            ],
            seed=SEED,
        )

        tagged = {
            entry["resource"]["resourceType"]
            for entry in assembled.bundle["entry"]
            if MACHINE_INFERRED in tags_of(entry["resource"])
        }
        # Observation.status was fabricated; Condition only had its subject wired.
        assert tagged == {"Observation"}

    def test_the_inferred_entry_indexes_match_the_tagged_entries(self) -> None:
        assembled = assemble_bundle([PATIENT, *TWO_VITALS], seed=SEED)

        tagged = {
            index
            for index, entry in enumerate(assembled.bundle["entry"])
            if MACHINE_INFERRED in tags_of(entry["resource"])
        }
        assert tagged == assembled.inferred_entry_indexes


class TestDeterminism:
    def test_the_same_entities_and_seed_produce_an_identical_bundle(self) -> None:
        first = assemble_bundle([PATIENT, *TWO_VITALS], seed=SEED)
        second = assemble_bundle([PATIENT, *TWO_VITALS], seed=SEED)

        assert first.bundle == second.bundle
        assert first.notes == second.notes

    def test_a_different_seed_changes_identifiers_but_not_content(self) -> None:
        """Two conversions must not collide when their instance slugs coincide."""
        first = assemble_bundle([PATIENT], seed="cnv_one")
        second = assemble_bundle([PATIENT], seed="cnv_two")

        assert first.bundle["entry"][0]["fullUrl"] != second.bundle["entry"][0]["fullUrl"]
        assert first.bundle["entry"][0]["resource"] == second.bundle["entry"][0]["resource"]

    def test_entry_identifiers_are_urn_uuids(self) -> None:
        assembled = assemble_bundle([PATIENT, *TWO_VITALS], seed=SEED)

        for entry in assembled.bundle["entry"]:
            assert entry["fullUrl"].startswith("urn:uuid:")


class TestPrivacy:
    def test_no_note_ever_repeats_an_extracted_value(self) -> None:
        """Notes are logged; values are PHI, so the two must not overlap."""
        secret = "SECRET-CLINICAL-DETAIL"
        assembled = assemble_bundle(
            [
                entity("Patient", "patient-1", "birthDate", secret),
                entity("Patient", "patient-1", "extension", secret),
                entity("Condition", "cond-1", "code", secret),
                entity("Observation", "obs-1", "encounter", secret),
            ],
            seed=SEED,
        )

        assert assembled.notes, "expected this fixture to produce notes"
        for note in assembled.notes:
            assert secret not in note.detail
            assert secret not in note.element
            assert secret not in note.resource_type

    def test_the_instance_slug_is_not_recoverable_from_an_entry_identifier(self) -> None:
        assembled = assemble_bundle(
            [entity("Patient", "patient-jane-doe", "gender", "female")], seed=SEED
        )

        assert "jane" not in assembled.bundle["entry"][0]["fullUrl"]


class TestStructuralValidity:
    """The meaningful version of the old ``require_fhir_bundle`` guard.

    The assembler is trusted code now, so checking that it returned a collection
    Bundle is tautological. Checking that every resource it built satisfies the
    typed FHIR models is not, and it is what L1 would ask.
    """

    def test_the_bundle_is_a_collection(self) -> None:
        assembled = assemble_bundle([PATIENT, *TWO_VITALS], seed=SEED)

        assert assembled.bundle["resourceType"] == "Bundle"
        assert assembled.bundle["type"] == "collection"

    def test_every_assembled_resource_satisfies_its_typed_fhir_model(self) -> None:
        assembled = assemble_bundle(
            [
                entity("Patient", "patient-1", "name", "Alexandra Q Mosciski"),
                entity("Patient", "patient-1", "gender", "male"),
                entity("Patient", "patient-1", "birthDate", "1962-01-15"),
                entity("Encounter", "enc-1", "period", "2024-01-15"),
                entity("Encounter", "enc-1", "reasonCode", "routine follow-up"),
                entity("Condition", "cond-1", "code", "type 2 diabetes mellitus"),
                entity("MedicationRequest", "med-1", "medicationCodeableConcept", "metformin"),
                entity("MedicationRequest", "med-1", "dosageInstruction", "500 mg twice daily"),
                *TWO_VITALS,
            ],
            seed=SEED,
        )

        for entry in assembled.bundle["entry"]:
            resource = entry["resource"]
            model = typed_model_for(resource["resourceType"])
            assert model is not None, resource["resourceType"]
            try:
                model.model_validate(resource)
            except ValidationError as exc:  # pragma: no cover - failure path
                pytest.fail(f"{resource['resourceType']} is not valid FHIR: {exc}")
