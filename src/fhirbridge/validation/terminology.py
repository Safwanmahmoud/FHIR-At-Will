"""L3 terminology validation (AGENTS.md 10).

L3 is the enforcement point for principle 2.3: *codes never come from model
weights*. Every ``Coding`` in the resource is re-confirmed with ``$validate-code``
against the terminology server **this deployment** trusts, independently of
whichever internal server the validator sidecar uses. Where a binding is known,
ValueSet membership is checked too, and a `required` binding that cannot be
confirmed is a blocking error.

Three outcomes must stay distinct, because collapsing them is how unvalidated
codes get through:

``valid``
    The server confirmed the code.
``invalid``
    The server denied the code. Blocking for required bindings.
``unanswerable``
    The server does not know that CodeSystem or ValueSet. This is *not* a pass.
    It is an error for a required binding and a warning otherwise, and it is
    always recorded in the layer notes.

A server outage is different again: it raises
:class:`~fhirbridge.domain.errors.TerminologyUnavailableError` so the request
fails closed with ``503`` (principle 2.4).
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import yaml

from fhirbridge.domain.errors import DomainError, ErrorCode
from fhirbridge.terminology.interface import TerminologyClient
from fhirbridge.terminology.models import BindingStrength
from fhirbridge.validation.element_walk import (
    CodedElement,
    CodedElementKind,
    iter_coded_elements,
)
from fhirbridge.validation.models import (
    IssueSeverity,
    LayerResult,
    LayerStatus,
    ValidationIssue,
    ValidationLayer,
)

_LAYER = ValidationLayer.TERMINOLOGY
_RULES_PATH: Final = Path(__file__).parent / "rules" / "bindings.yaml"
DEFAULT_MAX_CHECKS: Final[int] = 250


@dataclass(frozen=True, slots=True)
class Binding:
    """One curated element binding."""

    path: str
    value_set: str
    strength: BindingStrength
    kind: str


@dataclass(frozen=True, slots=True)
class BindingRegistry:
    """The curated binding subset, plus the systems treated as units."""

    bindings: dict[str, Binding]
    unit_systems: frozenset[str]

    def get(self, path: str) -> Binding | None:
        return self.bindings.get(path)


@lru_cache(maxsize=1)
def load_bindings(path: str | None = None) -> BindingRegistry:
    """Load and cache ``rules/bindings.yaml``."""
    source = Path(path) if path else _RULES_PATH
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    bindings: dict[str, Binding] = {}
    for entry in raw.get("bindings", []) or []:
        binding = Binding(
            path=str(entry["path"]),
            value_set=str(entry["value_set"]),
            strength=BindingStrength(str(entry.get("strength", "example"))),
            kind=str(entry.get("kind", "codeable_concept")),
        )
        bindings[binding.path] = binding
    unit_systems = frozenset(str(item) for item in raw.get("unit_systems", []) or [])
    return BindingRegistry(bindings=bindings, unit_systems=unit_systems)


@dataclass
class _Accumulator:
    issues: list[ValidationIssue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    checked: int = 0
    deferred_paths: set[str] = field(default_factory=set)
    unanswerable: int = 0
    invalid: int = 0
    text_only: int = 0


async def validate_terminology(
    typed_resource: Any,
    *,
    client: TerminologyClient,
    max_checks: int = DEFAULT_MAX_CHECKS,
    bindings: BindingRegistry | None = None,
) -> LayerResult:
    """Validate every coded element in ``typed_resource``.

    ``typed_resource`` is the typed model produced by L1. L3 does not run when
    L1 could not produce one — see :func:`skipped`.
    """
    started = time.perf_counter()
    registry = bindings or load_bindings()
    acc = _Accumulator()

    elements = list(iter_coded_elements(typed_resource))
    for element in elements:
        if acc.checked >= max_checks:
            acc.notes.append(
                f"Stopped after {max_checks} terminology checks; "
                f"{len(elements) - acc.checked} coded element(s) were not checked."
            )
            break
        await _check_element(element, client=client, registry=registry, acc=acc)

    _summarize(acc, registry, elements)

    blocking = [issue for issue in acc.issues if issue.severity.is_blocking]
    return LayerResult(
        layer=_LAYER,
        layer_number=_LAYER.number,
        status=LayerStatus.FAILED if blocking else LayerStatus.PASSED,
        blocking=True,
        issues=acc.issues,
        duration_ms=int((time.perf_counter() - started) * 1000),
        notes=acc.notes,
    )


def skipped(reason: str) -> LayerResult:
    """A skipped L3 result. Blocking stays true: a skip is never a pass."""
    return LayerResult(
        layer=_LAYER,
        layer_number=_LAYER.number,
        status=LayerStatus.SKIPPED,
        blocking=True,
        skipped_reason=reason,
    )


async def _check_element(
    element: CodedElement,
    *,
    client: TerminologyClient,
    registry: BindingRegistry,
    acc: _Accumulator,
) -> None:
    coding = element.coding
    if not coding.code:
        return

    binding = registry.get(element.binding_path)

    if element.kind is CodedElementKind.QUANTITY_UNIT:
        await _check_unit(element, client=client, registry=registry, acc=acc)
        return

    if element.kind is CodedElementKind.PRIMITIVE_CODE:
        if binding is None:
            acc.deferred_paths.add(element.binding_path)
            return
        await _validate(
            element,
            client=client,
            acc=acc,
            system=None,
            value_set=binding.value_set,
            strength=binding.strength,
        )
        return

    # A Coding. Confirm the concept exists in its own CodeSystem first.
    if not coding.system:
        acc.issues.append(
            _issue(
                IssueSeverity.ERROR,
                "code-invalid",
                "A Coding has a code but no system, so the code cannot be confirmed "
                "against any CodeSystem. Emit CodeableConcept.text instead.",
                element.location,
            )
        )
        return

    await _validate(
        element,
        client=client,
        acc=acc,
        system=coding.system,
        value_set=None,
        strength=BindingStrength.REQUIRED,
    )

    if binding is not None and binding.strength in (
        BindingStrength.REQUIRED,
        BindingStrength.EXTENSIBLE,
    ):
        await _validate(
            element,
            client=client,
            acc=acc,
            system=coding.system,
            value_set=binding.value_set,
            strength=binding.strength,
        )
    elif binding is None:
        acc.deferred_paths.add(element.binding_path)


async def _check_unit(
    element: CodedElement,
    *,
    client: TerminologyClient,
    registry: BindingRegistry,
    acc: _Accumulator,
) -> None:
    system = element.coding.system
    if system is None:
        acc.issues.append(
            _issue(
                IssueSeverity.WARNING,
                "code-invalid",
                "A Quantity has a unit code but no system, so the unit cannot be "
                "confirmed. UCUM units should declare system "
                "'http://unitsofmeasure.org'.",
                element.location,
            )
        )
        return
    if system not in registry.unit_systems:
        acc.deferred_paths.add(element.binding_path)
        return
    await _validate(
        element,
        client=client,
        acc=acc,
        system=system,
        value_set=None,
        strength=BindingStrength.PREFERRED,
        subject="unit",
    )


async def _validate(
    element: CodedElement,
    *,
    client: TerminologyClient,
    acc: _Accumulator,
    system: str | None,
    value_set: str | None,
    strength: BindingStrength,
    subject: str = "code",
) -> None:
    """One ``$validate-code`` call, mapped onto the three distinct outcomes."""
    acc.checked += 1
    code = element.coding.code
    assert code is not None

    try:
        outcome = await client.validate_code(
            system=system,
            code=code,
            display=element.coding.display,
            version=element.coding.version,
            value_set=value_set,
        )
    except DomainError as exc:
        if exc.code is not ErrorCode.UNKNOWN_VALUE_SET:
            raise
        acc.unanswerable += 1
        severity = IssueSeverity.ERROR if strength.is_blocking else IssueSeverity.WARNING
        target = f"ValueSet {value_set}" if value_set else f"CodeSystem {system}"
        acc.issues.append(
            _issue(
                severity,
                "not-found",
                f"The terminology server does not know {target}, so this "
                f"{strength} binding could not be confirmed. This is not a pass: load "
                "the code system into your terminology server "
                "(see docs/terminology-setup.md).",
                element.location,
                machine_code=str(ErrorCode.UNKNOWN_VALUE_SET),
            )
        )
        return

    if outcome.result:
        if value_set is None and outcome.display and element.coding.display:
            _check_display(element, outcome.display, acc)
        return

    acc.invalid += 1
    if value_set is not None:
        severity = IssueSeverity.ERROR if strength.is_blocking else IssueSeverity.WARNING
        detail = outcome.message or "the code is not a member of the bound ValueSet"
        acc.issues.append(
            _issue(
                severity,
                "code-invalid",
                f"Code '{code}' does not satisfy the {strength} binding to {value_set}: {detail}",
                element.location,
            )
        )
        return

    detail = outcome.message or "the terminology server does not recognize this code"
    acc.issues.append(
        _issue(
            IssueSeverity.ERROR,
            "code-invalid",
            f"{subject.capitalize()} '{system}|{code}' is not valid: {detail}. "
            "A Coding may only be emitted when the terminology server confirms it; "
            "otherwise use CodeableConcept.text only.",
            element.location,
        )
    )


def _check_display(element: CodedElement, server_display: str, acc: _Accumulator) -> None:
    """Warn when a display disagrees with the terminology server's own display.

    A wrong display is how a reviewer ends up confirming a code they did not
    actually read, so it is worth surfacing even though FHIR permits it.
    """
    supplied = (element.coding.display or "").strip().casefold()
    canonical = server_display.strip().casefold()
    if supplied and canonical and supplied != canonical:
        acc.issues.append(
            _issue(
                IssueSeverity.WARNING,
                "value",
                f"Display '{element.coding.display}' differs from the terminology "
                f"server's display '{server_display}' for this code. A reviewer reading "
                "the display may not be seeing what the code means.",
                f"{element.location}.display",
            )
        )


def _summarize(
    acc: _Accumulator, registry: BindingRegistry, elements: Sequence[CodedElement]
) -> None:
    acc.notes.insert(
        0,
        f"Checked {acc.checked} terminology call(s) across {len(elements)} coded "
        f"element(s); {len(registry.bindings)} binding path(s) are known to L3.",
    )
    if acc.deferred_paths:
        preview = ", ".join(sorted(acc.deferred_paths)[:10])
        more = len(acc.deferred_paths) - 10
        suffix = f" (+{more} more)" if more > 0 else ""
        acc.notes.append(
            f"{len(acc.deferred_paths)} element path(s) have no binding known to L3 and "
            f"were deferred to L2, which has the full StructureDefinition set: "
            f"{preview}{suffix}."
        )
    if acc.unanswerable:
        acc.notes.append(
            f"{acc.unanswerable} check(s) could not be answered by the terminology "
            "server. These are reported as issues, not as passes."
        )


def _issue(
    severity: IssueSeverity,
    code: str,
    message: str,
    expression: str | None,
    machine_code: str | None = None,
) -> ValidationIssue:
    return ValidationIssue(
        layer=_LAYER,
        severity=severity,
        code=code,
        message=message,
        expression=expression,
        machine_code=machine_code,
    )


__all__ = [
    "DEFAULT_MAX_CHECKS",
    "Binding",
    "BindingRegistry",
    "load_bindings",
    "skipped",
    "validate_terminology",
]
