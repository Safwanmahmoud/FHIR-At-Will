from __future__ import annotations

import pytest

from fhirbridge.domain.errors import LlmSchemaViolationError
from fhirbridge.llm.nar2fhir import (
    parse_entities,
    require_fhir_bundle,
    resource_catalog_text,
    resource_field_reference,
)


def test_catalog_contains_descriptions_and_observed_keys() -> None:
    catalog = resource_catalog_text()

    assert "Observation" in catalog
    assert "Measurements and simple assertions" in catalog
    assert "valueQuantity" in catalog


def test_field_reference_includes_fhir_datatypes() -> None:
    reference = resource_field_reference({"Patient", "Observation"})

    assert "Patient fields:" in reference
    assert "name: array<HumanName>" in reference
    assert "Observation fields:" in reference
    assert "valueQuantity: Quantity" in reference


def test_extracted_entities_are_catalog_constrained() -> None:
    entities = parse_entities(
        {
            "entities": [
                {
                    "resourceType": "Patient",
                    "keyword": "birthDate",
                    "value": "1990-05-12",
                }
            ]
        }
    )

    assert entities[0]["keyword"] == "birthDate"


@pytest.mark.parametrize(
    "entity",
    [
        {"resourceType": "Unknown", "keyword": "code", "value": "x"},
        {"resourceType": "Patient", "keyword": "code", "value": "x"},
        {"resourceType": "Patient", "keyword": "birthDate"},
    ],
)
def test_invalid_extracted_entities_are_rejected(entity: dict[str, str]) -> None:
    with pytest.raises(LlmSchemaViolationError):
        parse_entities({"entities": [entity]})


def test_generated_bundle_must_be_collection_with_extracted_types() -> None:
    bundle = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{"resource": {"resourceType": "Patient"}}],
    }

    assert require_fhir_bundle(bundle, allowed_resource_types={"Patient"}) is bundle

    with pytest.raises(LlmSchemaViolationError):
        require_fhir_bundle(bundle, allowed_resource_types={"Observation"})
