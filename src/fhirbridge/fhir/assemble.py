"""Deterministic FHIR R4 Bundle assembly from grounded entity triples.

Narrative conversion is two problems wearing one coat. Deciding *which facts a
sentence contains, and which of them describe the same real-world thing* needs
language understanding. Turning each fact into its declared FHIR datatype, wiring
references and emitting a Bundle has exactly one correct answer per element and
needs none.

This module is the second half. It takes entities that
:func:`fhirbridge.llm.nar2fhir.parse_entities` has already validated and builds the
Bundle with no model call, so the same entities always produce byte-identical
output. That removes the generation step's two failure modes: inventing a
``Coding.system``/``code`` pair the source never supplied, and putting a bare
string where FHIR requires an object.

What it refuses to do matters as much as what it does. A value that cannot be
represented in its declared datatype is dropped and reported, never coerced into
something plausible: ``"62-year-old"`` does not become a ``birthDate``, and
``"128/82 mmHg"`` does not become a ``Quantity`` of 128. Coded concepts get ``text``
(or ``display``) only, because asserting a code is L3's job and needs a terminology
server rather than a string.

Grouping depends on the ``instance`` key. Without it the entity stream is flat and
"blood pressure", "128/82 mmHg", "heart rate", "74/min" cannot be paired except by
array order, which is a model behavior rather than a guarantee -- and mispairing
produces a confident, wrong clinical value. Extraction assigns the key; assembly
only reads it.

Every :class:`AssemblyNote` is PHI-free by construction: notes name an entry index,
an element and a reason, never a value. They are therefore safe to log, unlike the
Bundle itself.
"""

from __future__ import annotations

import re
import types
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Union, get_args, get_origin

from fhirbridge.fhir.resource_types import typed_model_for
from fhirbridge.fhir.tags import AI_DERIVED, MACHINE_INFERRED, tag

_ENTRY_NAMESPACE: Final[uuid.UUID] = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://fhirbridge.org/nar2fhir/bundle-entry"
)
"""Namespace for ``uuid5`` entry identifiers, so a conversion is reproducible."""


class AssemblyAction(StrEnum):
    """What assembly did about an element it could not simply place."""

    DROPPED = "dropped"
    """The value could not be represented in the element's declared datatype."""

    INFERRED = "inferred"
    """A FHIR-required element was filled from the declared default table."""

    WIRED = "wired"
    """A reference was resolved structurally, not from a stated value."""

    UNRESOLVED = "unresolved"
    """A reference target was ambiguous, so the text was kept as ``display``."""

    CONFLICT = "conflict"
    """Two values were extracted for one scalar element; the later was discarded."""


@dataclass(frozen=True, slots=True)
class AssemblyNote:
    """One thing a reviewer needs to know about how a resource was built.

    Carries ``entry_index`` rather than the extraction ``instance`` slug: the slug
    is model-authored text that could echo a name, whereas an index cannot. That
    keeps the whole note set safe to log.
    """

    entry_index: int
    resource_type: str
    element: str
    action: AssemblyAction
    detail: str


@dataclass(frozen=True, slots=True)
class AssembledBundle:
    """A Bundle plus the record of everything assembly could not ground."""

    bundle: dict[str, Any]
    notes: tuple[AssemblyNote, ...]

    @property
    def inferred_entry_indexes(self) -> frozenset[int]:
        """Entries carrying at least one fabricated required value."""
        return frozenset(
            note.entry_index for note in self.notes if note.action is AssemblyAction.INFERRED
        )


class CoercionError(ValueError):
    """A value cannot be represented in its element's declared FHIR datatype."""


# Subjects before the encounter that contextualizes them, before clinical facts, so
# entry order is a property of the data rather than of the model's output order.
_TYPE_ORDER: Final[tuple[str, ...]] = (
    "Patient",
    "Practitioner",
    "PractitionerRole",
    "Organization",
    "Location",
    "Device",
    "Medication",
    "Encounter",
    "Condition",
    "Observation",
    "Procedure",
    "MedicationRequest",
    "MedicationAdministration",
    "Immunization",
    "AllergyIntolerance",
    "DiagnosticReport",
    "ImagingStudy",
    "CarePlan",
    "CareTeam",
    "DocumentReference",
    "SupplyDelivery",
    "Claim",
    "ExplanationOfBenefit",
    "Provenance",
)

