"""The FHIR R4 (4.0.1) resource-type registry.

``fhir.resources`` 8.x ships typed models for R4B (4.3.0) and R5, but not for R4
(4.0.1), which is the version this service targets and which US Core 9.0.0 is
built on. The two are close but not identical: R4B added ``Citation``,
``Evidence`` and ``SubscriptionStatus`` and dropped ``RiskEvidenceSynthesis``,
``EffectEvidenceSynthesis`` and the ``MedicinalProduct*`` family.

So L1 does two things rather than one:

1. Gate on :data:`R4_RESOURCE_TYPES`, the authoritative 4.0.1 list, so an
   R4B-only resource type can never slip through as "structurally valid R4".
2. Type-check with the R4B model when one exists, and otherwise report that L1
   could not type-check the resource and defer to L2 — the HAPI validator
   sidecar, which runs with ``-version 4.0.1`` and is authoritative.

See docs/adr/0004 and OPEN_QUESTIONS.md#Q1.
"""

from __future__ import annotations

import importlib
from functools import lru_cache
from types import ModuleType
from typing import Any, Final

ABSTRACT_R4_TYPES: Final[frozenset[str]] = frozenset({"Resource", "DomainResource"})
"""Abstract types that appear in the spec's resource-type list but cannot be instantiated."""

R4_RESOURCE_TYPES: Final[frozenset[str]] = frozenset(
    {
        "Account",
        "ActivityDefinition",
        "AdverseEvent",
        "AllergyIntolerance",
        "Appointment",
        "AppointmentResponse",
        "AuditEvent",
        "Basic",
        "Binary",
        "BiologicallyDerivedProduct",
        "BodyStructure",
        "Bundle",
        "CapabilityStatement",
        "CarePlan",
        "CareTeam",
        "CatalogEntry",
        "ChargeItem",
        "ChargeItemDefinition",
        "Claim",
        "ClaimResponse",
        "ClinicalImpression",
        "CodeSystem",
        "Communication",
        "CommunicationRequest",
        "CompartmentDefinition",
        "Composition",
        "ConceptMap",
        "Condition",
        "Consent",
        "Contract",
        "Coverage",
        "CoverageEligibilityRequest",
        "CoverageEligibilityResponse",
        "DetectedIssue",
        "Device",
        "DeviceDefinition",
        "DeviceMetric",
        "DeviceRequest",
        "DeviceUseStatement",
        "DiagnosticReport",
        "DocumentManifest",
        "DocumentReference",
        "DomainResource",
        "EffectEvidenceSynthesis",
        "Encounter",
        "Endpoint",
        "EnrollmentRequest",
        "EnrollmentResponse",
        "EpisodeOfCare",
        "EventDefinition",
        "Evidence",
        "EvidenceVariable",
        "ExampleScenario",
        "ExplanationOfBenefit",
        "FamilyMemberHistory",
        "Flag",
        "Goal",
        "GraphDefinition",
        "Group",
        "GuidanceResponse",
        "HealthcareService",
        "ImagingStudy",
        "Immunization",
        "ImmunizationEvaluation",
        "ImmunizationRecommendation",
        "ImplementationGuide",
        "InsurancePlan",
        "Invoice",
        "Library",
        "Linkage",
        "List",
        "Location",
        "Measure",
        "MeasureReport",
        "Media",
        "Medication",
        "MedicationAdministration",
        "MedicationDispense",
        "MedicationKnowledge",
        "MedicationRequest",
        "MedicationStatement",
        "MedicinalProduct",
        "MedicinalProductAuthorization",
        "MedicinalProductContraindication",
        "MedicinalProductIndication",
        "MedicinalProductIngredient",
        "MedicinalProductInteraction",
        "MedicinalProductManufactured",
        "MedicinalProductPackaged",
        "MedicinalProductPharmaceutical",
        "MedicinalProductUndesirableEffect",
        "MessageDefinition",
        "MessageHeader",
        "MolecularSequence",
        "NamingSystem",
        "NutritionOrder",
        "Observation",
        "ObservationDefinition",
        "OperationDefinition",
        "OperationOutcome",
        "Organization",
        "OrganizationAffiliation",
        "Parameters",
        "Patient",
        "PaymentNotice",
        "PaymentReconciliation",
        "Person",
        "PlanDefinition",
        "Practitioner",
        "PractitionerRole",
        "Procedure",
        "Provenance",
        "Questionnaire",
        "QuestionnaireResponse",
        "RelatedPerson",
        "RequestGroup",
        "ResearchDefinition",
        "ResearchElementDefinition",
        "ResearchStudy",
        "ResearchSubject",
        "Resource",
        "RiskAssessment",
        "RiskEvidenceSynthesis",
        "Schedule",
        "SearchParameter",
        "ServiceRequest",
        "Slot",
        "Specimen",
        "SpecimenDefinition",
        "StructureDefinition",
        "StructureMap",
        "Subscription",
        "Substance",
        "SubstanceNucleicAcid",
        "SubstancePolymer",
        "SubstanceProtein",
        "SubstanceReferenceInformation",
        "SubstanceSourceMaterial",
        "SubstanceSpecification",
        "SupplyDelivery",
        "SupplyRequest",
        "Task",
        "TerminologyCapabilities",
        "TestReport",
        "TestScript",
        "ValueSet",
        "VerificationResult",
        "VisionPrescription",
    }
)
"""The 148 entries of ``http://hl7.org/fhir/ValueSet/resource-types`` at 4.0.1."""

