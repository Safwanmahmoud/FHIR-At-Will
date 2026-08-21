"""Building FHIR ``OperationOutcome`` responses (AGENTS.md 12).

Nothing in this module may carry PHI. ``diagnostics`` is a developer-authored
sentence and ``safe_context`` is identifier-only. Clinical detail belongs in
response bodies such as the validation report, never in an error outcome.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from fhirbridge.domain.errors import ERROR_CODE_SYSTEM, FhirbridgeError, SafeContext

FHIR_JSON_MEDIA_TYPE: Final[str] = "application/fhir+json"
TRACE_ID_EXTENSION: Final[str] = "https://fhirbridge.org/StructureDefinition/trace-id"
ERROR_CODE_EXTENSION: Final[str] = "https://fhirbridge.org/StructureDefinition/error-code"
DOCS_EXTENSION: Final[str] = "https://fhirbridge.org/StructureDefinition/documentation"


def issue(
    *,
    severity: str,
    code: str,
    text: str,
    diagnostics: str | None = None,
    expression: Sequence[str] | None = None,
    machine_code: str | None = None,
) -> dict[str, Any]:
    """Build a single ``OperationOutcome.issue``."""
    details: dict[str, Any] = {"text": text}
    if machine_code is not None:
        details["coding"] = [{"system": ERROR_CODE_SYSTEM, "code": machine_code}]

    result: dict[str, Any] = {"severity": severity, "code": code, "details": details}
    if diagnostics:
        result["diagnostics"] = diagnostics
    if expression:
        result["expression"] = list(expression)
    return result


def operation_outcome(
    issues: Sequence[dict[str, Any]],
    *,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Wrap issues into an ``OperationOutcome``. At least one issue is required by FHIR."""
    if not issues:
        issues = [
            issue(
                severity="information",
                code="informational",
                text="No issues reported.",
            )
        ]
    outcome: dict[str, Any] = {"resourceType": "OperationOutcome", "issue": list(issues)}
    if trace_id:
        outcome["extension"] = [{"url": TRACE_ID_EXTENSION, "valueString": trace_id}]
    return outcome


def _render_safe_context(context: SafeContext) -> str | None:
    if not context:
        return None
    return "; ".join(f"{key}={value}" for key, value in sorted(context.items()))


def outcome_for_error(
    error: FhirbridgeError,
    *,
    trace_id: str | None = None,
    documentation_url: str | None = None,
) -> dict[str, Any]:
    """Render a domain error as an ``OperationOutcome``."""
    diagnostics = error.detail
    context = _render_safe_context(error.safe_context)
    if context:
        diagnostics = f"{diagnostics} [{context}]"

    outcome = operation_outcome(
        [
            issue(
                severity="error",
                code=error.spec.issue_type,
                text=error.spec.title,
                diagnostics=diagnostics,
                machine_code=str(error.code),
            )
        ],
        trace_id=trace_id,
    )
    if documentation_url:
        extensions: list[dict[str, Any]] = list(outcome.get("extension", []))
        extensions.append({"url": DOCS_EXTENSION, "valueUrl": documentation_url})
        outcome["extension"] = extensions
    return outcome


def platform_error_envelope(
    error: FhirbridgeError,
    *,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Render a platform error as the ``{"error": {...}}`` JSON envelope."""
    body: dict[str, Any] = {
        "error": {
            "code": str(error.code),
            "message": error.detail,
            "trace_id": trace_id,
            "details": dict(error.safe_context),
        }
    }
    return body


__all__ = [
    "DOCS_EXTENSION",
    "ERROR_CODE_EXTENSION",
    "FHIR_JSON_MEDIA_TYPE",
    "TRACE_ID_EXTENSION",
    "issue",
    "operation_outcome",
    "outcome_for_error",
    "platform_error_envelope",
]
