"""The extraction rule pack.

A rule is only worth its tokens if the model can act on it and the pipeline can carry
the result. Two failure modes are specific to this design and are what most of these
tests exist for:

- a rule that names an element assembly cannot build, so following it produces nothing;
- a rule that licenses a guess, which is the behavior the deterministic assembler was
  built to remove.
"""

from __future__ import annotations

import re

import pytest

from fhirbridge.fhir.assemble import resolve_datatype
from fhirbridge.llm.extraction_rules import (
    AGE_IS_NOT_A_BIRTH_DATE,
    EXTRACTION_RULES,
    NEGATED_AND_ATTRIBUTED_FINDINGS,
    ExtractionRule,
    extraction_rules_text,
)
from fhirbridge.llm.nar2fhir import OBSERVED_RESOURCE_KEYS
from fhirbridge.llm.prompts import NARRATIVE_TO_ENTITIES

RULE_ID = re.compile(r"^[a-z][a-z0-9-]*$")

# Only matches a dotted path whose prefix is a real resource type, so prose is ignored.
ELEMENT_PATH = re.compile(r"\b([A-Z][A-Za-z]+)\.([a-zA-Z][a-zA-Z0-9]*)\b")

COERCIBLE_ELEMENTS = {
    f"{resource_type}.{key}"
    for resource_type, keys in OBSERVED_RESOURCE_KEYS.items()
    for key in keys
    if resolve_datatype(resource_type, key)[0] not in ("unknown",)
}


@pytest.fixture(params=EXTRACTION_RULES, ids=lambda rule: rule.id)
def rule(request: pytest.FixtureRequest) -> ExtractionRule:
    return request.param


class TestPackShape:
    def test_the_pack_is_populated(self) -> None:
        assert len(EXTRACTION_RULES) >= 1

    def test_rule_ids_are_unique(self) -> None:
        ids = [rule.id for rule in EXTRACTION_RULES]

        assert len(ids) == len(set(ids))

    def test_every_rule_is_fully_populated(self, rule: ExtractionRule) -> None:
        assert RULE_ID.match(rule.id), rule.id
        assert rule.title.strip()
        assert rule.guidance.strip()
        assert rule.rationale.strip(), "a rule without a rationale cannot be reviewed"


class TestRulesMatchThePipeline:
    """A rule that the catalog or the assembler cannot honor is worse than no rule."""

    def test_declared_elements_are_in_the_catalog(self, rule: ExtractionRule) -> None:
        for path in rule.elements:
            resource_type, _, element = path.partition(".")
            allowed = OBSERVED_RESOURCE_KEYS.get(resource_type)
            assert allowed is not None, f"{rule.id} names unknown resource type {resource_type}"
            assert element in allowed, f"{rule.id} names {path}, which is not in the catalog"

    def test_declared_elements_are_datatypes_assembly_can_build(self, rule: ExtractionRule) -> None:
        """Observation.component is the cautionary case: a backbone element is dropped."""
        for path in rule.elements:
            resource_type, _, element = path.partition(".")
            datatype, _ = resolve_datatype(resource_type, element)
            assert datatype != "unknown", f"{rule.id} steers at {path}, which has no datatype"
            assert path in COERCIBLE_ELEMENTS, f"{rule.id} steers at {path}, which is dropped"

    def test_guidance_never_cites_an_element_outside_the_catalog(
        self, rule: ExtractionRule
    ) -> None:
        for resource_type, element in ELEMENT_PATH.findall(rule.guidance):
            allowed = OBSERVED_RESOURCE_KEYS.get(resource_type)
            if allowed is None:
                continue
            assert element in allowed, (
                f"{rule.id} guidance cites {resource_type}.{element}, which the catalog "
                "does not allow; the whole extraction would be rejected"
            )


class TestRendering:
    def test_every_title_and_guidance_reaches_the_prompt(self, rule: ExtractionRule) -> None:
        rendered = extraction_rules_text()

        assert rule.title in rendered
        for line in rule.guidance.splitlines():
            assert line.strip() in rendered

    def test_rationales_are_never_sent_to_the_model(self, rule: ExtractionRule) -> None:
        """Reviewer context would only spend tokens, and editing it must stay free."""
        assert rule.rationale not in extraction_rules_text()
        assert rule.rationale not in NARRATIVE_TO_ENTITIES.system

    def test_the_rules_are_numbered_in_declaration_order(self) -> None:
        rendered = extraction_rules_text()
        positions = [rendered.index(f"{index}. ") for index in range(1, len(EXTRACTION_RULES) + 1)]

        assert positions == sorted(positions)

    def test_the_pack_is_embedded_in_the_extraction_prompt(self) -> None:
        assert extraction_rules_text() in NARRATIVE_TO_ENTITIES.system


class TestSafetyCriticalContent:
    """These two rules prevent confident, undetectable clinical errors.

    Pinned by content because weakening either one produces a conformant resource
    that no downstream layer can flag: a birth date nobody stated, or a diagnosis
    the patient does not have.
    """

    def test_the_age_rule_forbids_computing_a_birth_date(self) -> None:
        guidance = AGE_IS_NOT_A_BIRTH_DATE.guidance

        assert "never compute a birth date from" in guidance
        assert "Patient.birthDate" in guidance
        assert "`Age`" in guidance, "the rule must say where the age does go"

    def test_the_age_rule_routes_the_age_somewhere_it_survives(self) -> None:
        """Refusing the birthDate is only half a fix; the number must be kept."""
        assert "Observation.code" in AGE_IS_NOT_A_BIRTH_DATE.elements
        assert "Observation.valueQuantity" in AGE_IS_NOT_A_BIRTH_DATE.elements

    def test_family_history_is_suppressed_rather_than_recorded_as_a_condition(self) -> None:
        guidance = NEGATED_AND_ATTRIBUTED_FINDINGS.guidance

        assert "family member" in guidance
        assert "emit nothing" in guidance

    def test_a_denial_is_recorded_as_refuted_rather_than_dropped(self) -> None:
        guidance = NEGATED_AND_ATTRIBUTED_FINDINGS.guidance

        assert "`refuted`" in guidance
        assert "Condition.verificationStatus" in NEGATED_AND_ATTRIBUTED_FINDINGS.elements
