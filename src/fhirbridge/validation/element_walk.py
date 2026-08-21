"""Walking a typed FHIR resource to find every coded element.

L3 must check *every* ``Coding`` in a resource. Doing that by pattern-matching
the serialized JSON is unsafe: ``Quantity`` also carries ``system`` and ``code``
but its ``code`` is a UCUM unit, not a terminology concept, and treating the two
alike would produce nonsense terminology errors and hide real ones.

So the walk operates on the *typed* model produced by L1, where ``Coding``,
``CodeableConcept`` and ``Quantity`` are distinct classes and ``code``-typed
primitives are identifiable from their annotation metadata.

Each hit carries two paths:

``location``
    Indexed, for reporting: ``Bundle.entry[4].resource.category[0].coding[0]``.
``definition_path``
    Unindexed and rooted at the containing resource, for binding lookup:
    ``Observation.category``. Entering ``Bundle.entry.resource`` restarts this
    path at the nested resource's own type.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Final, get_args, get_origin

from fhir_core.fhirabstractmodel import FHIRAbstractModel

from fhirbridge.terminology.models import Coding

_MAX_DEPTH: Final[int] = 64
"""Bounded so a pathological payload cannot exhaust the stack."""


class CodedElementKind(StrEnum):
    CODING = "coding"
    """A ``Coding``, either standalone or inside a ``CodeableConcept``."""

    PRIMITIVE_CODE = "primitive_code"
    """A ``code``-typed primitive such as ``Observation.status``."""

    QUANTITY_UNIT = "quantity_unit"
    """``Quantity.code``, which is a unit (usually UCUM), not a concept."""


@dataclass(frozen=True, slots=True)
class CodedElement:
    """One coded element found in a resource."""

    kind: CodedElementKind
    location: str
    definition_path: str
    resource_type: str
    coding: Coding
    binding_path: str
    """The path a binding is declared on.

    For a ``Coding`` inside a ``CodeableConcept`` this is the CodeableConcept's
    path, because that is where FHIR declares the binding.
    """

    text: str | None = None
    """``CodeableConcept.text``, when the coding came from one."""


def _element_name(model: type[FHIRAbstractModel], field_name: str) -> str:
    """Map a Python field name back to its FHIR element name."""
    field = model.model_fields.get(field_name)
    if field is not None and field.alias:
        return field.alias
    return field_name


def _is_code_primitive(annotation: Any) -> bool:
    """True when a field's annotation marks it as the FHIR ``code`` primitive."""
    for candidate in _flatten_annotation(annotation):
        if get_origin(candidate) is Annotated:
            for meta in get_args(candidate)[1:]:
                if type(meta).__name__ == "Code":
                    return True
    return False


def _flatten_annotation(annotation: Any, depth: int = 0) -> Iterator[Any]:
    """Yield an annotation and everything nested inside unions/optionals/lists."""
    if depth > 6:
        return
    yield annotation
    for arg in get_args(annotation):
        if arg is type(None):
            continue
        yield from _flatten_annotation(arg, depth + 1)


def iter_coded_elements(
    resource: FHIRAbstractModel,
    *,
    location_root: str | None = None,
) -> Iterator[CodedElement]:
    """Yield every coded element in ``resource``, depth-first."""
    root_type = resource.get_resource_type()
    yield from _walk(
        node=resource,
        location=location_root or root_type,
        definition_path=root_type,
        resource_type=root_type,
        depth=0,
    )


def _walk(
    *,
    node: Any,
    location: str,
    definition_path: str,
    resource_type: str,
    depth: int,
) -> Iterator[CodedElement]:
    if depth > _MAX_DEPTH or not isinstance(node, FHIRAbstractModel):
        return

    node_type = type(node)
    for field_name in node_type.model_fields:
        value = getattr(node, field_name, None)
        if value is None or field_name == "resourceType":
            continue

        element = _element_name(node_type, field_name)
        child_location = f"{location}.{element}"
        child_definition = f"{definition_path}.{element}"
        annotation = node_type.model_fields[field_name].annotation

        if isinstance(value, list):
            for index, item in enumerate(value):
                yield from _dispatch(
                    value=item,
                    location=f"{child_location}[{index}]",
                    definition_path=child_definition,
                    resource_type=resource_type,
                    annotation=annotation,
                    depth=depth,
                )
        else:
            yield from _dispatch(
                value=value,
                location=child_location,
                definition_path=child_definition,
                resource_type=resource_type,
                annotation=annotation,
                depth=depth,
            )


def _dispatch(
    *,
    value: Any,
    location: str,
    definition_path: str,
    resource_type: str,
    annotation: Any,
    depth: int,
) -> Iterator[CodedElement]:
    if isinstance(value, FHIRAbstractModel):
        fhir_type = value.get_resource_type()
        # fhir.resources datatypes are dynamically generated, so their element
        # attributes are invisible to the type checker. Narrowing by FHIR type
        # name above is the actual guarantee.
        element: Any = value

        if fhir_type == "Coding":
            yield _coding_element(
                element,
                kind=CodedElementKind.CODING,
                location=location,
                definition_path=definition_path,
                binding_path=definition_path,
                resource_type=resource_type,
            )
            return

        if fhir_type == "CodeableConcept":
            for index, coding in enumerate(element.coding or []):
                yield _coding_element(
                    coding,
                    kind=CodedElementKind.CODING,
                    location=f"{location}.coding[{index}]",
                    definition_path=f"{definition_path}.coding",
                    binding_path=definition_path,
                    resource_type=resource_type,
                    text=element.text,
                )
            return

        if fhir_type == "Quantity":
            if element.code:
                yield CodedElement(
                    kind=CodedElementKind.QUANTITY_UNIT,
                    location=f"{location}.code",
                    definition_path=f"{definition_path}.code",
                    resource_type=resource_type,
                    binding_path=f"{definition_path}.code",
                    coding=Coding(system=element.system, code=element.code, display=element.unit),
                )
            return

        # A nested resource (Bundle.entry.resource, contained, Provenance target)
        # restarts the definition path at its own type.
        if _is_resource(value):
            yield from _walk(
                node=value,
                location=location,
                definition_path=fhir_type,
                resource_type=fhir_type,
                depth=depth + 1,
            )
            return

        yield from _walk(
            node=value,
            location=location,
            definition_path=definition_path,
            resource_type=resource_type,
            depth=depth + 1,
        )
        return

    if isinstance(value, str) and _is_code_primitive(annotation):
        yield CodedElement(
            kind=CodedElementKind.PRIMITIVE_CODE,
            location=location,
            definition_path=definition_path,
            resource_type=resource_type,
            binding_path=definition_path,
            coding=Coding(system=None, code=value),
        )


def _is_resource(model: FHIRAbstractModel) -> bool:
    """True when the model is a FHIR resource rather than a datatype."""
    return any(base.__name__ == "Resource" for base in type(model).__mro__)


def _coding_element(
    coding: Any,
    *,
    kind: CodedElementKind,
    location: str,
    definition_path: str,
    binding_path: str,
    resource_type: str,
    text: str | None = None,
) -> CodedElement:
    return CodedElement(
        kind=kind,
        location=location,
        definition_path=definition_path,
        resource_type=resource_type,
        binding_path=binding_path,
        text=text,
        coding=Coding(
            system=getattr(coding, "system", None),
            code=getattr(coding, "code", None),
            display=getattr(coding, "display", None),
            version=getattr(coding, "version", None),
        ),
    )


__all__ = ["CodedElement", "CodedElementKind", "iter_coded_elements"]
