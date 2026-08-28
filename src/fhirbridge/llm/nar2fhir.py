"""Two-stage narrative-to-FHIR prompt support.

The first model call extracts grounded ``resourceType`` / ``keyword`` / ``value``
triples.  The second call receives those facts plus compact FHIR datatype guidance
and assembles the final Bundle.  Keeping extraction separate from assembly limits
the generator's vocabulary to elements observed in the validation corpus without
pretending that a flat string is already valid FHIR.
"""

from __future__ import annotations

import types
from collections.abc import Iterable
from typing import Any, Final, Union, get_args, get_origin

from fhirbridge.domain.errors import LlmSchemaViolationError
from fhirbridge.fhir.resource_types import typed_model_for

MAX_EXTRACTED_ENTITIES: Final[int] = 500

RESOURCE_DESCRIPTIONS: Final[dict[str, str]] = {
    "AllergyIntolerance": (
        "Risk of harmful or undesirable physiological response unique to an individual "
        "and associated with exposure to a substance."
    ),
    "CarePlan": (
        "Describes how practitioners intend to deliver care for a patient, group, or "
        "community over a period of time."
    ),
    "CareTeam": (
        "People and organizations who plan to participate in coordinating and delivering "
        "care for a patient."
    ),
    "Claim": (
        "A provider-issued list of professional services and products sent to an insurer "
        "for reimbursement."
    ),
    "Condition": (
        "A clinical condition, problem, diagnosis, or other issue that has risen to a "
        "level of concern."
    ),
    "Device": "A manufactured medical or non-medical item used in providing healthcare.",
    "DiagnosticReport": (
        "Findings and interpretation of diagnostic tests, including atomic results, "
        "images, text, and coded interpretations."
    ),
    "DocumentReference": (
        "Metadata and a reference for discovering and managing a clinical or other document."
    ),
    "Encounter": (
        "An interaction between a patient and healthcare providers for delivering services "
        "or assessing health."
    ),
    "ExplanationOfBenefit": (
        "Claim details and adjudication information used to explain benefits to a subscriber."
    ),
    "ImagingStudy": "Representation of content produced in a DICOM imaging study.",
    "Immunization": "An administered vaccine event or a reported immunization record.",
    "Location": "Details about a physical place where healthcare services are provided.",
    "Medication": "Identification and definition of a medication.",
    "MedicationAdministration": (
        "An event in which a patient consumes or is administered a medication."
    ),
    "MedicationRequest": (
        "An order or request for medication supply and instructions for administration."
    ),
    "Observation": "Measurements and simple assertions about a patient or other subject.",
    "Organization": (
        "A recognized grouping such as a company, institution, practice, or insurer."
    ),
    "Patient": (
        "Demographics and administrative information about an individual receiving care."
    ),
    "Practitioner": "A person directly or indirectly involved in providing healthcare.",
    "PractitionerRole": (
        "Roles, locations, specialties, and services a practitioner performs for an "
        "organization."
    ),
    "Procedure": "An action performed on or for a patient.",
    "Provenance": "Entities and processes involved in producing or influencing a resource.",
    "SupplyDelivery": "A record of delivery of a supplied item.",
}

