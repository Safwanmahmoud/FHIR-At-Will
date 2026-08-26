"""The deterministic tools the craft agent drives (principle 2.3, AGENTS.md 10).

Every mutating tool follows the same contract: build a *candidate* resource from
the model's arguments, prove it before it is committed, and refuse it otherwise.
Proof is two-part and both parts are deterministic:

* **Structure** — the candidate must parse through the L1 typed model, so unknown
  elements, bad cardinality and malformed primitives are rejected locally.
* **Terminology** — every clinical ``Coding`` the model introduced must be
  confirmed by ``$validate-code`` (LOINC, SNOMED CT, RxNorm, UCUM). A small,
  fixed set of FHIR code enums (status, clinical-status, gender) is checked
  against its published values locally rather than paid for on the wire.

When a check fails the tool returns the errors to the model instead of raising,
so the loop can try again; the draft never enters a non-conformant state. When
the terminology server cannot answer, the tool fails closed (2.4): an
unverifiable code is refused, never assumed valid.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from fhirbridge.agent.draft import PATIENT_ALIAS, DraftState
from fhirbridge.domain.errors import DomainError, TerminologyUnavailableError
from fhirbridge.terminology.interface import TerminologyClient, search_value_set_for_system
from fhirbridge.validation.cascade import ValidationCascade, ValidationSpec
from fhirbridge.validation.models import IssueSeverity, ValidationLayer
from fhirbridge.validation.structural import validate_structure

UCUM_SYSTEM = "http://unitsofmeasure.org"
LOINC_SYSTEM = "http://loinc.org"
SNOMED_SYSTEM = "http://snomed.info/sct"
RXNORM_SYSTEM = "http://www.nlm.nih.gov/research/umls/rxnorm"
OBSERVATION_CATEGORY_SYSTEM = "http://terminology.hl7.org/CodeSystem/observation-category"
CONDITION_CLINICAL_SYSTEM = "http://terminology.hl7.org/CodeSystem/condition-clinical"
ALLERGY_CLINICAL_SYSTEM = "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical"

# Small, fixed FHIR code enums: verified locally so a status typo is caught
# without a terminology round-trip.
_OBSERVATION_STATUS = frozenset(
    {
        "registered",
        "preliminary",
        "final",
        "amended",
        "corrected",
        "cancelled",
        "entered-in-error",
        "unknown",
    }
)
_CONDITION_CLINICAL = frozenset(
    {"active", "recurrence", "relapse", "inactive", "remission", "resolved"}
)
_MEDICATION_STATEMENT_STATUS = frozenset(
    {
        "active",
        "completed",
        "entered-in-error",
        "intended",
        "stopped",
        "on-hold",
        "unknown",
        "not-taken",
    }
)
_ALLERGY_CLINICAL = frozenset({"active", "inactive", "resolved"})
_ALLERGY_CRITICALITY = frozenset({"low", "high", "unable-to-assess"})
_ADMIN_GENDER = frozenset({"male", "female", "other", "unknown"})

_MAX_TRACE_ISSUES = 20


@dataclass(slots=True)
class ToolContext:
    """Everything a tool needs: the draft, the deterministic checkers, the spec."""

    draft: DraftState
    terminology: TerminologyClient
    cascade: ValidationCascade
    profiles: tuple[str, ...] = ()
    layers: frozenset[ValidationLayer] | None = None
    max_terminology_checks: int = 500
    ig_packages: tuple[str, ...] = ()
    conversion_id: str | None = None
    validation_enabled: bool = True


@dataclass(slots=True)
class ToolOutcome:
    """A tool's answer: a JSON-able body for the model, and control signals."""

    ok: bool
    content: dict[str, Any]
    finish: bool = False
    error: str | None = None


# --- Argument coercion -----------------------------------------------------


