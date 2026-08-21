"""L1 structural validation: typed round-trip (AGENTS.md 10).

L1 answers one question: is this payload a well-formed FHIR R4 resource? It
does so with the ``fhir.resources`` typed models, which reject unknown elements,
enforce primitive formats and cardinality, and resolve ``Bundle.entry.resource``
to concrete types.

Two limits are reported honestly rather than papered over:

* ``fhir.resources`` 8.x ships R4B (4.3.0) models, not R4 (4.0.1). L1 therefore
  gates resource types on the authoritative R4 list first, so an R4B-only type
  such as ``Citation`` is rejected rather than silently accepted as valid R4.
* Eighteen R4 types (the ``MedicinalProduct*`` and ``Substance*`` families,
  ``RiskEvidenceSynthesis``, ``EffectEvidenceSynthesis``) have no R4B model.
  Those are reported as "not type-checked at L1" and deferred to L2, never as
  passing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from fhirbridge.fhir.resource_types import (
    R4_RESOURCE_TYPES,
    is_instantiable,
    typed_model_for,
)
from fhirbridge.validation.models import (
    IssueSeverity,
    LayerResult,
    LayerStatus,
    ValidationIssue,
    ValidationLayer,
)

_LAYER = ValidationLayer.STRUCTURAL
_MAX_ISSUES = 200


@dataclass(slots=True)
class StructuralResult:
    """L1 output. ``typed`` is None when parsing failed or no model exists."""

    result: LayerResult
    typed: Any | None
    resource_type: str | None
    resource_count: int
    type_checked: bool


def _issue(
    severity: IssueSeverity,
    code: str,
    message: str,
    expression: str | None = None,
) -> ValidationIssue:
    return ValidationIssue(
        layer=_LAYER, severity=severity, code=code, message=message, expression=expression
    )


def validate_structure(payload: object) -> StructuralResult:
    """Parse ``payload`` into a typed FHIR resource, reporting every problem."""
    started = time.perf_counter()
    issues: list[ValidationIssue] = []
    notes: list[str] = []
    typed: Any | None = None
    type_checked = False
    resource_count = 0

    if not isinstance(payload, dict):
        issues.append(
            _issue(
                IssueSeverity.FATAL,
                "structure",
                "The payload must be a JSON object representing a single FHIR resource.",
            )
        )
        return _finish(started, issues, notes, typed, None, 0, type_checked)

    raw_type = payload.get("resourceType")
    if not isinstance(raw_type, str) or not raw_type:
        issues.append(
            _issue(
                IssueSeverity.FATAL,
                "structure",
                "The payload has no 'resourceType' element, so it is not a FHIR resource.",
                expression="$this.resourceType",
            )
        )
        return _finish(started, issues, notes, typed, None, 0, type_checked)

    resource_type = raw_type

    if resource_type not in R4_RESOURCE_TYPES:
        issues.append(
            _issue(
                IssueSeverity.FATAL,
                "not-supported",
                f"'{resource_type}' is not a resource type in FHIR R4 (4.0.1). "
                "Resource types introduced after 4.0.1 are rejected.",
                expression="$this.resourceType",
            )
        )
        return _finish(started, issues, notes, typed, resource_type, 0, type_checked)

    if not is_instantiable(resource_type):
        issues.append(
            _issue(
                IssueSeverity.FATAL,
                "not-supported",
                f"'{resource_type}' is an abstract type and cannot be instantiated.",
                expression="$this.resourceType",
            )
        )
        return _finish(started, issues, notes, typed, resource_type, 0, type_checked)

    model = typed_model_for(resource_type)
    if model is None:
        notes.append(
            f"{resource_type} has no typed model available in this build, so L1 did not "
            "type-check it. Conformance for this resource rests entirely on L2."
        )
        issues.append(
            _issue(
                IssueSeverity.WARNING,
                "incomplete",
                f"L1 could not type-check '{resource_type}': no typed model is available "
                "for this R4 resource type in this build. Deferred to L2.",
                expression="$this.resourceType",
            )
        )
        resource_count = _count_resources(payload, resource_type)
        return _finish(started, issues, notes, typed, resource_type, resource_count, type_checked)

    try:
        typed = model.model_validate(payload)
        type_checked = True
    except ValidationError as exc:
        issues.extend(_translate_pydantic_errors(exc, resource_type))
    except ValueError as exc:
        issues.append(_issue(IssueSeverity.FATAL, "structure", str(exc)))

    resource_count = _count_resources(payload, resource_type)
    if resource_type == "Bundle" and resource_count == 0:
        issues.append(
            _issue(
                IssueSeverity.WARNING,
                "informational",
                "The Bundle has no entries, so there is nothing to validate.",
                expression="Bundle.entry",
            )
        )

    return _finish(started, issues, notes, typed, resource_type, resource_count, type_checked)


def _count_resources(payload: dict[str, Any], resource_type: str) -> int:
    if resource_type != "Bundle":
        return 1
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return 0
    return sum(
        1
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("resource"), dict)
    )


def _translate_pydantic_errors(exc: ValidationError, resource_type: str) -> list[ValidationIssue]:
    """Turn pydantic errors into FHIR-shaped issues with FHIRPath expressions."""
    issues: list[ValidationIssue] = []
    for error in exc.errors()[:_MAX_ISSUES]:
        expression = _fhirpath_from_loc(error["loc"], resource_type)
        code = _issue_code_for(str(error["type"]))
        issues.append(_issue(IssueSeverity.ERROR, code, str(error["msg"]), expression=expression))
    remaining = len(exc.errors()) - len(issues)
    if remaining > 0:
        issues.append(
            _issue(
                IssueSeverity.INFORMATION,
                "informational",
                f"{remaining} further structural error(s) were truncated from this report.",
            )
        )
    return issues


def _issue_code_for(pydantic_type: str) -> str:
    if "missing" in pydantic_type:
        return "required"
    if "extra_forbidden" in pydantic_type:
        return "structure"
    if "type" in pydantic_type or "parsing" in pydantic_type:
        return "structure"
    return "value"


def _fhirpath_from_loc(loc: tuple[int | str, ...], resource_type: str) -> str:
    """Render a pydantic error location as a FHIRPath-style expression."""
    parts: list[str] = [resource_type]
    for item in loc:
        if isinstance(item, int):
            if parts:
                parts[-1] = f"{parts[-1]}[{item}]"
        else:
            parts.append(str(item))
    return ".".join(parts)


def _finish(
    started: float,
    issues: list[ValidationIssue],
    notes: list[str],
    typed: Any | None,
    resource_type: str | None,
    resource_count: int,
    type_checked: bool,
) -> StructuralResult:
    blocking_errors = [issue for issue in issues if issue.severity.is_blocking]
    result = LayerResult(
        layer=_LAYER,
        layer_number=_LAYER.number,
        status=LayerStatus.FAILED if blocking_errors else LayerStatus.PASSED,
        blocking=True,
        issues=issues,
        duration_ms=int((time.perf_counter() - started) * 1000),
        notes=notes,
    )
    return StructuralResult(
        result=result,
        typed=typed,
        resource_type=resource_type,
        resource_count=resource_count,
        type_checked=type_checked,
    )


__all__ = ["StructuralResult", "validate_structure"]