OBSERVED_RESOURCE_KEYS: Final[dict[str, frozenset[str]]] = {
    "AllergyIntolerance": frozenset(
        {
            "category",
            "clinicalStatus",
            "code",
            "criticality",
            "patient",
            "reaction",
            "recordedDate",
            "type",
            "verificationStatus",
        }
    ),
    "CarePlan": frozenset(
        {
            "activity",
            "addresses",
            "careTeam",
            "category",
            "encounter",
            "intent",
            "period",
            "status",
            "subject",
        }
    ),
    "CareTeam": frozenset(
        {
            "encounter",
            "managingOrganization",
            "participant",
            "period",
            "reasonCode",
            "status",
            "subject",
        }
    ),
    "Claim": frozenset(
        {
            "billablePeriod",
            "created",
            "diagnosis",
            "facility",
            "insurance",
            "item",
            "patient",
            "prescription",
            "priority",
            "procedure",
            "provider",
            "status",
            "supportingInfo",
            "total",
            "type",
            "use",
        }
    ),
    "Condition": frozenset(
        {
            "abatementDateTime",
            "category",
            "clinicalStatus",
            "code",
            "encounter",
            "onsetDateTime",
            "recordedDate",
            "subject",
            "verificationStatus",
        }
    ),
    "Device": frozenset(
        {
            "deviceName",
            "distinctIdentifier",
            "expirationDate",
            "lotNumber",
            "manufactureDate",
            "patient",
            "serialNumber",
            "status",
            "type",
            "udiCarrier",
        }
    ),
    "DiagnosticReport": frozenset(
        {
            "category",
            "code",
            "effectiveDateTime",
            "encounter",
            "issued",
            "performer",
            "presentedForm",
            "result",
            "status",
            "subject",
        }
    ),
    "DocumentReference": frozenset(
        {
            "author",
            "category",
            "content",
            "context",
            "custodian",
            "date",
            "identifier",
            "status",
            "subject",
            "type",
        }
    ),
    "Encounter": frozenset(
        {
            "class",
            "hospitalization",
            "identifier",
            "location",
            "participant",
            "period",
            "reasonCode",
            "serviceProvider",
            "status",
            "subject",
            "type",
        }
    ),
    "ExplanationOfBenefit": frozenset(
        {
            "billablePeriod",
            "careTeam",
            "claim",
            "contained",
            "created",
            "diagnosis",
            "facility",
            "identifier",
            "insurance",
            "insurer",
            "item",
            "outcome",
            "patient",
            "payment",
            "provider",
            "referral",
            "status",
            "total",
            "type",
            "use",
        }
    ),
    "ImagingStudy": frozenset(
        {
            "encounter",
            "identifier",
            "location",
            "numberOfInstances",
            "numberOfSeries",
            "procedureCode",
            "series",
            "started",
            "status",
            "subject",
        }
    ),
    "Immunization": frozenset(
        {
            "encounter",
            "location",
            "occurrenceDateTime",
            "patient",
            "primarySource",
            "status",
            "vaccineCode",
        }
    ),
    "Location": frozenset(
        {
            "address",
            "description",
            "identifier",
            "managingOrganization",
            "mode",
            "name",
            "physicalType",
            "position",
            "status",
            "telecom",
        }
    ),
    "Medication": frozenset({"code", "status"}),
    "MedicationAdministration": frozenset(
        {
            "context",
            "dosage",
            "effectiveDateTime",
            "medicationCodeableConcept",
            "reasonCode",
            "reasonReference",
            "status",
            "subject",
        }
    ),
    "MedicationRequest": frozenset(
        {
            "authoredOn",
            "category",
            "dosageInstruction",
            "encounter",
            "intent",
            "medicationCodeableConcept",
            "medicationReference",
            "reasonCode",
            "reasonReference",
            "requester",
            "status",
            "subject",
        }
    ),
    "Observation": frozenset(
        {
            "category",
            "code",
            "component",
            "effectiveDateTime",
            "encounter",
            "issued",
            "status",
            "subject",
            "valueCodeableConcept",
            "valueQuantity",
            "valueString",
        }
    ),
    "Organization": frozenset(
        {"active", "address", "extension", "identifier", "name", "telecom", "type"}
    ),
    "Patient": frozenset(
        {
            "address",
            "birthDate",
            "communication",
            "deceasedDateTime",
            "extension",
            "gender",
            "identifier",
            "maritalStatus",
            "multipleBirthBoolean",
            "multipleBirthInteger",
            "name",
            "telecom",
        }
    ),
    "Practitioner": frozenset(
        {"active", "address", "extension", "gender", "identifier", "name", "telecom"}
    ),
    "PractitionerRole": frozenset(
        {"code", "location", "organization", "practitioner", "specialty", "telecom"}
    ),
    "Procedure": frozenset(
        {
            "code",
            "encounter",
            "location",
            "performedPeriod",
            "reasonCode",
            "reasonReference",
            "status",
            "subject",
        }
    ),
    "Provenance": frozenset({"agent", "recorded", "target"}),
    "SupplyDelivery": frozenset(
        {"occurrenceDateTime", "patient", "status", "suppliedItem", "type"}
    ),
}

DATATYPE_LEGEND: Final[str] = """\
HumanName: object with family string, given string array, prefix string array, suffix string array
CodeableConcept: object with optional coding array and/or text string
Coding: object with system, code, and display strings; omit system/code unless source supplies them
Reference: object with reference and/or display strings
Quantity: object with numeric value and optional unit, system, and code strings
Period: object with start and/or end ISO 8601 strings
Address: object with line string array and optional city, state, postalCode, country strings
Identifier: object with optional system and value strings
ContactPoint: object with system, value, and optional use strings
Annotation: object with text string and optional time string"""


