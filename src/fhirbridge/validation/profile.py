"""L2 profile validation against the validator sidecar (AGENTS.md 10).

Semantics follow FHIR ``$validate``: caller-supplied ``profiles`` apply to the
*root* resource, and for a Bundle each entry's conformance is driven by its own
``meta.profile``. Passing a resource profile alongside a Bundle root is a caller
mistake, so we say so rather than silently validating nothing useful.

This layer is blocking on errors, and it fails closed: if the sidecar is
unreachable, or if it cannot resolve a requested profile, the whole validation
fails rather than reporting a pass we cannot justify.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from fhirbridge.domain.errors import IgNotLoadedError, ValidatorUnavailableError
from fhirbridge.fhir.validator_client import ValidatorClient, ValidatorIssue
from fhirbridge.validation.models import (
    IssueSeverity,
    LayerResult,
    LayerStatus,
    ValidationIssue,
    ValidationLayer,
)

_LAYER = ValidationLayer.PROFILE
_MAX_ISSUES = 500

_SEVERITY_MAP: dict[str, IssueSeverity] = {
    "fatal": IssueSeverity.FATAL,
    "error": IssueSeverity.ERROR,
    "warning": IssueSeverity.WARNING,
    "information": IssueSeverity.INFORMATION,
    "informational": IssueSeverity.INFORMATION,
    "success": IssueSeverity.INFORMATION,
}


async def validate_profile(
    payload: dict[str, Any],
    *,
    client: ValidatorClient,
    profiles: Sequence[str] = (),
    resource_type: str | None = None,
) -> LayerResult:
    """Run ``POST /validateResource`` and normalize the outcome.

    Raises :class:`ValidatorUnavailableError` or :class:`IgNotLoadedError`; both
    are fail-closed conditions and must not be swallowed into a passing report.
    """
    started = time.perf_counter()
    notes: list[str] = []

    if resource_type == "Bundle" and profiles:
        notes.append(
            "Caller-supplied profiles are applied to the Bundle itself. Entry-level "
            "conformance is driven by each entry's meta.profile, per FHIR $validate."
        )

    declared = _declared_profiles(payload)
    if declared and not profiles:
        notes.append(
            f"No profiles were supplied; validating against the {len(declared)} profile(s) "
            "declared in meta.profile."
        )

    outcome = await client.validate_resource(payload, profiles=profiles)

    issues = [_translate(issue) for issue in outcome.issues[:_MAX_ISSUES]]
    truncated = len(outcome.issues) - len(issues)
    if truncated > 0:
        issues.append(
            ValidationIssue(
                layer=_LAYER,
                severity=IssueSeverity.INFORMATION,
                code="informational",
                message=f"{truncated} further profile issue(s) were truncated from this report.",
            )
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
    """A skipped L2 result. Only for explicit caller opt-out, never for an outage."""
    return LayerResult(
        layer=_LAYER,
        layer_number=_LAYER.number,
        status=LayerStatus.SKIPPED,
        blocking=True,
        skipped_reason=reason,
    )


def _declared_profiles(payload: dict[str, Any]) -> list[str]:
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return []
    declared = meta.get("profile")
    if not isinstance(declared, list):
        return []
    return [item for item in declared if isinstance(item, str)]


def _translate(issue: ValidatorIssue) -> ValidationIssue:
    return ValidationIssue(
        layer=_LAYER,
        severity=_SEVERITY_MAP.get(issue.severity, IssueSeverity.ERROR),
        code=issue.code,
        message=issue.message,
        expression=issue.expression,
        line=issue.line,
        column=issue.column,
    )


__all__ = ["IgNotLoadedError", "ValidatorUnavailableError", "skipped", "validate_profile"]
