"""Narrative minimization orchestration and its gateway attestation."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from fhirbridge.deid.detectors import DeclaredIdentifier, Detector, build_detectors
from fhirbridge.deid.policy import DeidMode, DeidPolicy
from fhirbridge.deid.spans import resolve_overlaps
from fhirbridge.deid.vault import Vault
from fhirbridge.observability import metrics
from fhirbridge.version import DEID_RULESET_VERSION


@dataclass(frozen=True, slots=True)
class DeidReport:
    mode: str
    profile: str
    ruleset_version: str
    detections: dict[str, int] = field(default_factory=dict)
    replacements: int = 0
    restored: int = 0
    residual_risk: str = "not_assessed"


@dataclass(slots=True)
class Minimization:
    """Proof and state for one provider call; contains PHI and stays internal."""

    safe_text: str
    policy: DeidPolicy
    vault: Vault
    detections: dict[str, int]
    applied: bool
    restored: int = 0

    def assert_safe_payload(self, payload: Any) -> None:
        if self.policy.enforced and not self.applied:
            from fhirbridge.domain.errors import PhiMinimizationRequiredError

            raise PhiMinimizationRequiredError()
        if self.policy.enforced:
            self.vault.assert_originals_absent(payload)

    def restore_entities(self, entities: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
        restored_entities: list[dict[str, str]] = []
        restored_count = 0
        for entity in entities:
            value = entity["value"]
            restored = self.vault.restore(value)
            restored_count += int(restored != value)
            self.vault.assert_surrogates_absent(restored)
            restored_entities.append({**entity, "value": restored})
        self.restored = restored_count
        metrics.DEID_REVERSALS.labels(outcome="success").inc()
        return restored_entities

    def report(self) -> DeidReport:
        return DeidReport(
            mode=str(self.policy.mode),
            profile=str(self.policy.profile),
            ruleset_version=DEID_RULESET_VERSION,
            detections=dict(sorted(self.detections.items())),
            replacements=self.vault.size,
            restored=self.restored,
        )

    def close(self) -> None:
        self.vault.clear()


def minimize(
    text: str,
    *,
    policy: DeidPolicy,
    declared: Sequence[DeclaredIdentifier] = (),
    detectors: Sequence[Detector] | None = None,
) -> Minimization:
    metrics.DEID_RUNS.labels(mode=str(policy.mode), profile=str(policy.profile)).inc()
    if policy.mode is DeidMode.OFF:
        return Minimization(
            safe_text=text,
            policy=policy,
            vault=Vault(),
            detections={},
            applied=False,
        )

    active = (
        tuple(detectors) if detectors is not None else build_detectors(policy.profile, declared)
    )
    spans = resolve_overlaps(span for detector in active for span in detector.detect(text))
    counts = Counter(str(span.identifier_class) for span in spans)
    for identifier_class, count in counts.items():
        metrics.DEID_DETECTIONS.labels(identifier_class=identifier_class).inc(count)

    if policy.mode is DeidMode.ADVISORY:
        return Minimization(
            safe_text=text,
            policy=policy,
            vault=Vault(),
            detections=dict(counts),
            applied=False,
        )

    vault = Vault()
    safe = text
    for span in reversed(spans):
        original = text[span.start : span.end]
        surrogate = vault.surrogate_for(original, span.identifier_class)
        safe = f"{safe[: span.start]}{surrogate}{safe[span.end :]}"
    return Minimization(
        safe_text=safe,
        policy=policy,
        vault=vault,
        detections=dict(counts),
        applied=True,
    )


__all__ = ["DeidReport", "Minimization", "minimize"]
