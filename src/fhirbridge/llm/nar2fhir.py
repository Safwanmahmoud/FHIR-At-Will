"""Grounded entity extraction: the model-facing half of narrative-to-FHIR.

One model call extracts ``resourceType`` / ``instance`` / ``keyword`` / ``value``
entities, constrained to the catalog below. Everything after that is deterministic
and lives in :mod:`fhirbridge.fhir.assemble`, which needs no model at all.

The split is drawn where a model earns its keep. Reading a sentence to find the
facts, and recognizing that two of them describe the same measurement, requires
language understanding. Choosing the FHIR datatype for a fact does not, and a model
asked to do it will occasionally invent a code or nest a string where an object
belongs.

``instance`` is what makes the deterministic half possible. Without it the stream
is flat, and "blood pressure", "128/82 mmHg", "heart rate", "74/min" can only be
paired by array order, which no model guarantees.
"""

from __future__ import annotations

import re
from typing import Any, Final

from fhirbridge.domain.errors import LlmSchemaViolationError

MAX_EXTRACTED_ENTITIES: Final[int] = 500

ENTITY_FIELDS: Final[frozenset[str]] = frozenset({"resourceType", "instance", "keyword", "value"})

_INSTANCE_SLUG: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
"""Bounded lowercase slug.

Constrained rather than free text for two reasons: it caps what a model can put in
a grouping key, and it discourages the key from carrying a name. The slug never
reaches a log or an identifier -- entry ``fullUrl`` values are hashes of it -- but
narrowing the shape keeps that guarantee cheap to hold.
"""

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
    "Organization": ("A recognized grouping such as a company, institution, practice, or insurer."),
    "Patient": ("Demographics and administrative information about an individual receiving care."),
    "Practitioner": "A person directly or indirectly involved in providing healthcare.",
    "PractitionerRole": (
        "Roles, locations, specialties, and services a practitioner performs for an organization."
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


def parse_entities(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Validate and normalize the extraction call's output without echoing PHI.

    All-or-nothing on purpose. A model that returned one malformed entity was not
    following the schema, and silently keeping the rest would hand assembly a
    partial picture of the narrative while reporting nothing.
    """
    raw_entities = payload.get("entities")
    if not isinstance(raw_entities, list):
        raise LlmSchemaViolationError("The extraction model did not return an entities array.")
    if len(raw_entities) > MAX_EXTRACTED_ENTITIES:
        raise LlmSchemaViolationError("The extraction model returned too many entities.")

    entities: list[dict[str, str]] = []
    for raw in raw_entities:
        if not isinstance(raw, dict) or set(raw) != ENTITY_FIELDS:
            raise LlmSchemaViolationError(
                "Each extracted entity must contain only resourceType, instance, keyword, "
                "and value."
            )
        if any(not isinstance(raw[field], str) or not raw[field].strip() for field in raw):
            raise LlmSchemaViolationError("Extracted entity fields must be non-empty strings.")

        resource_type = raw["resourceType"].strip()
        instance = raw["instance"].strip()
        keyword = raw["keyword"].strip()

        allowed = OBSERVED_RESOURCE_KEYS.get(resource_type)
        if allowed is None or keyword not in allowed:
            raise LlmSchemaViolationError(
                "The extraction model returned a resource type or key outside the catalog."
            )
        if not _INSTANCE_SLUG.match(instance):
            raise LlmSchemaViolationError(
                "Each extracted entity's instance must be a lowercase slug of letters, "
                "digits, and hyphens."
            )
        entities.append(
            {
                "resourceType": resource_type,
                "instance": instance,
                "keyword": keyword,
                # The only field whose source wording is preserved verbatim.
                "value": raw["value"],
            }
        )
    return entities


__all__ = [
    "ENTITY_FIELDS",
    "MAX_EXTRACTED_ENTITIES",
    "OBSERVED_RESOURCE_KEYS",
    "RESOURCE_DESCRIPTIONS",
    "parse_entities",
    "resource_catalog_text",
]
