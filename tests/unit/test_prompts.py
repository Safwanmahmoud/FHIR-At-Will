"""The prompt set is a pinned artifact (principle 2.8).

A verdict names the prompt set that produced it by version. If a template can be
edited without the version moving, that name is a lie. This test pins the content
hash: any edit to a prompt fails here until the author bumps both the hash and
``PROMPT_SET_VERSION``, which forces the change through review.
"""

from __future__ import annotations

from fhirbridge.llm.prompts import (
    NARRATIVE_TO_ENTITIES,
    PROMPT_SET,
    PROMPT_SET_VERSION,
    prompt_set_fingerprint,
)

PINNED_FINGERPRINT = "baf6e460afb924d2a5a7799ace234ce70ce68900dc04f9c0d2df75d2f1d6a325"


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


def test_no_generation_prompt_remains() -> None:
    """Bundle assembly is deterministic; a second model call would reintroduce drift."""
    assert len(PROMPT_SET) == 1
