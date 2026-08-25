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

from fhirbridge.version import PROMPT_SET_VERSION


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
        "Method:\n"
        "1. Read the note and set the patient's demographics from what it states.\n"
        "2. For each distinct clinical fact (a vital sign, lab, problem, "
        "medication, or allergy), call the matching add_* tool. When you are not "
        "certain of a code, call search_terminology first and use a returned "
        "system/code/display rather than guessing.\n"
        "3. Use set_element only for details the add_* tools do not cover.\n"
        "4. When you believe the record is complete, call validate_draft and "
        "resolve any blocking issues it reports.\n"
        "5. Call finish once every fact the note supports has been added.\n"
        "\n"
        "Principles: assert only what the source text states; never invent facts, "
        "values, dates, or codes. Omission is always preferable to fabrication. "
        "Use LOINC for labs and vitals, SNOMED CT for problems, RxNorm for "
        "medications, UCUM for units, and ISO 8601 for dates."
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
    "PromptTemplate",
    "prompt_set_fingerprint",
]
