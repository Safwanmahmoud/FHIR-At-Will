"""L4 invariant checking via the sidecar's FHIRPath engine (AGENTS.md 10).

Each invariant is evaluated with the resource as the FHIRPath context. For a
Bundle, ``Bundle``-scoped invariants run against the Bundle and every other
invariant runs against each entry resource in turn.

An invariant that the FHIRPath host cannot evaluate is reported as
*inconclusive*, never as passing. Rules known to use host-specific constructs
(``%resource``) are marked ``tolerate_evaluation_failure`` in the pack so an
unsupported construct downgrades to an informational note instead of an error.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import yaml

from fhirbridge.domain.errors import ValidatorUnavailableError
from fhirbridge.fhir.validator_client import ValidatorClient
from fhirbridge.validation.models import (
    IssueSeverity,
    LayerResult,
    LayerStatus,
    ValidationIssue,
    ValidationLayer,
)

_LAYER = ValidationLayer.INVARIANTS
_RULES_PATH: Final = Path(__file__).parent / "rules" / "invariants.yaml"
_WILDCARD: Final = "*"


@dataclass(frozen=True, slots=True)
class Invariant:
    """One invariant from the pack."""

    id: str
    expression: str
    severity: IssueSeverity
    human: str
    applies_to: frozenset[str]
    source: str = "fhirbridge"
    tolerate_evaluation_failure: bool = False

    def applies(self, resource_type: str) -> bool:
        return _WILDCARD in self.applies_to or resource_type in self.applies_to


@dataclass(frozen=True, slots=True)
class InvariantPack:
    invariants: tuple[Invariant, ...]
    max_evaluations: int

    def for_resource(self, resource_type: str) -> tuple[Invariant, ...]:
        return tuple(item for item in self.invariants if item.applies(resource_type))


@lru_cache(maxsize=1)
def load_invariants(path: str | None = None) -> InvariantPack:
    """Load and cache ``rules/invariants.yaml``."""
    source = Path(path) if path else _RULES_PATH
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    invariants = tuple(
        Invariant(
            id=str(entry["id"]),
            expression=str(entry["expression"]).strip(),
            severity=IssueSeverity(str(entry.get("severity", "error"))),
            human=str(entry.get("human", "")).strip(),
            applies_to=frozenset(str(item) for item in entry.get("applies_to", [_WILDCARD])),
            source=str(entry.get("source", "fhirbridge")),
            tolerate_evaluation_failure=bool(entry.get("tolerate_evaluation_failure", False)),
        )
        for entry in raw.get("invariants", []) or []
    )
    return InvariantPack(
        invariants=invariants, max_evaluations=int(raw.get("max_evaluations", 200))
    )


@dataclass
class _Targets:
    """The (resource, location) pairs an invariant pack is evaluated against."""

    items: list[tuple[str, dict[str, Any], str]] = field(default_factory=list)


def _collect_targets(payload: dict[str, Any], resource_type: str) -> _Targets:
    targets = _Targets()
    targets.items.append((resource_type, payload, resource_type))
    if resource_type != "Bundle":
        return targets
    for index, entry in enumerate(payload.get("entry", []) or []):
        if not isinstance(entry, dict):
            continue
        resource = entry.get("resource")
        if not isinstance(resource, dict):
            continue
        nested_type = resource.get("resourceType")
        if isinstance(nested_type, str):
            targets.items.append((nested_type, resource, f"Bundle.entry[{index}].resource"))
    return targets


async def validate_invariants(
    payload: dict[str, Any],
    *,
    client: ValidatorClient,
    resource_type: str,
    pack: InvariantPack | None = None,
) -> LayerResult:
    """Evaluate the invariant pack against ``payload``."""
    started = time.perf_counter()
    rules = pack or load_invariants()
    issues: list[ValidationIssue] = []
    notes: list[str] = []
    evaluated = 0
    inconclusive = 0

    targets = _collect_targets(payload, resource_type)
    budget_exhausted = False

    for target_type, resource, location in targets.items:
        for invariant in rules.for_resource(target_type):
            if evaluated >= rules.max_evaluations:
                budget_exhausted = True
                break
            evaluated += 1
            try:
                outcome = await client.evaluate_fhirpath(resource, invariant.expression)
            except ValidatorUnavailableError:
                # A dead sidecar is a fail-closed condition, not an inconclusive rule.
                raise
            except ValueError:
                inconclusive += 1
                if not invariant.tolerate_evaluation_failure:
                    issues.append(
                        _inconclusive_issue(
                            invariant,
                            location,
                            "the FHIRPath host would not evaluate the expression",
                        )
                    )
                continue

            if outcome.is_true:
                continue

            if not outcome.values:
                inconclusive += 1
                if not invariant.tolerate_evaluation_failure:
                    issues.append(
                        _inconclusive_issue(
                            invariant,
                            location,
                            "the FHIRPath host returned an empty result, so the "
                            "invariant was not demonstrated to hold",
                        )
                    )
                continue

            issues.append(
                ValidationIssue(
                    layer=_LAYER,
                    severity=invariant.severity,
                    code="invariant",
                    rule_id=invariant.id,
                    message=(
                        f"{invariant.id} failed: {invariant.human} "
                        f"[{invariant.source}; FHIRPath: {invariant.expression}]"
                    ),
                    expression=location,
                )
            )
        if budget_exhausted:
            break

    notes.append(
        f"Evaluated {evaluated} invariant(s) across {len(targets.items)} resource(s) "
        f"from a pack of {len(rules.invariants)}."
    )
    if budget_exhausted:
        notes.append(
            f"Stopped at the {rules.max_evaluations}-evaluation budget; some invariants "
            "were not evaluated and are not claimed to pass."
        )
    if inconclusive:
        notes.append(f"{inconclusive} invariant(s) were inconclusive on this FHIRPath host.")

    blocking = [issue for issue in issues if issue.severity.is_blocking]
    return LayerResult(
        layer=_LAYER,
        layer_number=_LAYER.number,
        status=LayerStatus.FAILED if blocking else LayerStatus.PASSED,
        blocking=True,
        issues=issues,
        duration_ms=int((time.perf_counter() - started) * 1000),
        notes=notes,
    )


def skipped(reason: str) -> LayerResult:
    return LayerResult(
        layer=_LAYER,
        layer_number=_LAYER.number,
        status=LayerStatus.SKIPPED,
        blocking=True,
        skipped_reason=reason,
    )


def _inconclusive_issue(invariant: Invariant, location: str, why: str) -> ValidationIssue:
    return ValidationIssue(
        layer=_LAYER,
        severity=IssueSeverity.WARNING,
        code="incomplete",
        rule_id=invariant.id,
        message=(
            f"{invariant.id} was inconclusive: {why}. The invariant is not reported as "
            f"passing. [{invariant.source}]"
        ),
        expression=location,
    )


__all__ = [
    "Invariant",
    "InvariantPack",
    "load_invariants",
    "skipped",
    "validate_invariants",
]