# Which resource type a Reference-typed element points at. ``None`` marks elements
# whose target is genuinely ambiguous in FHIR, which are never auto-wired.
_REFERENCE_TARGETS: Final[Mapping[str, str | None]] = {
    "addresses": "Condition",
    "author": "Practitioner",
    "careTeam": "CareTeam",
    "claim": "Claim",
    "context": "Encounter",
    "custodian": "Organization",
    "encounter": "Encounter",
    "facility": "Location",
    "insurer": "Organization",
    "location": "Location",
    "managingOrganization": "Organization",
    "medicationReference": "Medication",
    "organization": "Organization",
    "patient": "Patient",
    "performer": "Practitioner",
    "practitioner": "Practitioner",
    "prescription": "MedicationRequest",
    "provider": "Organization",
    "reasonReference": None,
    "requester": "Practitioner",
    "result": None,
    "serviceProvider": "Organization",
    "subject": "Patient",
    "supportingInfo": None,
    "target": None,
}

# The 1..1 subject element per resource type, wired when extraction omits it.
_SUBJECT_KEYS: Final[Mapping[str, str]] = {
    "AllergyIntolerance": "patient",
    "CarePlan": "subject",
    "Claim": "patient",
    "Condition": "subject",
    "DiagnosticReport": "subject",
    "DocumentReference": "subject",
    "Encounter": "subject",
    "ExplanationOfBenefit": "patient",
    "ImagingStudy": "subject",
    "Immunization": "patient",
    "MedicationAdministration": "subject",
    "MedicationRequest": "subject",
    "Observation": "subject",
    "Procedure": "subject",
}

# Elements FHIR requires at 1..1 that a narrative essentially never states, and for
# which a single defensible constant exists. Reviewed as clinical policy, not code.
#
# Scope is deliberately tight. Elements that are only 0..1 are absent even where a
# default would be convenient, because filling them is fabrication without a
# conformance reason. Required elements with no defensible constant --
# Claim.created, Claim.provider, Immunization.vaccineCode -- are also absent and are
# left missing on purpose, for validation to report.
_REQUIRED_DEFAULTS: Final[Mapping[str, Mapping[str, Any]]] = {
    "CarePlan": {"status": "active", "intent": "plan"},
    "Claim": {"status": "active", "use": "claim"},
    "DiagnosticReport": {"status": "final"},
    "DocumentReference": {"status": "current"},
    "Encounter": {
        "status": "finished",
        "class": {
            "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
            "code": "AMB",
            "display": "ambulatory",
        },
    },
    "ExplanationOfBenefit": {"status": "active", "outcome": "complete", "use": "claim"},
    "ImagingStudy": {"status": "available"},
    "Immunization": {"status": "completed"},
    "MedicationAdministration": {"status": "completed"},
    "MedicationRequest": {"status": "active", "intent": "order"},
    "Observation": {"status": "final"},
    "Procedure": {"status": "completed"},
}

_FULL_DATE: Final[str] = r"\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])"
_PARTIAL_DATE: Final[str] = r"\d{4}(-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01]))?)?"
_TIME: Final[str] = r"([01]\d|2[0-3]):[0-5]\d:([0-5]\d|60)(\.\d+)?"
_TZ: Final[str] = r"(Z|[+-]([01]\d|2[0-3]):[0-5]\d)"

_DATE_RE: Final[re.Pattern[str]] = re.compile(rf"^{_PARTIAL_DATE}$")
_DATETIME_RE: Final[re.Pattern[str]] = re.compile(rf"^({_PARTIAL_DATE}|{_FULL_DATE}T{_TIME}{_TZ})$")
_INSTANT_RE: Final[re.Pattern[str]] = re.compile(rf"^{_FULL_DATE}T{_TIME}{_TZ}$")
_QUANTITY_RE: Final[re.Pattern[str]] = re.compile(r"^(?P<value>-?\d+(?:\.\d+)?)\s*(?P<unit>\S.*)?$")

# "128/82 mmHg" is two numbers and belongs in Observation.component, not in one
# Quantity. A UCUM unit may itself begin with a solidus ("74/min" -> 74 /min), so the
# discriminator has to be a digit after the slash rather than the slash alone.
_COMPOUND_RE: Final[re.Pattern[str]] = re.compile(r"^-?\d+(\.\d+)?\s*/\s*-?\d")

_BOOLEANS: Final[Mapping[str, bool]] = {
    "true": True,
    "yes": True,
    "false": False,
    "no": False,
}


def _temporal(pattern: re.Pattern[str], label: str) -> Callable[[str], str]:
    def coerce(value: str) -> str:
        text = value.strip()
        if not pattern.match(text):
            raise CoercionError(f"not a FHIR {label}")
        return text

    return coerce


