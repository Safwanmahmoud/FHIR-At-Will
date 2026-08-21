"""L1 structural validation (AGENTS.md 10).

The interesting cases are the ones where L1 must *not* claim a pass: a resource
type that only exists after R4, and an R4 type with no typed model in this build.
"""

from __future__ import annotations

from typing import Any

import pytest

from fhirbridge.validation.models import IssueSeverity, LayerStatus, ValidationLayer
from fhirbridge.validation.structural import validate_structure
from tests.helpers import OBSERVATION


def test_a_conformant_observation_passes_and_is_type_checked() -> None:
    result = validate_structure(OBSERVATION)

    assert result.result.status is LayerStatus.PASSED
    assert result.resource_type == "Observation"
    assert result.resource_count == 1
    assert result.type_checked is True
    assert result.typed is not None
    assert result.result.issues == []


def test_the_layer_is_always_marked_blocking() -> None:
    assert validate_structure(OBSERVATION).result.blocking is True
    assert validate_structure({}).result.blocking is True


@pytest.mark.parametrize("payload", ["not a resource", 42, None, [], ["Observation"]])
def test_a_non_object_payload_is_fatal(payload: object) -> None:
    result = validate_structure(payload)

    assert result.result.status is LayerStatus.FAILED
    assert result.typed is None
    assert result.result.issues[0].severity is IssueSeverity.FATAL


def test_a_payload_without_a_resource_type_is_fatal() -> None:
    result = validate_structure({"status": "final"})

    issue = result.result.issues[0]
    assert issue.severity is IssueSeverity.FATAL
    assert issue.expression == "$this.resourceType"
    assert "no 'resourceType'" in issue.message


def test_an_empty_resource_type_is_fatal() -> None:
    assert validate_structure({"resourceType": ""}).result.status is LayerStatus.FAILED


def test_a_post_r4_resource_type_is_rejected_rather_than_accepted_as_r4() -> None:
    """``Citation`` exists in R4B/R5 but not in R4 4.0.1.

    The typed models in this build are R4B, so a naive round-trip would accept
    it. Gating on the R4 type list first is what keeps the 4.0.1 claim honest.
    """
    result = validate_structure({"resourceType": "Citation", "status": "active"})

    issue = result.result.issues[0]
    assert issue.severity is IssueSeverity.FATAL
    assert issue.code == "not-supported"
    assert "FHIR R4 (4.0.1)" in issue.message


def test_an_abstract_type_cannot_be_instantiated() -> None:
    result = validate_structure({"resourceType": "Resource"})

    assert result.result.status is LayerStatus.FAILED
    assert "abstract" in result.result.issues[0].message


def test_an_r4_type_with_no_typed_model_warns_and_defers_to_l2() -> None:
    """``MedicinalProduct`` is R4-only, so there is no R4B model to check it with.

    That must surface as "not type-checked", never as a pass.
    """
    result = validate_structure({"resourceType": "MedicinalProduct", "id": "mp-1"})

    assert result.type_checked is False
    assert result.typed is None
    assert result.result.status is LayerStatus.PASSED  # no blocking issue, but...
    warning = result.result.issues[0]
    assert warning.severity is IssueSeverity.WARNING
    assert warning.code == "incomplete"
    assert "Deferred to L2" in warning.message
    assert any("rests entirely on L2" in note for note in result.result.notes)


def test_an_unknown_element_is_an_error_with_a_fhirpath_expression() -> None:
    result = validate_structure(OBSERVATION | {"notAnElement": "x"})

    assert result.result.status is LayerStatus.FAILED
    codes = {issue.code for issue in result.result.issues}
    assert "structure" in codes


def test_a_missing_required_element_is_reported_as_required() -> None:
    result = validate_structure({"resourceType": "Observation", "id": "o"})

    assert result.result.status is LayerStatus.FAILED
    assert any(issue.code == "required" for issue in result.result.issues)


def test_a_bad_primitive_format_points_at_the_element() -> None:
    result = validate_structure(OBSERVATION | {"effectiveDateTime": "the third of May"})

    assert result.result.status is LayerStatus.FAILED
    expressions = {issue.expression for issue in result.result.issues}
    assert "Observation.effectiveDateTime" in expressions


def test_nested_errors_render_array_indices_in_the_expression() -> None:
    broken = {
        "resourceType": "Observation",
        "status": "preliminary",
        "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4", "display": 12}]},
    }

    result = validate_structure(broken)

    expressions = {issue.expression for issue in result.result.issues}
    assert any(expr and "coding[0]" in expr for expr in expressions)


class TestBundles:
    def _bundle(self, *resources: dict[str, Any]) -> dict[str, Any]:
        return {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [{"resource": resource} for resource in resources],
        }

    def test_entries_are_counted(self) -> None:
        result = validate_structure(self._bundle(OBSERVATION, OBSERVATION))

        assert result.resource_type == "Bundle"
        assert result.resource_count == 2
        assert result.result.status is LayerStatus.PASSED

    def test_an_empty_bundle_warns_that_there_is_nothing_to_validate(self) -> None:
        result = validate_structure({"resourceType": "Bundle", "type": "collection"})

        assert result.resource_count == 0
        assert any("nothing to validate" in issue.message for issue in result.result.issues)

    def test_entries_without_a_resource_are_not_counted(self) -> None:
        payload = {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [{"fullUrl": "urn:uuid:1"}, {"resource": OBSERVATION}],
        }

        assert validate_structure(payload).resource_count == 1

    def test_a_broken_entry_fails_the_whole_bundle(self) -> None:
        result = validate_structure(self._bundle({"resourceType": "Observation"}))

        assert result.result.status is LayerStatus.FAILED


def test_the_layer_number_matches_the_cascade_table() -> None:
    result = validate_structure(OBSERVATION).result

    assert result.layer is ValidationLayer.STRUCTURAL
    assert result.layer_number == 1


def test_issue_counts_are_derived_from_the_issue_list() -> None:
    result = validate_structure({"resourceType": "MedicinalProduct"}).result

    assert result.warnings == 1
    assert result.errors == 0