def _str(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _num(args: dict[str, Any], key: str) -> float | int | None:
    value = args.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return int(value) if value.strip().lstrip("-").isdigit() else float(value)
        except ValueError:
            return None
    return None


def _given_names(args: dict[str, Any], key: str) -> list[str]:
    value = args.get(key)
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []


# --- Deterministic gates ---------------------------------------------------


def _structural_errors(candidate: dict[str, Any]) -> list[str]:
    """Blocking L1 issues for a candidate resource, as short strings."""
    outcome = validate_structure(candidate)
    return [
        f"{issue.expression or issue.code}: {issue.message}"
        for issue in outcome.result.issues
        if issue.severity.is_blocking
    ]


def _collect_codings(node: Any) -> list[tuple[str, str]]:
    """Every ``system``/``code`` pair anywhere in a resource tree."""
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        system = node.get("system")
        code = node.get("code")
        if isinstance(system, str) and isinstance(code, str) and system and code:
            found.append((system, code))
        for value in node.values():
            found.extend(_collect_codings(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_collect_codings(item))
    return found


async def _terminology_errors(
    terminology: TerminologyClient, codings: Iterable[tuple[str, str]]
) -> list[str]:
    """Confirm each coding with ``$validate-code``; fail closed on outage.

    Displays are intentionally not sent: the gate confirms the code *exists and
    is valid*, and a display that merely disagrees with the server's preferred
    term is an L3 warning, not a reason to refuse the edit.
    """
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for system, code in codings:
        if (system, code) in seen:
            continue
        seen.add((system, code))
        try:
            result = await terminology.validate_code(system=system, code=code)
        except TerminologyUnavailableError:
            errors.append(
                f"could not verify {system}|{code}: the terminology server is "
                "unavailable, so this code cannot be committed (failing closed)"
            )
            continue
        except DomainError:
            errors.append(
                f"could not verify {system}|{code}: the terminology server does not "
                "know this code system"
            )
            continue
        if not result.result:
            detail = result.message or (result.issues[0] if result.issues else "code not valid")
            errors.append(f"{system}|{code} rejected by the terminology server: {detail}")
    return errors


def _reject(errors: list[str]) -> ToolOutcome:
    return ToolOutcome(
        ok=False,
        content={"ok": False, "errors": errors},
        error="; ".join(errors)[:400],
    )


def _committed(full_url: str, resource_type: str, draft: DraftState) -> ToolOutcome:
    return ToolOutcome(
        ok=True,
        content={
            "ok": True,
            "full_url": full_url,
            "resource_type": resource_type,
            "draft": draft.summary(),
        },
    )


async def _commit_resource(
    ctx: ToolContext,
    full_url: str,
    candidate: dict[str, Any],
    clinical_codings: list[tuple[str, str]],
) -> ToolOutcome:
    """Run both gates and commit, or return the failures to the model."""
    structural = _structural_errors(candidate)
    if structural:
        return _reject(structural)
    terminology = await _terminology_errors(ctx.terminology, clinical_codings)
    if terminology:
        return _reject(terminology)
    ctx.draft.put(full_url, candidate)
    return _committed(full_url, str(candidate.get("resourceType", "")), ctx.draft)


# --- Tool handlers ---------------------------------------------------------


async def _set_patient_demographics(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    candidate = ctx.draft.snapshot(ctx.draft.patient_full_url) or {"resourceType": "Patient"}

    family = _str(args, "family")
    given = _given_names(args, "given")
    if family or given:
        name: dict[str, Any] = {}
        if family:
            name["family"] = family
        if given:
            name["given"] = given
        candidate["name"] = [name]

    gender = _str(args, "gender")
    if gender is not None:
        if gender.lower() not in _ADMIN_GENDER:
            return _reject([f"gender must be one of {sorted(_ADMIN_GENDER)}, got '{gender}'"])
        candidate["gender"] = gender.lower()

    birth_date = _str(args, "birth_date")
    if birth_date is not None:
        candidate["birthDate"] = birth_date

    structural = _structural_errors(candidate)
    if structural:
        return _reject(structural)
    ctx.draft.put(ctx.draft.patient_full_url, candidate)
    return _committed(ctx.draft.patient_full_url, "Patient", ctx.draft)


async def _add_observation(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    code = _str(args, "code")
    display = _str(args, "display")
    if not code or not display:
        return _reject(["'code' and 'display' are required"])
    system = _str(args, "code_system") or LOINC_SYSTEM
    status = (_str(args, "status") or "final").lower()
    if status not in _OBSERVATION_STATUS:
        return _reject([f"status must be one of {sorted(_OBSERVATION_STATUS)}"])

    observation: dict[str, Any] = {
        "resourceType": "Observation",
        "status": status,
        "code": {"coding": [{"system": system, "code": code, "display": display}], "text": display},
        "subject": ctx.draft.patient_reference(),
    }

    category_code = _str(args, "category_code")
    if category_code:
        observation["category"] = [
            {
                "coding": [
                    {
                        "system": OBSERVATION_CATEGORY_SYSTEM,
                        "code": category_code,
                        "display": _str(args, "category_display") or category_code,
                    }
                ]
            }
        ]

    effective = _str(args, "effective_datetime")
    if effective:
        observation["effectiveDateTime"] = effective

    clinical_codings = [(system, code)]
    value_number = _num(args, "value_number")
    if value_number is not None:
        quantity: dict[str, Any] = {"value": value_number}
        unit = _str(args, "unit")
        unit_code = _str(args, "unit_code")
        if unit:
            quantity["unit"] = unit
        if unit_code:
            quantity["system"] = UCUM_SYSTEM
            quantity["code"] = unit_code
            clinical_codings.append((UCUM_SYSTEM, unit_code))
        observation["valueQuantity"] = quantity
    else:
        value_string = _str(args, "value_string")
        if value_string:
            observation["valueString"] = value_string

    full_url, _ = ctx.draft.new_resource("Observation")
    return await _commit_resource(ctx, full_url, observation, clinical_codings)


async def _add_condition(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    code = _str(args, "code")
    display = _str(args, "display")
    if not code or not display:
        return _reject(["'code' and 'display' are required"])
    system = _str(args, "code_system") or SNOMED_SYSTEM

    condition: dict[str, Any] = {
        "resourceType": "Condition",
        "code": {"coding": [{"system": system, "code": code, "display": display}], "text": display},
        "subject": ctx.draft.patient_reference(),
    }

    clinical_status = _str(args, "clinical_status")
    if clinical_status:
        if clinical_status.lower() not in _CONDITION_CLINICAL:
            return _reject([f"clinical_status must be one of {sorted(_CONDITION_CLINICAL)}"])
        condition["clinicalStatus"] = {
            "coding": [{"system": CONDITION_CLINICAL_SYSTEM, "code": clinical_status.lower()}]
        }

    onset = _str(args, "onset_datetime")
    if onset:
        condition["onsetDateTime"] = onset

    full_url, _ = ctx.draft.new_resource("Condition")
    return await _commit_resource(ctx, full_url, condition, [(system, code)])


async def _add_medication_statement(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    code = _str(args, "code")
    display = _str(args, "display")
    if not code or not display:
        return _reject(["'code' and 'display' are required"])
    system = _str(args, "code_system") or RXNORM_SYSTEM
    status = (_str(args, "status") or "active").lower()
    if status not in _MEDICATION_STATEMENT_STATUS:
        return _reject([f"status must be one of {sorted(_MEDICATION_STATEMENT_STATUS)}"])

    statement: dict[str, Any] = {
        "resourceType": "MedicationStatement",
        "status": status,
        "medicationCodeableConcept": {
            "coding": [{"system": system, "code": code, "display": display}],
            "text": display,
        },
        "subject": ctx.draft.patient_reference(),
    }

    dosage_text = _str(args, "dosage_text")
    if dosage_text:
        statement["dosage"] = [{"text": dosage_text}]

    effective = _str(args, "effective_datetime")
    if effective:
        statement["effectiveDateTime"] = effective

    full_url, _ = ctx.draft.new_resource("MedicationStatement")
    return await _commit_resource(ctx, full_url, statement, [(system, code)])


async def _add_allergy_intolerance(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    display = _str(args, "display")
    if not display:
        return _reject(["'display' is required"])
    code = _str(args, "code")
    system = _str(args, "code_system") or SNOMED_SYSTEM

    allergy: dict[str, Any] = {
        "resourceType": "AllergyIntolerance",
        "patient": ctx.draft.patient_reference(),
    }
    clinical_codings: list[tuple[str, str]] = []
    if code:
        allergy["code"] = {
            "coding": [{"system": system, "code": code, "display": display}],
            "text": display,
        }
        clinical_codings.append((system, code))
    else:
        allergy["code"] = {"text": display}

    clinical_status = _str(args, "clinical_status")
    if clinical_status:
        if clinical_status.lower() not in _ALLERGY_CLINICAL:
            return _reject([f"clinical_status must be one of {sorted(_ALLERGY_CLINICAL)}"])
        allergy["clinicalStatus"] = {
            "coding": [{"system": ALLERGY_CLINICAL_SYSTEM, "code": clinical_status.lower()}]
        }

    criticality = _str(args, "criticality")
    if criticality:
        if criticality.lower() not in _ALLERGY_CRITICALITY:
            return _reject([f"criticality must be one of {sorted(_ALLERGY_CRITICALITY)}"])
        allergy["criticality"] = criticality.lower()

    full_url, _ = ctx.draft.new_resource("AllergyIntolerance")
    return await _commit_resource(ctx, full_url, allergy, clinical_codings)


async def _search_terminology(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    query = _str(args, "query")
    if not query:
        return _reject(["'query' is required"])
    value_set = _str(args, "value_set")
    system = _str(args, "system")
    if not value_set:
        if not system:
            return _reject(["provide either 'system' or 'value_set' to search"])
        value_set = search_value_set_for_system(system)
    count = _num(args, "count")
    limit = int(count) if isinstance(count, (int, float)) and count > 0 else 10

    try:
        expansion = await ctx.terminology.expand(
            value_set=value_set, filter_text=query, count=limit
        )
    except TerminologyUnavailableError:
        return ToolOutcome(
            ok=False,
            content={"ok": False, "errors": ["the terminology server is unavailable"]},
            error="terminology unavailable",
        )
    except DomainError:
        return _reject([f"unknown value set or code system: {value_set}"])

    candidates = [
        {"system": coding.system, "code": coding.code, "display": coding.display}
        for coding in expansion.contains
        if coding.code
    ]
    return ToolOutcome(ok=True, content={"ok": True, "candidates": candidates})


async def _set_element(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    reference = _str(args, "full_url") or PATIENT_ALIAS
    resolved = ctx.draft.resolve(reference)
    if resolved is None:
        return _reject([f"unknown resource '{reference}'; add it first, then use its full_url"])
    path = _str(args, "path")
    if not path:
        return _reject(["'path' is required, e.g. 'note.0.text' or 'encounter.reference'"])
    raw_value = args.get("value_json")
    if not isinstance(raw_value, str):
        return _reject(["'value_json' must be a JSON-encoded string"])
    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        return _reject([f"value_json is not valid JSON: {exc}"])

    candidate = ctx.draft.snapshot(resolved)
    if candidate is None:  # pragma: no cover - resolve already guaranteed existence
        return _reject([f"unknown resource '{reference}'"])

    error = _apply_path(candidate, path, value)
    if error:
        return _reject([error])

    return await _commit_resource(ctx, resolved, candidate, _collect_codings(value))


def _apply_path(target: dict[str, Any], path: str, value: Any) -> str | None:
    """Set ``value`` at a dotted ``path``, creating intermediate objects/lists.

    Numeric segments index (and grow) a list; everything else is an object key.
    ``resourceType`` is off-limits so a tool can never change a resource's type
    out from under the validators.
    """
    segments = [segment for segment in path.split(".") if segment]
    if not segments:
        return "path is empty"
    if segments[0] == "resourceType":
        return "resourceType cannot be changed"

    cursor: Any = target
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        is_number = segment.lstrip("-").isdigit()
        if last:
            if is_number:
                if not isinstance(cursor, list):
                    return f"'{segment}' indexes a list but the parent is not a list"
                position = int(segment)
                _grow(cursor, position)
                cursor[position] = value
            else:
                if not isinstance(cursor, dict):
                    return f"cannot set '{segment}' on a non-object"
                cursor[segment] = value
            return None
        nxt = segments[index + 1]
        want_list = nxt.lstrip("-").isdigit()
        if is_number:
            if not isinstance(cursor, list):
                return f"'{segment}' indexes a list but the parent is not a list"
            position = int(segment)
            _grow(cursor, position)
            if not isinstance(cursor[position], (dict, list)):
                cursor[position] = [] if want_list else {}
            cursor = cursor[position]
        else:
            if not isinstance(cursor, dict):
                return f"cannot descend into '{segment}' of a non-object"
            if not isinstance(cursor.get(segment), (dict, list)):
                cursor[segment] = [] if want_list else {}
            cursor = cursor[segment]
    return None


def _grow(target: list[Any], index: int) -> None:
    while len(target) <= index:
        target.append(None)


async def _validate_draft(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    del args
    if not ctx.validation_enabled:
        return ToolOutcome(
            ok=True,
            content={
                "ok": True,
                "skipped": True,
                "message": (
                    "Validation is disabled for this comparison-only request. "
                    "Finish after adding all narrative facts."
                ),
            },
        )
    report = await ctx.cascade.run(
        ctx.draft.to_bundle(),
        ValidationSpec(
            profiles=ctx.profiles,
            layers=ctx.layers,
            max_terminology_checks=ctx.max_terminology_checks,
            ig_packages=ctx.ig_packages,
            conversion_id=ctx.conversion_id,
        ),
    )
    blocking = [
        {
            "layer": str(issue.layer),
            "code": issue.code,
            "expression": issue.expression,
            "message": issue.message[:200],
        }
        for issue in report.blocking_issues
    ][:_MAX_TRACE_ISSUES]
    warnings = sum(
        1
        for layer in report.layers
        for issue in layer.issues
        if issue.severity is IssueSeverity.WARNING
    )
    return ToolOutcome(
        ok=True,
        content={
            "ok": True,
            "status": str(report.status),
            "conformant": report.conformant,
            "resource_count": report.resource_count,
            "blocking_issues": blocking,
            "warnings": warnings,
        },
    )


async def _finish(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    del args
    return ToolOutcome(
        ok=True,
        content={"ok": True, "draft": ctx.draft.summary()},
        finish=True,
    )


# --- Registry and schemas --------------------------------------------------

_HANDLERS = {
    "set_patient_demographics": _set_patient_demographics,
    "add_observation": _add_observation,
    "add_condition": _add_condition,
    "add_medication_statement": _add_medication_statement,
    "add_allergy_intolerance": _add_allergy_intolerance,
    "search_terminology": _search_terminology,
    "set_element": _set_element,
    "validate_draft": _validate_draft,
    "finish": _finish,
}


def _tool(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


_STR: dict[str, Any] = {"type": "string"}


TOOL_SCHEMAS: list[dict[str, Any]] = [
    _tool(
        "set_patient_demographics",
        "Set demographics on the subject Patient. Call only with details the note "
        "states. Omit anything the note does not support.",
        {
            "family": {**_STR, "description": "Family (last) name."},
            "given": {
                "type": "array",
                "items": _STR,
                "description": "Given name(s), first then middle.",
            },
            "gender": {
                **_STR,
                "description": "Administrative gender: male, female, other, or unknown.",
            },
            "birth_date": {**_STR, "description": "Date of birth as YYYY, YYYY-MM, or YYYY-MM-DD."},
        },
        [],
    ),
    _tool(
        "add_observation",
        "Add one Observation (a vital sign, lab result, or measurement). Use "
        "search_terminology first to find a real LOINC code if unsure.",
        {
            "code": {**_STR, "description": "The code for what was measured, e.g. a LOINC code."},
            "display": {**_STR, "description": "Human-readable name of the measurement."},
            "code_system": {**_STR, "description": f"Code system URL. Defaults to {LOINC_SYSTEM}."},
            "status": {**_STR, "description": "Observation status. Defaults to 'final'."},
            "category_code": {
                **_STR,
                "description": "observation-category code, e.g. 'vital-signs' or 'laboratory'.",
            },
            "category_display": {**_STR, "description": "Display for the category."},
            "effective_datetime": {**_STR, "description": "When observed (ISO 8601)."},
            "value_number": {"type": "number", "description": "Numeric result value."},
            "unit": {**_STR, "description": "Human-readable unit, e.g. 'beats/minute'."},
            "unit_code": {**_STR, "description": "UCUM unit code, e.g. '/min', 'mm[Hg]', 'mg/dL'."},
            "value_string": {**_STR, "description": "Use only for a non-numeric result."},
        },
        ["code", "display"],
    ),
    _tool(
        "add_condition",
        "Add one Condition (a problem, diagnosis, or clinical finding). Prefer a "
        "SNOMED CT code; use search_terminology to find one.",
        {
            "code": {**_STR, "description": "The condition code, e.g. a SNOMED CT code."},
            "display": {**_STR, "description": "Human-readable name of the condition."},
            "code_system": {
                **_STR,
                "description": f"Code system URL. Defaults to {SNOMED_SYSTEM}.",
            },
            "clinical_status": {
                **_STR,
                "description": "active, recurrence, relapse, inactive, remission, or resolved.",
            },
            "onset_datetime": {**_STR, "description": "Onset date/time (ISO 8601)."},
        },
        ["code", "display"],
    ),
    _tool(
        "add_medication_statement",
        "Add one MedicationStatement (a medication the patient takes). Prefer an "
        "RxNorm code; use search_terminology to find one.",
        {
            "code": {**_STR, "description": "The medication code, e.g. an RxNorm code."},
            "display": {**_STR, "description": "Human-readable medication name."},
            "code_system": {
                **_STR,
                "description": f"Code system URL. Defaults to {RXNORM_SYSTEM}.",
            },
            "status": {**_STR, "description": "Statement status. Defaults to 'active'."},
            "dosage_text": {**_STR, "description": "Free-text dosage, e.g. '500 mg twice daily'."},
            "effective_datetime": {**_STR, "description": "When taken/recorded (ISO 8601)."},
        },
        ["code", "display"],
    ),
    _tool(
        "add_allergy_intolerance",
        "Add one AllergyIntolerance. A coded substance is preferred but a text-only "
        "allergy is allowed when no code is known.",
        {
            "display": {**_STR, "description": "The substance or allergy, human-readable."},
            "code": {**_STR, "description": "Substance code (optional), e.g. SNOMED CT or RxNorm."},
            "code_system": {
                **_STR,
                "description": f"Code system URL for 'code'. Defaults to {SNOMED_SYSTEM}.",
            },
            "clinical_status": {**_STR, "description": "active, inactive, or resolved."},
            "criticality": {**_STR, "description": "low, high, or unable-to-assess."},
        },
        ["display"],
    ),
    _tool(
        "search_terminology",
        "Find valid codes for a term before you use them. Returns candidate "
        "system/code/display triples from the terminology server.",
        {
            "query": {**_STR, "description": "Text to search for, e.g. 'heart rate' or 'aspirin'."},
            "system": {
                **_STR,
                "description": (
                    f"Code system to search, e.g. {LOINC_SYSTEM}, {SNOMED_SYSTEM}, or "
                    f"{RXNORM_SYSTEM}."
                ),
            },
            "value_set": {**_STR, "description": "A ValueSet URL to search instead of a system."},
            "count": {"type": "integer", "description": "Max candidates to return (default 10)."},
        },
        ["query"],
    ),
    _tool(
        "set_element",
        "Set an arbitrary FHIR element on an existing resource, for fields the "
        "other tools do not cover. The change is validated before it is kept.",
        {
            "full_url": {
                **_STR,
                "description": "'patient', or the full_url a previous tool returned.",
            },
            "path": {
                **_STR,
                "description": "Dotted element path; numeric segments index arrays, "
                "e.g. 'note.0.text' or 'component.0.valueQuantity.value'.",
            },
            "value_json": {
                **_STR,
                "description": "The value as a JSON string (e.g. '\"final\"', '72', "
                '\'{"reference":"urn:uuid:..."}\').',
            },
        },
        ["path", "value_json"],
    ),
    _tool(
        "validate_draft",
        "Validate the current draft through the full cascade and return any "
        "remaining blocking issues, so you can fix them before finishing.",
        {},
        [],
    ),
    _tool(
        "finish",
        "Signal that the draft is complete. Call this once every fact the note "
        "supports has been added and validate_draft looks acceptable.",
        {},
        [],
    ),
]

_KNOWN_TOOL_NAMES = frozenset(_HANDLERS)
_SCHEMA_TOOL_NAMES = {schema["function"]["name"] for schema in TOOL_SCHEMAS}
assert _SCHEMA_TOOL_NAMES == _KNOWN_TOOL_NAMES


async def dispatch_tool(name: str, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    """Run the named tool, or report that the model asked for an unknown one."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return ToolOutcome(
            ok=False,
            content={"ok": False, "errors": [f"unknown tool '{name}'"]},
            error=f"unknown tool '{name}'",
        )
    return await handler(ctx, args)


__all__ = [
    "TOOL_SCHEMAS",
    "ToolContext",
    "ToolOutcome",
    "dispatch_tool",
]