def _quantity(value: str) -> dict[str, Any]:
    text = value.strip()
    if _COMPOUND_RE.match(text):
        raise CoercionError("compound value; a single Quantity cannot hold it")
    match = _QUANTITY_RE.match(text)
    if match is None:
        raise CoercionError("no leading numeric value")
    raw = match.group("value")
    quantity: dict[str, Any] = {"value": float(raw) if "." in raw else int(raw)}
    unit = match.group("unit")
    if unit:
        # unit only, never system/code: claiming the string is valid UCUM is a
        # terminology assertion, and this module verifies nothing.
        quantity["unit"] = unit.strip()
    return quantity


def _human_name(value: str) -> dict[str, Any]:
    text = value.strip()
    parts = text.split()
    name: dict[str, Any] = {"text": text}
    if len(parts) >= 2:
        name["family"] = parts[-1]
        name["given"] = parts[:-1]
    return name


def _integer(value: str) -> int:
    try:
        return int(value.strip())
    except ValueError as exc:
        raise CoercionError("not an integer") from exc


def _boolean(value: str) -> bool:
    try:
        return _BOOLEANS[value.strip().lower()]
    except KeyError as exc:
        raise CoercionError("not a boolean") from exc


def _text(key: str) -> Callable[[str], dict[str, Any]]:
    def coerce(value: str) -> dict[str, Any]:
        return {key: value.strip()}

    return coerce


def _stripped(value: str) -> str:
    return value.strip()


def _period(value: str) -> dict[str, Any]:
    return {"start": _temporal(_DATETIME_RE, "dateTime")(value)}


_COERCERS: Final[Mapping[str, Callable[[str], Any]]] = {
    "Address": _text("text"),
    "Annotation": _text("text"),
    "Attachment": _text("title"),
    "Code": _stripped,
    "CodeableConcept": _text("text"),
    "Coding": _text("display"),
    "ContactPoint": _text("value"),
    "Date": _temporal(_DATE_RE, "date"),
    "DateTime": _temporal(_DATETIME_RE, "dateTime"),
    "Dosage": _text("text"),
    "HumanName": _human_name,
    "Identifier": _text("value"),
    "Instant": _temporal(_INSTANT_RE, "instant (needs full precision and a timezone)"),
    "Integer": _integer,
    "Period": _period,
    "PositiveInt": _integer,
    "Quantity": _quantity,
    "String": _stripped,
    "UnsignedInt": _integer,
    "bool": _boolean,
}
"""Every coercible FHIR datatype reachable from the extraction catalog.

Complete rather than representative: the catalog's elements resolve to these plus
``Reference`` (wired separately) and a tail of backbone elements and ``Extension``,
which have no single-string representation and are reported as dropped.
"""


def resolve_datatype(resource_type: str, element: str) -> tuple[str, bool]:
    """Return the ``(datatype, is_list)`` the typed FHIR model declares for an element.

    ``("unknown", False)`` means the R4B models cannot describe the element, which
    the caller must treat as not coercible rather than as a free-form string.
    """
    model = typed_model_for(resource_type)
    if model is None:
        return "unknown", False
    fields = {
        str(field.alias or name): field
        for name, field in model.model_fields.items()
        if not name.endswith("__ext")
    }
    field = fields.get(element)
    return _unwrap(field.annotation) if field is not None else ("unknown", False)


def _unwrap(annotation: Any) -> tuple[str, bool]:
    origin = get_origin(annotation)
    args = tuple(arg for arg in get_args(annotation) if arg is not type(None))

    if origin in (Union, types.UnionType):
        for arg in args:
            name, is_list = _unwrap(arg)
            if name != "unknown":
                return name, is_list
        return "unknown", False
    if origin is list:
        name, _ = _unwrap(args[0]) if args else ("unknown", False)
        return name, True
    # fhir.resources spells primitives Annotated[str, Code()], Annotated[date, Date()]:
    # the metadata class names the FHIR datatype, the base type is only its Python form.
    if hasattr(annotation, "__metadata__"):
        metadata = annotation.__metadata__
        base, _ = _unwrap(get_args(annotation)[0])
        return (type(metadata[0]).__name__ if metadata else base), False
    if isinstance(annotation, type):
        return annotation.__name__.removesuffix("Type"), False
    return "unknown", False


