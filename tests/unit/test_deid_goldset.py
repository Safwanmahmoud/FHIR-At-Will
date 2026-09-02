"""Small synthetic direct-identifier recall gate."""

from __future__ import annotations

import pytest

from fhirbridge.deid.detectors import DeclaredIdentifier
from fhirbridge.deid.minimize import minimize
from fhirbridge.deid.policy import DeidMode, DeidPolicy, DeidProfile
from fhirbridge.deid.spans import IdentifierClass

pytestmark = pytest.mark.golden

CASES = (
    ("Jane Smith was seen on 01/02/2026.", "Jane Smith", IdentifierClass.NAME),
    ("Call 617-555-0101 for follow-up.", "617-555-0101", IdentifierClass.PHONE),
    ("MRN: A12345 has a new result.", "MRN: A12345", IdentifierClass.MRN),
    ("Send to jane@example.com.", "jane@example.com", IdentifierClass.EMAIL),
    ("SSN 123-45-6789 was copied in error.", "123-45-6789", IdentifierClass.SSN),
)


@pytest.mark.parametrize(("narrative", "identifier", "identifier_class"), CASES)
def test_direct_identifier_is_absent_from_enforced_egress(
    narrative: str,
    identifier: str,
    identifier_class: IdentifierClass,
) -> None:
    result = minimize(
        narrative,
        policy=DeidPolicy(
            mode=DeidMode.ENFORCED,
            profile=DeidProfile.HIPAA_SAFE_HARBOR,
            allow_audio_egress=False,
        ),
        declared=[DeclaredIdentifier(identifier_class, identifier)],
    )

    assert identifier.casefold() not in result.safe_text.casefold()
