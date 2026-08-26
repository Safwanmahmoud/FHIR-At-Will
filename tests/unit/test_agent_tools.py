"""The craft agent's tools are the safety boundary (principle 2.3).

These tests assert the property the whole design rests on: a tool commits a change
only when it is structurally valid FHIR *and* every clinical code it introduced is
confirmed by the terminology server. A rejected code, an unknown enum value, or a
terminology outage must leave the draft untouched and return the reason to the
model instead of raising.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from fhirbridge.agent.draft import DraftState
from fhirbridge.agent.tools import ToolContext, dispatch_tool
from fhirbridge.domain.errors import TerminologyUnavailableError
from fhirbridge.terminology.models import (
    Coding,
    ExpansionResult,
    ValidateCodeResult,
)

pytestmark = pytest.mark.asyncio


class FakeTerminology:
    """A terminology server with scripted answers, recording what it was asked."""

    def __init__(
        self,
        *,
        valid: bool = True,
        raise_unavailable: bool = False,
        expansion: Sequence[Coding] = (),
    ) -> None:
        self.valid = valid
        self.raise_unavailable = raise_unavailable
        self.expansion = tuple(expansion)
        self.validated: list[tuple[str | None, str]] = []

    async def validate_code(
        self,
        *,
        system: str | None,
        code: str,
        display: str | None = None,
        version: str | None = None,
        value_set: str | None = None,
    ) -> ValidateCodeResult:
        self.validated.append((system, code))
        if self.raise_unavailable:
            raise TerminologyUnavailableError("down")
        return ValidateCodeResult(
            result=self.valid,
            coding=Coding(system=system, code=code),
            message=None if self.valid else "code not valid",
        )

    async def expand(
        self,
        *,
        value_set: str,
        filter_text: str | None = None,
        count: int | None = None,
        offset: int | None = None,
    ) -> ExpansionResult:
        return ExpansionResult(value_set=value_set, contains=self.expansion)


def _ctx(terminology: FakeTerminology) -> ToolContext:
    # cascade is only used by validate_draft, which these tests do not exercise.
    return ToolContext(draft=DraftState.new(), terminology=terminology, cascade=None)  # type: ignore[arg-type]


class TestDraft:
    async def test_a_new_draft_is_a_collection_bundle_with_only_the_patient(self) -> None:
        bundle = DraftState.new().to_bundle()

        assert bundle["type"] == "collection"
        assert [entry["resource"]["resourceType"] for entry in bundle["entry"]] == ["Patient"]


class TestPatientDemographics:
    async def test_it_sets_name_and_gender(self) -> None:
        ctx = _ctx(FakeTerminology())

        outcome = await dispatch_tool(
            "set_patient_demographics",
            {"family": "Roe", "given": "Jane", "gender": "female"},
            ctx,
        )

        assert outcome.ok
        patient = ctx.draft.get(ctx.draft.patient_full_url)
        assert patient is not None
        assert patient["name"] == [{"family": "Roe", "given": ["Jane"]}]
        assert patient["gender"] == "female"

    async def test_it_refuses_a_gender_outside_the_value_set(self) -> None:
        ctx = _ctx(FakeTerminology())

        outcome = await dispatch_tool("set_patient_demographics", {"gender": "yes"}, ctx)

        assert not outcome.ok
        assert "gender" in (outcome.error or "")


class TestAddObservation:
    async def test_a_valid_code_is_committed_and_linked_to_the_patient(self) -> None:
        terminology = FakeTerminology(valid=True)
        ctx = _ctx(terminology)

        outcome = await dispatch_tool(
            "add_observation",
            {
                "code": "8867-4",
                "display": "Heart rate",
                "category_code": "vital-signs",
                "value_number": 72,
                "unit": "beats/minute",
                "unit_code": "/min",
            },
            ctx,
        )

        assert outcome.ok, outcome.error
        observations = [
            resource
            for resource in ctx.draft.resources.values()
            if resource["resourceType"] == "Observation"
        ]
        assert len(observations) == 1
        assert observations[0]["subject"] == {"reference": ctx.draft.patient_full_url}
        # Both the LOINC code and the UCUM unit were verified against the server.
        assert ("http://loinc.org", "8867-4") in terminology.validated
        assert ("http://unitsofmeasure.org", "/min") in terminology.validated

    async def test_a_rejected_code_is_not_committed(self) -> None:
        ctx = _ctx(FakeTerminology(valid=False))

        outcome = await dispatch_tool(
            "add_observation", {"code": "0000-0", "display": "Nonsense"}, ctx
        )

        assert not outcome.ok
        assert not any(
            resource["resourceType"] == "Observation" for resource in ctx.draft.resources.values()
        )

    async def test_a_terminology_outage_fails_closed(self) -> None:
        ctx = _ctx(FakeTerminology(raise_unavailable=True))

        outcome = await dispatch_tool(
            "add_observation", {"code": "8867-4", "display": "Heart rate"}, ctx
        )

        assert not outcome.ok
        assert "unavailable" in (outcome.error or "")
        assert all(
            resource["resourceType"] != "Observation" for resource in ctx.draft.resources.values()
        )

    async def test_missing_required_arguments_are_rejected(self) -> None:
        ctx = _ctx(FakeTerminology())

        outcome = await dispatch_tool("add_observation", {"display": "no code"}, ctx)

        assert not outcome.ok


class TestSearchTerminology:
    async def test_it_returns_candidate_codes(self) -> None:
        terminology = FakeTerminology(
            expansion=[Coding(system="http://loinc.org", code="8867-4", display="Heart rate")]
        )
        ctx = _ctx(terminology)

        outcome = await dispatch_tool(
            "search_terminology", {"query": "heart rate", "system": "http://loinc.org"}, ctx
        )

        assert outcome.ok
        assert outcome.content["candidates"][0]["code"] == "8867-4"


class TestValidateDraft:
    async def test_comparison_only_mode_skips_the_cascade(self) -> None:
        ctx = _ctx(FakeTerminology())
        ctx.validation_enabled = False

        outcome = await dispatch_tool("validate_draft", {}, ctx)

        assert outcome.ok
        assert outcome.content["skipped"] is True


class TestSetElement:
    async def test_it_sets_a_nested_element_and_validates(self) -> None:
        ctx = _ctx(FakeTerminology())

        outcome = await dispatch_tool(
            "set_element",
            {"full_url": "patient", "path": "birthDate", "value_json": '"1970-01-01"'},
            ctx,
        )

        assert outcome.ok, outcome.error
        patient = ctx.draft.get(ctx.draft.patient_full_url)
        assert patient is not None
        assert patient["birthDate"] == "1970-01-01"

    async def test_it_refuses_to_change_the_resource_type(self) -> None:
        ctx = _ctx(FakeTerminology())

        outcome = await dispatch_tool(
            "set_element",
            {"full_url": "patient", "path": "resourceType", "value_json": '"Observation"'},
            ctx,
        )

        assert not outcome.ok


class TestUnknownTool:
    async def test_an_unknown_tool_is_reported_not_raised(self) -> None:
        ctx = _ctx(FakeTerminology())

        outcome = await dispatch_tool("delete_everything", {}, ctx)

        assert not outcome.ok
        assert "unknown tool" in (outcome.error or "")


class TestFinish:
    async def test_finish_signals_completion(self) -> None:
        ctx = _ctx(FakeTerminology())

        outcome = await dispatch_tool("finish", {}, ctx)

        assert outcome.ok
        assert outcome.finish