def resource_catalog_text() -> str:
    """Catalog used by the grounded extraction call."""
    return "\n\n".join(
        "\n".join(
            (
                resource_type,
                f"Description: {RESOURCE_DESCRIPTIONS[resource_type]}",
                f"Allowed keys: {', '.join(sorted(keys))}",
            )
        )
        for resource_type, keys in sorted(OBSERVED_RESOURCE_KEYS.items())
    )


def resource_field_reference(resource_types: Iterable[str]) -> str:
    """Observed element names plus typed-model datatype hints for generation."""
    sections: list[str] = []
    for resource_type in sorted(set(resource_types)):
        keys = OBSERVED_RESOURCE_KEYS.get(resource_type)
        if keys is None:
            continue
        model = typed_model_for(resource_type)
        fields = (
            {
                str(field.alias or name): field
                for name, field in model.model_fields.items()
                if not name.endswith("__ext")
            }
            if model is not None
            else {}
        )
        lines = [f"{resource_type} fields:"]
        for key in sorted(keys):
            field = fields.get(key)
            hint = _annotation_hint(field.annotation) if field is not None else "FHIR R4 element"
            lines.append(f"  {key}: {hint}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def parse_entities(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Validate and normalize the extraction call's output without echoing PHI."""
    raw_entities = payload.get("entities")
    if not isinstance(raw_entities, list):
        raise LlmSchemaViolationError("The extraction model did not return an entities array.")
    if len(raw_entities) > MAX_EXTRACTED_ENTITIES:
        raise LlmSchemaViolationError("The extraction model returned too many entities.")

    entities: list[dict[str, str]] = []
    for raw in raw_entities:
        if not isinstance(raw, dict) or set(raw) != {"resourceType", "keyword", "value"}:
            raise LlmSchemaViolationError(
                "Each extracted entity must contain only resourceType, keyword, and value."
            )
        resource_type = raw.get("resourceType")
        keyword = raw.get("keyword")
        value = raw.get("value")
        if (
            not isinstance(resource_type, str)
            or not resource_type.strip()
            or not isinstance(keyword, str)
            or not keyword.strip()
            or not isinstance(value, str)
            or not value.strip()
        ):
            raise LlmSchemaViolationError("Extracted entity fields must be non-empty strings.")
        allowed = OBSERVED_RESOURCE_KEYS.get(resource_type)
        if allowed is None or keyword not in allowed:
            raise LlmSchemaViolationError(
                "The extraction model returned a resource type or key outside the catalog."
            )
        entities.append(
            {"resourceType": resource_type, "keyword": keyword, "value": value}
        )
    return entities


def require_fhir_bundle(
    payload: dict[str, Any], *, allowed_resource_types: Iterable[str]
) -> dict[str, Any]:
    """Require the generation call to return a collection Bundle of allowed types."""
    if payload.get("resourceType") != "Bundle" or payload.get("type") != "collection":
        raise LlmSchemaViolationError(
            "The generation model did not return a FHIR collection Bundle."
        )
    entries = payload.get("entry")
    if not isinstance(entries, list):
        raise LlmSchemaViolationError("The generated Bundle has no entry array.")

    allowed = set(allowed_resource_types)
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("resource"), dict):
            raise LlmSchemaViolationError(
                "Every generated Bundle entry must contain a resource object."
            )
        resource_type = entry["resource"].get("resourceType")
        if resource_type not in allowed:
            raise LlmSchemaViolationError(
                "The generated Bundle contains a resource type unsupported by extraction."
            )
    return payload


def _annotation_hint(annotation: Any) -> str:
    origin = get_origin(annotation)
    args = tuple(arg for arg in get_args(annotation) if arg is not type(None))
    if origin in (Union, types.UnionType):
        return " | ".join(_annotation_hint(arg) for arg in args)
    if origin is list:
        item = _annotation_hint(args[0]) if args else "any"
        return f"array<{item}>"
    if isinstance(annotation, type):
        name = annotation.__name__
        return name.removesuffix("Type")
    return str(annotation).replace("typing.", "")


__all__ = [
    "DATATYPE_LEGEND",
    "MAX_EXTRACTED_ENTITIES",
    "OBSERVED_RESOURCE_KEYS",
    "RESOURCE_DESCRIPTIONS",
    "parse_entities",
    "require_fhir_bundle",
    "resource_catalog_text",
    "resource_field_reference",
]
