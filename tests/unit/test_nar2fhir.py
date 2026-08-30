from __future__ import annotations

import pytest

from fhirbridge.domain.errors import LlmSchemaViolationError
from fhirbridge.llm.nar2fhir import (
    MAX_EXTRACTED_ENTITIES,
    parse_entities,
    resource_catalog_text,
)


def entity(**overrides: str) -> dict[str, str]:
    return {
        "resourceType": "Patient",
        "instance": "patient-1",
        "keyword": "birthDate",
        "value": "1990-05-12",
    } | overrides


def test_catalog_contains_descriptions_and_observed_keys() -> None:
    catalog = resource_catalog_text()

    assert "Observation" in catalog
    assert "Measurements and simple assertions" in catalog
    assert "valueQuantity" in catalog


def test_extracted_entities_are_catalog_constrained() -> None:
    entities = parse_entities({"entities": [entity()]})

    assert entities == [
        {
            "resourceType": "Patient",
            "instance": "patient-1",
            "keyword": "birthDate",
            "value": "1990-05-12",
        }
    ]


def test_the_source_wording_of_a_value_is_preserved() -> None:
    """Only identifiers are normalized; a value is evidence and must survive intact."""
    entities = parse_entities({"entities": [entity(value="  128/82 mmHg  ")]})

    assert entities[0]["value"] == "  128/82 mmHg  "


@pytest.mark.parametrize(
    ("invalid", "reason"),
    [
        (entity(resourceType="Unknown"), "resource type outside the catalog"),
        (entity(keyword="code"), "key not allowed for this resource type"),
        ({k: v for k, v in entity().items() if k != "instance"}, "instance missing"),
        ({k: v for k, v in entity().items() if k != "value"}, "value missing"),
        (entity() | {"extra": "x"}, "unexpected field"),
        (entity(value=" "), "blank value"),
        (entity(instance="Patient_1"), "uppercase and underscore in slug"),
        (entity(instance="-leading-hyphen"), "slug may not start with a hyphen"),
        (entity(instance="a" * 65), "slug too long"),
    ],
)
def test_invalid_extracted_entities_are_rejected(invalid: dict[str, str], reason: str) -> None:
    with pytest.raises(LlmSchemaViolationError):
        parse_entities({"entities": [invalid]}), reason


def test_one_invalid_entity_rejects_the_whole_payload() -> None:
    """All-or-nothing: a partial parse would silently narrow the narrative."""
    with pytest.raises(LlmSchemaViolationError):
        parse_entities({"entities": [entity(), entity(resourceType="Unknown")]})


def test_a_missing_entities_array_is_a_schema_violation() -> None:
    with pytest.raises(LlmSchemaViolationError):
        parse_entities({"facts": []})


def test_an_oversized_extraction_is_rejected() -> None:
    payload = {"entities": [entity()] * (MAX_EXTRACTED_ENTITIES + 1)}

    with pytest.raises(LlmSchemaViolationError):
        parse_entities(payload)


def test_entities_sharing_an_instance_are_returned_in_order() -> None:
    """Assembly groups on (resourceType, instance), so both must survive parsing."""
    entities = parse_entities(
        {
            "entities": [
                entity(resourceType="Observation", instance="obs-hr", keyword="code", value="HR"),
                entity(
                    resourceType="Observation",
                    instance="obs-hr",
                    keyword="valueQuantity",
                    value="74/min",
                ),
            ]
        }
    )

    assert [item["instance"] for item in entities] == ["obs-hr", "obs-hr"]
    assert [item["keyword"] for item in entities] == ["code", "valueQuantity"]
