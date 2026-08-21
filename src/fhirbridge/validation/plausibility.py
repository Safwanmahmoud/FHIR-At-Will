"""L5 plausibility checks (AGENTS.md 10).

Pure Python over the serialized resource; no network, no LLM, deterministic.

The rule kinds are a closed set rather than embedded expressions. That is a
safety decision: a YAML file that can express arbitrary logic is a YAML file
that can quietly grow into clinical decision support, which principle 2.9
forbids. Adding a *kind* requires a code change and a review.

Every rule's severity can be overridden per deployment, which is what
"configurable severity" in the cascade table means.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import yaml

from fhirbridge.validation.models import (
    IssueSeverity,
    LayerResult,
    LayerStatus,
    ValidationIssue,
    ValidationLayer,
)

_LAYER = ValidationLayer.PLAUSIBILITY
_RULES_PATH: Final = Path(__file__).parent / "rules" / "plausibility.yaml"
_WILDCARD: Final = "*"


class RuleKind(StrEnum):
    QUANTITY_RANGE = "quantity_range"
    NON_NEGATIVE_QUANTITY = "non_negative_quantity"
    FUTURE_DATE = "future_date"
    DATE_ORDER = "date_order"
    BIRTH_DATE_ORDER = "birth_date_order"
    DOSE_MAGNITUDE = "dose_magnitude"
    SEX_ANATOMY_CONFLICT = "sex_anatomy_conflict"


@dataclass(frozen=True, slots=True)
class PlausibilityRule:
    id: str
    kind: RuleKind
    severity: IssueSeverity
    message: str
    applies_to: frozenset[str]
    enabled: bool = True
    codes: frozenset[str] = frozenset()
    expected_unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    paths: tuple[str, ...] = ()
    pairs: tuple[tuple[str, str], ...] = ()
    limits: Mapping[str, float] = field(default_factory=dict)
    gender: str | None = None

    def applies(self, resource_type: str) -> bool:
        return _WILDCARD in self.applies_to or resource_type in self.applies_to


@dataclass(frozen=True, slots=True)
class PlausibilityPack:
    rules: tuple[PlausibilityRule, ...]
    future_date_tolerance_days: int

    def for_resource(self, resource_type: str) -> tuple[PlausibilityRule, ...]:
        return tuple(rule for rule in self.rules if rule.enabled and rule.applies(resource_type))


@lru_cache(maxsize=1)
def load_plausibility_rules(path: str | None = None) -> PlausibilityPack:
    """Load and cache ``rules/plausibility.yaml``."""
    source = Path(path) if path else _RULES_PATH
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    defaults = raw.get("defaults", {}) or {}
    rules: list[PlausibilityRule] = []
    for entry in raw.get("rules", []) or []:
        rules.append(
            PlausibilityRule(
                id=str(entry["id"]),
                kind=RuleKind(str(entry["kind"])),
                severity=IssueSeverity(str(entry.get("severity", "warning"))),
                message=str(entry.get("message", "")).strip(),
                applies_to=frozenset(str(item) for item in entry.get("applies_to", [_WILDCARD])),
                enabled=bool(entry.get("enabled", True)),
                codes=frozenset(str(item) for item in entry.get("codes", []) or []),
                expected_unit=(str(entry["expected_unit"]) if entry.get("expected_unit") else None),
                minimum=_as_float(entry.get("min")),
                maximum=_as_float(entry.get("max")),
                paths=tuple(str(item) for item in entry.get("paths", []) or []),
                pairs=tuple(
                    (str(pair["earlier"]), str(pair["later"]))
                    for pair in entry.get("pairs", []) or []
                ),
                limits={
                    str(unit): float(limit)
                    for unit, limit in (entry.get("limits", {}) or {}).items()
                },
                gender=str(entry["gender"]) if entry.get("gender") else None,
            )
        )
    return PlausibilityPack(
        rules=tuple(rules),
        future_date_tolerance_days=int(defaults.get("future_date_tolerance_days", 1)),
    )


@dataclass(slots=True)
class _Target:
    """One resource to check, with its reporting location."""

    resource_type: str
    resource: dict[str, Any]
    location: str


def validate_plausibility(
    payload: dict[str, Any],
    *,
    resource_type: str,
    pack: PlausibilityPack | None = None,
    severity_overrides: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> LayerResult:
    """Run the plausibility pack against ``payload``."""
    started = time.perf_counter()
    rules = pack or load_plausibility_rules()
    overrides = {key: IssueSeverity(value) for key, value in (severity_overrides or {}).items()}
    reference_now = now or datetime.now(UTC)

    targets = _collect_targets(payload, resource_type)
    patients = _index_patients(payload, resource_type)

    issues: list[ValidationIssue] = []
    fired = 0
    for target in targets:
        for rule in rules.for_resource(target.resource_type):
            new_issues = _apply_rule(
                rule,
                target=target,
                pack=rules,
                patients=patients,
                now=reference_now,
            )
            for issue in new_issues:
                severity = overrides.get(rule.id, issue.severity)
                issues.append(
                    issue
                    if severity is issue.severity
                    else issue.model_copy(update={"severity": severity})
                )
            fired += len(new_issues)

    notes = [
        f"Applied {len([r for r in rules.rules if r.enabled])} enabled rule(s) to "
        f"{len(targets)} resource(s); {fired} finding(s)."
    ]
    disabled = [rule.id for rule in rules.rules if not rule.enabled]
    if disabled:
        notes.append(
            f"{len(disabled)} rule(s) are disabled in this deployment: {', '.join(disabled)}."
        )
    notes.append(
        "L5 reports physiologically impossible values only. It does not interpret "
        "abnormal-but-possible results, which would be clinical decision support."
    )

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


# --- Rule dispatch ---------------------------------------------------------


def _apply_rule(
    rule: PlausibilityRule,
    *,
    target: _Target,
    pack: PlausibilityPack,
    patients: Mapping[str, dict[str, Any]],
    now: datetime,
) -> list[ValidationIssue]:
    match rule.kind:
        case RuleKind.QUANTITY_RANGE:
            return _check_quantity_range(rule, target)
        case RuleKind.NON_NEGATIVE_QUANTITY:
            return _check_non_negative(rule, target)
        case RuleKind.FUTURE_DATE:
            return _check_future_dates(rule, target, pack, now)
        case RuleKind.DATE_ORDER:
            return _check_date_order(rule, target)
        case RuleKind.BIRTH_DATE_ORDER:
            return _check_birth_date_order(rule, target, patients)
        case RuleKind.DOSE_MAGNITUDE:
            return _check_dose_magnitude(rule, target)
        case RuleKind.SEX_ANATOMY_CONFLICT:
            return _check_sex_anatomy(rule, target, patients)


def _check_quantity_range(rule: PlausibilityRule, target: _Target) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for holder_codes, quantity, location in _iter_coded_quantities(target):
        if not rule.codes & holder_codes:
            continue
        value = _as_float(quantity.get("value"))
        if value is None:
            continue
        unit = quantity.get("code") or quantity.get("unit")

        if rule.expected_unit and unit and unit != rule.expected_unit:
            issues.append(
                _issue(
                    rule,
                    f"{rule.message} Expected unit '{rule.expected_unit}' but found "
                    f"'{unit}', so the range check could not be applied. Convert the "
                    "value or correct the unit.",
                    f"{location}.code",
                    severity=IssueSeverity.WARNING,
                )
            )
            continue

        if (rule.minimum is not None and value < rule.minimum) or (
            rule.maximum is not None and value > rule.maximum
        ):
            bounds = f"[{rule.minimum}, {rule.maximum}]"
            issues.append(
                _issue(
                    rule,
                    f"{rule.message} Value {value} {unit or ''} is outside the "
                    f"plausible range {bounds}.".replace("  ", " "),
                    f"{location}.value",
                )
            )
    return issues


def _check_non_negative(rule: PlausibilityRule, target: _Target) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for _codes, quantity, location in _iter_coded_quantities(target):
        value = _as_float(quantity.get("value"))
        if value is not None and value < 0:
            issues.append(_issue(rule, f"{rule.message} Value is {value}.", f"{location}.value"))
    return issues


def _check_future_dates(
    rule: PlausibilityRule, target: _Target, pack: PlausibilityPack, now: datetime
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    cutoff = now + timedelta(days=pack.future_date_tolerance_days)
    for path in rule.paths:
        if not path.startswith(f"{target.resource_type}."):
            continue
        element = path.split(".", 1)[1]
        raw = target.resource.get(element)
        moment = _parse_fhir_datetime(raw)
        if moment is not None and moment > cutoff:
            issues.append(
                _issue(
                    rule,
                    f"{rule.message} {element} is {raw}, which is after "
                    f"{cutoff.date().isoformat()}.",
                    f"{target.location}.{element}",
                )
            )
    return issues


def _check_date_order(rule: PlausibilityRule, target: _Target) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for earlier_path, later_path in rule.pairs:
        if not earlier_path.startswith(f"{target.resource_type}."):
            continue
        earlier_element = earlier_path.split(".", 1)[1]
        later_element = later_path.split(".", 1)[1]
        earlier = _parse_fhir_datetime(target.resource.get(earlier_element))
        later = _parse_fhir_datetime(target.resource.get(later_element))
        if earlier is not None and later is not None and earlier > later:
            issues.append(
                _issue(
                    rule,
                    f"{rule.message} {earlier_element}={target.resource.get(earlier_element)} "
                    f"is after {later_element}={target.resource.get(later_element)}.",
                    f"{target.location}.{later_element}",
                )
            )
    return issues


def _check_birth_date_order(
    rule: PlausibilityRule, target: _Target, patients: Mapping[str, dict[str, Any]]
) -> list[ValidationIssue]:
    if target.resource_type == "Patient":
        return []
    patient = _resolve_subject(target.resource, patients)
    if patient is None:
        return []
    birth = _parse_fhir_datetime(patient.get("birthDate"))
    if birth is None:
        return []

    issues: list[ValidationIssue] = []
    for path in rule.paths:
        if not path.startswith(f"{target.resource_type}."):
            continue
        element = path.split(".", 1)[1]
        moment = _parse_fhir_datetime(target.resource.get(element))
        if moment is not None and moment < birth:
            issues.append(
                _issue(
                    rule,
                    f"{rule.message} {element}={target.resource.get(element)} precedes "
                    f"the subject's birthDate={patient.get('birthDate')}.",
                    f"{target.location}.{element}",
                )
            )
    return issues


def _check_dose_magnitude(rule: PlausibilityRule, target: _Target) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for index, dosage in enumerate(_as_list(target.resource.get("dosage"))):
        if not isinstance(dosage, dict):
            continue
        for rate_index, dose_and_rate in enumerate(_as_list(dosage.get("doseAndRate"))):
            if not isinstance(dose_and_rate, dict):
                continue
            quantity = dose_and_rate.get("doseQuantity")
            if not isinstance(quantity, dict):
                continue
            value = _as_float(quantity.get("value"))
            unit = quantity.get("code") or quantity.get("unit")
            if value is None or unit is None:
                continue
            limit = rule.limits.get(str(unit))
            if limit is not None and value > limit:
                issues.append(
                    _issue(
                        rule,
                        f"{rule.message} Dose {value} {unit} exceeds the plausible "
                        f"maximum of {limit} {unit}.",
                        f"{target.location}.dosage[{index}].doseAndRate[{rate_index}]"
                        ".doseQuantity.value",
                    )
                )
    return issues


def _check_sex_anatomy(
    rule: PlausibilityRule, target: _Target, patients: Mapping[str, dict[str, Any]]
) -> list[ValidationIssue]:
    patient = _resolve_subject(target.resource, patients)
    if patient is None or patient.get("gender") != rule.gender:
        return []
    codes = _concept_codes(target.resource.get("code"))
    if not rule.codes & codes:
        return []
    return [_issue(rule, rule.message, f"{target.location}.code")]


# --- Traversal helpers -----------------------------------------------------


def _collect_targets(payload: dict[str, Any], resource_type: str) -> list[_Target]:
    targets = [_Target(resource_type, payload, resource_type)]
    if resource_type != "Bundle":
        return targets
    for index, entry in enumerate(_as_list(payload.get("entry"))):
        if not isinstance(entry, dict):
            continue
        resource = entry.get("resource")
        if isinstance(resource, dict) and isinstance(resource.get("resourceType"), str):
            targets.append(
                _Target(
                    str(resource["resourceType"]),
                    resource,
                    f"Bundle.entry[{index}].resource",
                )
            )
    return targets


def _index_patients(payload: dict[str, Any], resource_type: str) -> dict[str, dict[str, Any]]:
    """Index Patients in the payload by ``Patient/{id}``, ``{id}`` and fullUrl."""
    index: dict[str, dict[str, Any]] = {}

    def register(resource: dict[str, Any], full_url: str | None) -> None:
        identifier = resource.get("id")
        if isinstance(identifier, str):
            index[f"Patient/{identifier}"] = resource
            index[identifier] = resource
        if full_url:
            index[full_url] = resource

    if resource_type == "Patient":
        register(payload, None)
        return index

    for entry in _as_list(payload.get("entry")):
        if not isinstance(entry, dict):
            continue
        resource = entry.get("resource")
        if isinstance(resource, dict) and resource.get("resourceType") == "Patient":
            full_url = entry.get("fullUrl")
            register(resource, full_url if isinstance(full_url, str) else None)
    return index


def _resolve_subject(
    resource: dict[str, Any], patients: Mapping[str, dict[str, Any]]
) -> dict[str, Any] | None:
    for key in ("subject", "patient"):
        reference = resource.get(key)
        if not isinstance(reference, dict):
            continue
        target = reference.get("reference")
        if isinstance(target, str):
            resolved = patients.get(target) or patients.get(target.split("/")[-1])
            if resolved is not None:
                return resolved
    # A single-Patient bundle with unresolvable references is still unambiguous.
    unique = {id(value): value for value in patients.values()}
    return next(iter(unique.values())) if len(unique) == 1 else None


def _iter_coded_quantities(
    target: _Target,
) -> list[tuple[frozenset[str], dict[str, Any], str]]:
    """Yield ``(codes, quantity, location)`` for a resource's coded quantities."""
    found: list[tuple[frozenset[str], dict[str, Any], str]] = []
    root_codes = _concept_codes(target.resource.get("code"))

    quantity = target.resource.get("valueQuantity")
    if isinstance(quantity, dict):
        found.append((root_codes, quantity, f"{target.location}.valueQuantity"))

    for index, component in enumerate(_as_list(target.resource.get("component"))):
        if not isinstance(component, dict):
            continue
        component_quantity = component.get("valueQuantity")
        if isinstance(component_quantity, dict):
            found.append(
                (
                    _concept_codes(component.get("code")),
                    component_quantity,
                    f"{target.location}.component[{index}].valueQuantity",
                )
            )
    return found


