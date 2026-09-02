"""Deterministic PHI minimization and reversible-vault tests."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from fhirbridge.deid.detectors import (
    DeclaredIdentifier,
    DeclaredIdentifierDetector,
    PatternDetector,
    UnknownProperNounDetector,
)
from fhirbridge.deid.minimize import minimize
from fhirbridge.deid.policy import DeidMode, DeidPolicy, DeidProfile
from fhirbridge.deid.spans import IdentifierClass, Span, resolve_overlaps
from fhirbridge.deid.vault import Vault


def policy(mode: DeidMode = DeidMode.ENFORCED) -> DeidPolicy:
    return DeidPolicy(
        mode=mode,
        profile=DeidProfile.HIPAA_SAFE_HARBOR,
        allow_audio_egress=False,
    )


def test_longest_overlapping_span_wins() -> None:
    spans = resolve_overlaps(
        [
            Span(0, 4, IdentifierClass.NAME, "short"),
            Span(0, 10, IdentifierClass.NAME, "long"),
            Span(12, 15, IdentifierClass.DATE, "other"),
        ]
    )

    assert [(span.start, span.end) for span in spans] == [(0, 10), (12, 15)]


def test_declared_name_variants_are_detected_case_insensitively() -> None:
    detector = DeclaredIdentifierDetector([DeclaredIdentifier(IdentifierClass.NAME, "Jane Smith")])

    spans = detector.detect("SMITH JANE saw J. Smith and Jane Smith's chart.")

    assert len(spans) == 3


def test_safe_harbor_patterns_detect_dates_ssn_email_and_old_age() -> None:
    text = "Born 01/02/1930, age 94 years old, SSN 123-45-6789, email jane@example.com."

    classes = {
        span.identifier_class
        for span in PatternDetector(DeidProfile.HIPAA_SAFE_HARBOR).detect(text)
    }

    assert {
        IdentifierClass.DATE,
        IdentifierClass.AGE,
        IdentifierClass.SSN,
        IdentifierClass.EMAIL,
    } <= classes


def test_limited_data_set_keeps_dates_and_zip_codes() -> None:
    text = "Seen 01/02/2026 in 02139; email jane@example.com."

    classes = {
        span.identifier_class
        for span in PatternDetector(DeidProfile.HIPAA_LIMITED_DATA_SET).detect(text)
    }

    assert IdentifierClass.DATE not in classes
    assert IdentifierClass.ZIP not in classes
    assert IdentifierClass.EMAIL in classes


def test_clinical_eponyms_are_not_mistaken_for_names() -> None:
    detector = UnknownProperNounDetector()

    spans = detector.detect("Parkinson disease and Bell palsy.")

    assert not spans


def test_enforced_minimization_replaces_and_restores_declared_phi() -> None:
    narrative = "Jane Smith has MRN A12345 and was seen 01/02/2026."
    declared = [
        DeclaredIdentifier(IdentifierClass.NAME, "Jane Smith"),
        DeclaredIdentifier(IdentifierClass.MRN, "A12345"),
    ]
    result = minimize(narrative, policy=policy(), declared=declared)

    assert "Jane Smith" not in result.safe_text
    assert "01/02/2026" not in result.safe_text
    assert "[[NAME_" in result.safe_text
    assert result.vault.restore(result.safe_text) == narrative
    assert result.report().residual_risk == "not_assessed"


def test_advisory_mode_detects_but_does_not_transform() -> None:
    narrative = "Jane Smith called 617-555-0101."
    result = minimize(
        narrative,
        policy=policy(DeidMode.ADVISORY),
        declared=[DeclaredIdentifier(IdentifierClass.NAME, "Jane Smith")],
    )

    assert result.safe_text == narrative
    assert result.detections
    assert not result.applied


@given(st.text(alphabet=st.characters(exclude_characters="[]"), max_size=100))
def test_vault_round_trip_is_identity(value: str) -> None:
    vault = Vault()
    token = vault.surrogate_for(value, IdentifierClass.OTHER)

    assert vault.restore(f"before {token} after") == f"before {value} after"
