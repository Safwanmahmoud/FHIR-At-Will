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

from fhirbridge.llm.nar2fhir import DATATYPE_LEGEND, resource_catalog_text
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

    def render_user(self, **values: str) -> str:
        return self.user_template.format(**values)


NARRATIVE_TO_ENTITIES: Final[PromptTemplate] = PromptTemplate(
    id="nar2fhir_extract_entities",
    system=(
        "Extract every explicitly stated clinical and demographic entity from the "
        "narrative. Return exactly one JSON object containing only an `entities` array. "
        "Each item must contain exactly `resourceType`, `keyword`, and `value`.\n\n"
        "`resourceType` must be an exact resource type from the catalog. `keyword` must "
        "be an exact allowed key under that same resource type. `value` must preserve "
        "the source wording or be minimally normalized.\n\n"
        "Create a separate item for every entity, even when items share a key or type. "
        "Use each description to choose the resource whose purpose matches the fact. "
        "Choose the most specific allowed key. Never infer missing facts, return FHIR "
        "paths, or emit administrative keys such as resourceType, id, meta, or text.\n\n"
        "FHIR R4 resource catalog with observed keys:\n" + resource_catalog_text()
    ),
    user_template=(
        "Extract grounded FHIR entities from this clinical narrative.\n\n"
        "Clinical narrative:\n{narrative}"
    ),
)

ENTITIES_TO_FHIR_BUNDLE: Final[PromptTemplate] = PromptTemplate(
    id="nar2fhir_entities_to_bundle",
    system=(
        "You are a FHIR R4 (4.0.1) assembly system. Convert a clinical narrative and "
        "its grounded extracted entities into valid FHIR resources. Return exactly one "
        'JSON object: {"resourceType":"Bundle","type":"collection","entry":[...]}. '
        "Return JSON only, without prose or markdown.\n\n"
        "Rules:\n"
        "1. Build one resource per distinct real-world instance; do not collapse "
        "separate events merely because they share a resource type.\n"
        "2. Use only the supplied fields for each resource type and omit unsupported "
        "facts. Always include resourceType on every resource.\n"
        "3. Represent fields using their declared FHIR datatype, never a bare string "
        "where an object or array is required.\n"
        "4. For coded concepts, use CodeableConcept.text. Add Coding.system and "
        "Coding.code only when the narrative explicitly supplies an exact code; never "
        "invent codes.\n"
        "5. Give each entry a unique urn:uuid fullUrl and link resources using those "
        "fullUrls. Do not invent external identifiers.\n"
        "6. Use ISO 8601 dates and UCUM quantities when stated. Omit resources whose "
        "required fields cannot be grounded.\n"
        "7. Apply requested profiles only when the generated resource satisfies them.\n\n"
        "Common datatype shapes:\n" + DATATYPE_LEGEND
    ),
    user_template=(
        "Requested profiles (may be empty): {profiles}\n\n"
        "Allowed resource fields and datatypes:\n{field_reference}\n\n"
        "Clinical narrative:\n{narrative}\n\n"
        "Grounded extracted entities:\n{entities}"
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
    NARRATIVE_TO_ENTITIES.id: NARRATIVE_TO_ENTITIES,
    ENTITIES_TO_FHIR_BUNDLE.id: ENTITIES_TO_FHIR_BUNDLE,
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
    "ENTITIES_TO_FHIR_BUNDLE",
    "NARRATIVE_TO_DRAFT_AGENT",
    "NARRATIVE_TO_ENTITIES",
    "PROMPT_SET",
    "PROMPT_SET_VERSION",
    "TERMINOLOGY_REFERENCE",
    "PromptTemplate",
    "prompt_set_fingerprint",
]
