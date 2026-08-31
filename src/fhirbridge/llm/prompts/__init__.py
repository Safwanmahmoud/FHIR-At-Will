"""The hash-pinned prompt template set (AGENTS.md 9, principle 2.8).

A verdict is only reproducible if the prompt that produced it is pinned. The
human-facing pin is :data:`~fhirbridge.version.PROMPT_SET_VERSION`, stamped into
every report; :func:`prompt_set_fingerprint` is the machine check that the
templates below have not drifted from the version they claim to be. A unit test
asserts the fingerprint against a committed constant, so any edit to a prompt
fails CI until the author bumps the version and the pin together.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final

from fhirbridge.llm.extraction_rules import extraction_rules_text
from fhirbridge.llm.nar2fhir import resource_catalog_text
from fhirbridge.version import PROMPT_SET_VERSION


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    """One system/user prompt pair."""

    id: str
    system: str
    user_template: str

    def render_user(self, **values: str) -> str:
        return self.user_template.format(**values)


NARRATIVE_TO_ENTITIES: Final[PromptTemplate] = PromptTemplate(
    id="nar2fhir_extract_entities",
    system=(
        "Extract every explicitly stated clinical and demographic entity from the "
        "narrative. Return exactly one JSON object containing only an `entities` array. "
        "Each item must contain exactly `resourceType`, `instance`, `keyword`, and "
        "`value`.\n\n"
        "`resourceType` must be an exact resource type from the catalog. `keyword` must "
        "be an exact allowed key under that same resource type. `value` must preserve "
        "the source wording or be minimally normalized.\n\n"
        "`instance` identifies which single real-world thing a fact describes. Use a "
        "short slug of lowercase letters, digits, and hyphens, reused by every key "
        "belonging to that same thing, for example `patient-1`, `obs-blood-pressure`, "
        "`obs-heart-rate`, `med-metformin`. Name the kind of thing, never the person: an "
        "`instance` must not contain a patient name, an identifier, a date, or any other "
        "identifying detail.\n\n"
        "Create a separate item for every entity, even when items share a key or type. "
        "Use each description to choose the resource whose purpose matches the fact. "
        "Choose the most specific allowed key. Never infer missing facts, return FHIR "
        "paths, or emit administrative keys such as resourceType, id, meta, or text.\n\n"
        "Extraction rules. These override the general guidance above where they "
        "conflict:\n\n" + extraction_rules_text() + "\n\n"
        "FHIR R4 resource catalog with observed keys:\n" + resource_catalog_text()
    ),
    user_template=(
        "Extract grounded FHIR entities from this clinical narrative.\n\n"
        "Clinical narrative:\n{narrative}"
    ),
)

DICTATION_TRANSCRIBE: Final[PromptTemplate] = PromptTemplate(
    id="voice2fhir_dictation_transcribe",
    system=(
        "You are a medical dictation transcriber. Transcribe the supplied audio verbatim "
        "into plain text.\n\n"
        "Output only the words that were spoken. Do not translate, summarize, paraphrase, "
        "correct, or add any clinical interpretation, and do not add speaker labels, "
        "timestamps, headings, or commentary of your own.\n\n"
        "Preserve clinically decisive words exactly, negation and quantities above all: "
        "`no`, `not`, `denies`, `without`, and every number and unit change the meaning of "
        "a chart and must never be dropped or altered. If a stretch of audio is "
        "unintelligible, write `[inaudible]` rather than guessing at the words. If there is "
        "no discernible speech, return an empty string."
    ),
    # The audio is supplied by the gateway as a separate content part, not through
    # this template, so there is nothing to interpolate here.
    user_template="",
)

PROMPT_SET: Final[dict[str, PromptTemplate]] = {
    NARRATIVE_TO_ENTITIES.id: NARRATIVE_TO_ENTITIES,
    DICTATION_TRANSCRIBE.id: DICTATION_TRANSCRIBE,
}


def prompt_set_fingerprint() -> str:
    """A stable content hash over every template in the set.

    Independent of dict ordering: templates are hashed in sorted id order so the
    fingerprint depends only on content, never on insertion order.
    """
    digest = hashlib.sha256()
    for template in sorted(PROMPT_SET.values(), key=lambda item: item.id):
        digest.update(template.id.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(template.system.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(template.user_template.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


__all__ = [
    "DICTATION_TRANSCRIBE",
    "NARRATIVE_TO_ENTITIES",
    "PROMPT_SET",
    "PROMPT_SET_VERSION",
    "PromptTemplate",
    "prompt_set_fingerprint",
]