def _concept_codes(concept: object) -> frozenset[str]:
    """Render a CodeableConcept's codings as ``system|code`` strings."""
    if not isinstance(concept, dict):
        return frozenset()
    codes: set[str] = set()
    for coding in _as_list(concept.get("coding")):
        if isinstance(coding, dict):
            system = coding.get("system")
            code = coding.get("code")
            if isinstance(system, str) and isinstance(code, str):
                codes.add(f"{system}|{code}")
    return frozenset(codes)


def _as_list(value: object) -> Sequence[Any]:
    if isinstance(value, list):
        return value
    return ()


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _parse_fhir_datetime(value: object) -> datetime | None:
    """Parse FHIR ``date``, ``dateTime`` and ``instant`` into an aware datetime.

    Partial dates are anchored at the start of the period, which is the
    conservative reading for an ordering check: it cannot manufacture a
    violation that a full date would not also show.
    """
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    try:
        if len(text) == 4:
            return datetime(int(text), 1, 1, tzinfo=UTC)
        if len(text) == 7:
            year, month = text.split("-")
            return datetime(int(year), int(month), 1, tzinfo=UTC)
        if len(text) == 10:
            parsed_date = date.fromisoformat(text)
            return datetime(parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=UTC)
        normalized = text.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def _issue(
    rule: PlausibilityRule,
    message: str,
    expression: str,
    *,
    severity: IssueSeverity | None = None,
) -> ValidationIssue:
    return ValidationIssue(
        layer=_LAYER,
        severity=severity or rule.severity,
        code="business-rule",
        rule_id=rule.id,
        message=message,
        expression=expression,
    )


__all__ = [
    "PlausibilityPack",
    "PlausibilityRule",
    "RuleKind",
    "load_plausibility_rules",
    "skipped",
    "validate_plausibility",
]
