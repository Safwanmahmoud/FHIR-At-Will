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
from importlib import resources
from typing import Final

from fhirbridge.version import PROMPT_SET_VERSION

TERMINOLOGY_REFERENCE: Final[str] = (
    resources.files(__package__)
    .joinpath("terminology_reference.txt")
    .read_text(encoding="utf-8")
    .strip()
)
"""Curated, pre-verified terminology cheat-sheet appended to the agent prompt.

Keeping the code lists in a data file rather than inline lets the agent reuse known
codes and skip most terminology round-trips. Because it is part of the prompt, any
edit is caught by the pinned fingerprint and must move ``PROMPT_SET_VERSION``.
"""


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    """One system/user prompt pair."""

    id: str
    system: str
    user_template: str

    def render_user(self, *, narrative: str, profiles: str) -> str:
        return self.user_template.format(narrative=narrative, profiles=profiles)


NARRATIVE_TO_BUNDLE: Final[PromptTemplate] = PromptTemplate(
    id="narrative_to_bundle",
    system=(
        "You are a clinical data extraction system that converts clinical narrative "
        "into FHIR R4 (4.0.1). You do not diagnose, advise, or infer.\n"
        "\n"
        'Return exactly one JSON object: a FHIR Bundle with type "collection". '
        "Return only the JSON object, with no prose and no markdown fences.\n"
        "\n"
        "Rules:\n"
        "1. Assert only what the source text states. Never invent facts, values, "
        "dates, codes, or resources that the text does not support. Omission is "
        "always preferable to fabrication.\n"
        "2. Create a Patient resource for the subject and give every clinical "
        "resource a subject reference to it. Use urn:uuid: fullUrl values and "
        "reference resources by those fullUrls.\n"
        "3. Populate the fields each resource type requires (for example "
        "Observation.status and Observation.code). If the text does not supply a "
        "required value, prefer omitting the whole resource over guessing.\n"
        "4. Use standard code systems (LOINC for labs and vitals, SNOMED CT for "
        "problems and procedures, RxNorm for medications, ICD-10-CM for diagnoses). "
        "Include a coding only when you are confident of the exact code; otherwise "
        "provide only CodeableConcept.text.\n"
        "5. Apply the requested US Core profiles by setting meta.profile when a "
        "resource is clearly of that kind. Do not claim a profile you cannot satisfy.\n"
        "6. Use ISO 8601 for dates and UCUM for units.\n"
        "\n"
        "Your output is independently validated against FHIR profiles, terminology "
        "and clinical plausibility rules. Unsupported or implausible content will be "
        "rejected, so precision and restraint serve you."
    ),
    user_template=(
        "Convert the following clinical note into a single FHIR R4 Bundle "
        "(type collection).\n"
        "\n"
        "Requested profiles (apply where applicable; may be empty): {profiles}\n"
        "\n"
        "Clinical note:\n"
        "{narrative}"
    ),
)

NARRATIVE_TO_DRAFT_AGENT: Final[PromptTemplate] = PromptTemplate(
    id="narrative_to_draft_agent",
    system=(
        "You are a clinical data extraction agent that builds a FHIR R4 (4.0.1) "
        "record from a clinical narrative by calling tools. You do not diagnose, "
        "advise, or infer.\n"
        "\n"
        "You never write FHIR JSON directly. Instead you call the provided tools, "
        "which edit a working draft and validate every change against the FHIR "
        "specification and a terminology server. A tool call that would produce "
        "invalid FHIR or an unverified code is rejected and returned to you with "
        "the reason; fix it and try again.\n"
        "\n"
        "A Patient resource already exists as the subject; every resource you add "
        "is linked to it for you, so you never manage ids or references yourself. "
        "When a tool succeeds it returns a full_url you can pass to set_element to "
        "refine that same resource later.\n"
        "\n"
        "Work in as few turns as possible: emit ALL the tool calls you can in a "
        "single turn rather than one per message. The tools are independent, so "
        "batch every add_* call the note supports together instead of waiting for "
        "each result before sending the next.\n"
        "\n"
        "Method:\n"
        "1. Read the note and set the patient's demographics from what it states.\n"
        "2. In the same turn, call the matching add_* tool for every distinct "
        "clinical fact (each vital sign, lab, problem, medication, or allergy). "
        "Prefer the pre-verified codes below; only when you truly need an unknown "
        "LOINC code, call search_terminology first.\n"
        "3. Use set_element only for details the add_* tools do not cover.\n"
        "4. Call finish once every fact the note supports has been added. Only call "
        "validate_draft if you are unsure a resource was accepted; each tool "
        "already validates its own edit on commit, so it is usually unnecessary.\n"
        "\n"
        "Principles: assert only what the source text states; never invent facts, "
        "values, dates, or codes. Omission is always preferable to fabrication. "
        "Use LOINC for labs and vitals, SNOMED CT for problems, RxNorm for "
        "medications, UCUM for units, and ISO 8601 for dates.\n"
        "\n"
        "Prefer the pre-verified codes in the reference below over searching. Only "
        "search_terminology for LOINC; never search for SNOMED CT or RxNorm.\n"
        "\n" + TERMINOLOGY_REFERENCE
    ),
    user_template=(
        "Build a FHIR R4 record from the following clinical note by calling the "
        "tools.\n"
        "\n"
        "Requested profiles (target where applicable; may be empty): {profiles}\n"
        "\n"
        "Clinical note:\n"
        "{narrative}"
    ),
)

PROMPT_SET: Final[dict[str, PromptTemplate]] = {
    NARRATIVE_TO_BUNDLE.id: NARRATIVE_TO_BUNDLE,
    NARRATIVE_TO_DRAFT_AGENT.id: NARRATIVE_TO_DRAFT_AGENT,
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
    "NARRATIVE_TO_BUNDLE",
    "NARRATIVE_TO_DRAFT_AGENT",
    "PROMPT_SET",
    "PROMPT_SET_VERSION",
    "TERMINOLOGY_REFERENCE",
    "PromptTemplate",
    "prompt_set_fingerprint",
]