def assemble_bundle(entities: Sequence[Mapping[str, str]], *, seed: str) -> AssembledBundle:
    """Build a collection Bundle from validated, instance-keyed entities.

    ``entities`` must already have passed
    :func:`fhirbridge.llm.nar2fhir.parse_entities`; nothing here re-checks that a
    resource type or element is in the catalog.

    ``seed`` scopes the ``uuid5`` entry identifiers, normally the conversion id.
    Reusing a seed reproduces a Bundle exactly; distinct seeds keep two conversions
    from colliding on identifiers when their instance slugs happen to match.
    """
    groups: dict[tuple[str, str], list[Mapping[str, str]]] = {}
    for entity in entities:
        groups.setdefault((entity["resourceType"], entity["instance"]), []).append(entity)

    keys = sorted(groups, key=lambda key: (_type_rank(key[0]), key[1]))
    full_urls = {
        key: f"urn:uuid:{uuid.uuid5(_ENTRY_NAMESPACE, f'{seed}:{key[0]}/{key[1]}')}" for key in keys
    }

    counts: dict[str, int] = {}
    for resource_type, _ in keys:
        counts[resource_type] = counts.get(resource_type, 0) + 1
    # A reference is only safe to wire when its target type has exactly one instance.
    singletons = {key[0]: full_urls[key] for key in keys if counts[key[0]] == 1}

    notes: list[AssemblyNote] = []
    entries: list[dict[str, Any]] = []
    for index, key in enumerate(keys):
        resource = _assemble_resource(
            key[0], groups[key], singletons=singletons, index=index, notes=notes
        )
        entries.append({"fullUrl": full_urls[key], "resource": resource})

    inferred = {note.entry_index for note in notes if note.action is AssemblyAction.INFERRED}
    for index, entry in enumerate(entries):
        tags = [tag(AI_DERIVED, "Proposed by a language model")]
        if index in inferred:
            tags.append(tag(MACHINE_INFERRED, "A required element was not grounded in the source"))
        entry["resource"]["meta"] = {"tag": tags}

    bundle = {"resourceType": "Bundle", "type": "collection", "entry": entries}
    return AssembledBundle(bundle=bundle, notes=tuple(notes))


def _assemble_resource(
    resource_type: str,
    group: Iterable[Mapping[str, str]],
    *,
    singletons: Mapping[str, str],
    index: int,
    notes: list[AssemblyNote],
) -> dict[str, Any]:
    resource: dict[str, Any] = {"resourceType": resource_type}

    def note(element: str, action: AssemblyAction, detail: str) -> None:
        notes.append(AssemblyNote(index, resource_type, element, action, detail))

    for entity in group:
        element, value = entity["keyword"], entity["value"]
        datatype, is_list = resolve_datatype(resource_type, element)

        if datatype == "Reference":
            target = _REFERENCE_TARGETS.get(element)
            full_url = singletons.get(target) if target else None
            if full_url is None:
                placed: Any = {"display": value.strip()}
                note(
                    element,
                    AssemblyAction.UNRESOLVED,
                    f"no unambiguous {target or 'target'} instance; kept as display text",
                )
            else:
                placed = {"reference": full_url}
        elif datatype not in _COERCERS:
            note(element, AssemblyAction.DROPPED, f"{datatype} has no single-string form")
            continue
        else:
            try:
                placed = _COERCERS[datatype](value)
            except CoercionError as exc:
                note(element, AssemblyAction.DROPPED, f"{exc} ({datatype})")
                continue

        if is_list:
            resource.setdefault(element, []).append(placed)
        elif element in resource:
            note(element, AssemblyAction.CONFLICT, "a second value was extracted for this element")
        else:
            resource[element] = placed

    subject_key = _SUBJECT_KEYS.get(resource_type)
    if subject_key is not None and subject_key not in resource:
        patient_url = singletons.get("Patient")
        if patient_url is None:
            note(
                subject_key,
                AssemblyAction.UNRESOLVED,
                "required at 1..1 but no unambiguous Patient exists",
            )
        else:
            # Structural inference: there is exactly one subject in the document, so
            # this cannot be wrong given the input. Reported, but not INFERRED, which
            # is reserved for values that were genuinely fabricated.
            resource[subject_key] = {"reference": patient_url}
            note(subject_key, AssemblyAction.WIRED, "pointed at the only Patient (required 1..1)")

    for element, default in _REQUIRED_DEFAULTS.get(resource_type, {}).items():
        if element not in resource:
            resource[element] = default
            note(element, AssemblyAction.INFERRED, "required by FHIR and not stated in the source")

    return resource


def _type_rank(resource_type: str) -> tuple[int, str]:
    if resource_type in _TYPE_ORDER:
        return _TYPE_ORDER.index(resource_type), resource_type
    return len(_TYPE_ORDER), resource_type


__all__ = [
    "AssembledBundle",
    "AssemblyAction",
    "AssemblyNote",
    "CoercionError",
    "assemble_bundle",
    "resolve_datatype",
]
