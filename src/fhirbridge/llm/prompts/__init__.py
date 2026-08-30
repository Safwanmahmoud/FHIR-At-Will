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

from fhirbridge.llm.nar2fhir import DATATYPE_LEGEND, resource_catalog_text
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

PROMPT_SET: Final[dict[str, PromptTemplate]] = {
    NARRATIVE_TO_ENTITIES.id: NARRATIVE_TO_ENTITIES,
    ENTITIES_TO_FHIR_BUNDLE.id: ENTITIES_TO_FHIR_BUNDLE,
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
    "NARRATIVE_TO_ENTITIES",
    "PROMPT_SET",
    "PROMPT_SET_VERSION",
    "PromptTemplate",
    "prompt_set_fingerprint",
]