CONCRETE_R4_RESOURCE_TYPES: Final[frozenset[str]] = R4_RESOURCE_TYPES - ABSTRACT_R4_TYPES

_TYPED_MODEL_PACKAGE: Final[str] = "fhir.resources.R4B"

# Resource types whose Python module name is not simply the lowercased type name.
_MODULE_OVERRIDES: Final[dict[str, str]] = {}


class UnknownResourceTypeError(LookupError):
    """Raised when a resource type is not part of FHIR R4 4.0.1."""


def is_r4_resource_type(resource_type: str) -> bool:
    """True when ``resource_type`` exists in FHIR R4 4.0.1."""
    return resource_type in R4_RESOURCE_TYPES


def is_instantiable(resource_type: str) -> bool:
    """True when ``resource_type`` is a concrete (non-abstract) R4 resource."""
    return resource_type in CONCRETE_R4_RESOURCE_TYPES


@lru_cache(maxsize=256)
def typed_model_for(resource_type: str) -> type[Any] | None:
    """Return the ``fhir.resources`` R4B model class, or None if R4B lacks it.

    A None result is not an error: it means L1 cannot type-check this resource
    type and the caller must record a "not type-checked" note and rely on L2.
    """
    if not is_instantiable(resource_type):
        return None
    module_name = _MODULE_OVERRIDES.get(resource_type, resource_type.lower())
    try:
        module: ModuleType = importlib.import_module(f"{_TYPED_MODEL_PACKAGE}.{module_name}")
    except ModuleNotFoundError:
        return None
    model = getattr(module, resource_type, None)
    return model if isinstance(model, type) else None


@lru_cache(maxsize=1)
def types_without_typed_model() -> frozenset[str]:
    """R4 resource types with no R4B typed model available for L1."""
    return frozenset(
        name for name in sorted(CONCRETE_R4_RESOURCE_TYPES) if typed_model_for(name) is None
    )


__all__ = [
    "ABSTRACT_R4_TYPES",
    "CONCRETE_R4_RESOURCE_TYPES",
    "R4_RESOURCE_TYPES",
    "UnknownResourceTypeError",
    "is_instantiable",
    "is_r4_resource_type",
    "typed_model_for",
    "types_without_typed_model",
]
