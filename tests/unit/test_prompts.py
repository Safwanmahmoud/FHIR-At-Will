"""The prompt set is a pinned artifact (principle 2.8).

A verdict names the prompt set that produced it by version. If a template can be
edited without the version moving, that name is a lie. This test pins the content
hash: any edit to a prompt fails here until the author bumps both the hash and
``PROMPT_SET_VERSION``, which forces the change through review.
"""

from __future__ import annotations

from fhirbridge.llm.extraction_rules import extraction_rules_text
from fhirbridge.llm.prompts import (
    DICTATION_TRANSCRIBE,
    NARRATIVE_TO_ENTITIES,
    PROMPT_SET,
    PROMPT_SET_VERSION,
    prompt_set_fingerprint,
)

PINNED_FINGERPRINT = "b24932e90102a717cb300e8ab09f03c03d314458e94d4ba25e472704b2d4fb97"


def test_the_prompt_set_has_not_drifted_from_its_pinned_hash() -> None:
    assert prompt_set_fingerprint() == PINNED_FINGERPRINT, (
        "A prompt template changed. Bump PROMPT_SET_VERSION and update PINNED_FINGERPRINT "
        "to the value printed by prompt_set_fingerprint()."
    )


def test_the_fingerprint_is_deterministic() -> None:
    assert prompt_set_fingerprint() == prompt_set_fingerprint()


def test_the_version_is_stamped_and_the_set_is_populated() -> None:
    assert PROMPT_SET_VERSION
    assert NARRATIVE_TO_ENTITIES.id in PROMPT_SET
    assert DICTATION_TRANSCRIBE.id in PROMPT_SET


def test_the_user_template_renders_the_narrative() -> None:
    rendered = NARRATIVE_TO_ENTITIES.render_user(narrative="chest pain")

    assert "chest pain" in rendered


def test_extraction_asks_for_the_instance_grouping_key() -> None:
    """Deterministic assembly cannot group without it, so the prompt must demand it."""
    system = NARRATIVE_TO_ENTITIES.system

    assert "`instance`" in system
    assert "resourceType`, `instance`, `keyword`, and `value`" in system


def test_extraction_forbids_identifying_detail_in_the_grouping_key() -> None:
    """The slug is model-authored text that ends up in the response body."""
    system = NARRATIVE_TO_ENTITIES.system

    assert "must not contain a patient name" in system


def test_no_bundle_generation_prompt_remains() -> None:
    """Bundle assembly is deterministic; a model call for it would reintroduce drift.

    The set holds only the two model-facing prompts: grounded extraction and verbatim
    dictation. Neither produces a Bundle.
    """
    assert set(PROMPT_SET) == {NARRATIVE_TO_ENTITIES.id, DICTATION_TRANSCRIBE.id}


def test_the_dictation_prompt_protects_negation_and_refuses_to_interpret() -> None:
    """A mistranscribed negation or number silently corrupts the chart."""
    system = DICTATION_TRANSCRIBE.system

    assert "verbatim" in system
    assert "negation" in system
    assert "interpretation" in system


def test_the_extraction_rule_pack_is_embedded() -> None:
    """Rules live in their own module but must be pinned by this fingerprint."""
    assert "Extraction rules" in NARRATIVE_TO_ENTITIES.system
    assert extraction_rules_text() in NARRATIVE_TO_ENTITIES.system


def test_every_prompt_is_ascii() -> None:
    """Smart quotes and dashes tokenize unpredictably and creep in via copy-paste."""
    for template in PROMPT_SET.values():
        offenders = sorted({char for char in template.system if ord(char) > 127})
        assert not offenders, f"{template.id} system prompt contains {offenders}"
        offenders = sorted({char for char in template.user_template if ord(char) > 127})
        assert not offenders, f"{template.id} user template contains {offenders}"


def test_the_rules_come_before_the_catalog() -> None:
    """The catalog is long; guidance buried after it is easy for a model to lose."""
    system = NARRATIVE_TO_ENTITIES.system

    assert system.index("Extraction rules") < system.index("FHIR R4 resource catalog")
