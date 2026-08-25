"""The prompt set is a pinned artifact (principle 2.8).

A verdict names the prompt set that produced it by version. If a template can be
edited without the version moving, that name is a lie. This test pins the content
hash: any edit to a prompt fails here until the author bumps both the hash and
``PROMPT_SET_VERSION``, which forces the change through review.
"""

from __future__ import annotations

from fhirbridge.llm.prompts import (
    NARRATIVE_TO_BUNDLE,
    NARRATIVE_TO_DRAFT_AGENT,
    PROMPT_SET,
    PROMPT_SET_VERSION,
    prompt_set_fingerprint,
)

PINNED_FINGERPRINT = "d4a2c15f47ce0b1c21abfabfc32178bb6cce280d5b0dbecc234f81c8f9027959"


def test_the_prompt_set_has_not_drifted_from_its_pinned_hash() -> None:
    assert prompt_set_fingerprint() == PINNED_FINGERPRINT, (
        "A prompt template changed. Bump PROMPT_SET_VERSION and update PINNED_FINGERPRINT "
        "to the value printed by prompt_set_fingerprint()."
    )


def test_the_fingerprint_is_deterministic() -> None:
    assert prompt_set_fingerprint() == prompt_set_fingerprint()


def test_the_version_is_stamped_and_the_set_is_populated() -> None:
    assert PROMPT_SET_VERSION
    assert NARRATIVE_TO_BUNDLE.id in PROMPT_SET
    assert NARRATIVE_TO_DRAFT_AGENT.id in PROMPT_SET


def test_the_user_template_renders_narrative_and_profiles() -> None:
    rendered = NARRATIVE_TO_BUNDLE.render_user(narrative="chest pain", profiles="us-core-patient")

    assert "chest pain" in rendered
    assert "us-core-patient" in rendered
