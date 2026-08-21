"""L2 profile and L4 invariant layers (AGENTS.md 10).

Both layers delegate to the validator sidecar, and both must fail closed. The
subtle requirement is L2's treatment of an *unresolvable profile*: a validator
without US Core loaded returns a clean OperationOutcome for a resource claiming
a US Core profile, so "no issues" would become an unfounded conformance claim.
"""

from __future__ import annotations

from typing import Any

import pytest

from fhirbridge.domain.errors import ValidatorUnavailableError
from fhirbridge.fhir.validator_client import FhirPathNotEvaluableError
from fhirbridge.validation.invariants import (
    load_invariants,
    validate_invariants,
)
from fhirbridge.validation.models import IssueSeverity, LayerStatus, ValidationLayer
from fhirbridge.validation.profile import validate_profile
from tests.fakes import FakeValidatorClient, issue
from tests.helpers import OBSERVATION, US_CORE_PATIENT


class TestProfileLayer:
    async def test_a_clean_outcome_passes(self) -> None:
        result = await validate_profile(OBSERVATION, client=FakeValidatorClient())

        assert result.layer is ValidationLayer.PROFILE
        assert result.layer_number == 2
        assert result.status is LayerStatus.PASSED
        assert result.blocking is True

    async def test_errors_are_translated_with_their_location(self) -> None:
        client = FakeValidatorClient(
            issues=(
                issue(
                    severity="error",
                    code="required",
                    message="Observation.category: minimum required = 1",
                    expression="Observation.category",
                ),
            )
        )

        result = await validate_profile(OBSERVATION, client=client)

        assert result.status is LayerStatus.FAILED
        assert result.issues[0].severity is IssueSeverity.ERROR
        assert result.issues[0].expression == "Observation.category"

    async def test_warnings_do_not_fail_the_layer(self) -> None:
        client = FakeValidatorClient(issues=(issue(severity="warning", message="best practice"),))

        result = await validate_profile(OBSERVATION, client=client)

        assert result.status is LayerStatus.PASSED
        assert result.warnings == 1

    async def test_an_unknown_severity_is_treated_as_an_error(self) -> None:
        """Fail closed on a severity we do not recognize rather than ignoring it."""
        client = FakeValidatorClient(issues=(issue(severity="catastrophe"),))

        result = await validate_profile(OBSERVATION, client=client)

        assert result.issues[0].severity is IssueSeverity.ERROR

    async def test_the_success_severity_is_informational(self) -> None:
        client = FakeValidatorClient(issues=(issue(severity="success", message="All OK"),))

        result = await validate_profile(OBSERVATION, client=client)

        assert result.issues[0].severity is IssueSeverity.INFORMATION
        assert result.status is LayerStatus.PASSED

    async def test_requested_profiles_are_forwarded_to_the_sidecar(self) -> None:
        client = FakeValidatorClient()

        await validate_profile(
            {"resourceType": "Patient"}, client=client, profiles=[US_CORE_PATIENT]
        )

        assert client.validate_calls[0][1] == (US_CORE_PATIENT,)

    async def test_an_outage_propagates_as_a_fail_closed_error(self) -> None:
        with pytest.raises(ValidatorUnavailableError):
            await validate_profile(OBSERVATION, client=FakeValidatorClient(unavailable=True))

    async def test_a_bundle_with_caller_profiles_says_where_they_apply(self) -> None:
        """Silently validating nothing useful would be worse than saying so."""
        result = await validate_profile(
            {"resourceType": "Bundle", "type": "collection"},
            client=FakeValidatorClient(),
            profiles=[US_CORE_PATIENT],
            resource_type="Bundle",
        )

        assert any("Entry-level conformance" in note for note in result.notes)

    async def test_declared_profiles_are_named_in_the_notes(self) -> None:
        payload = {"resourceType": "Patient", "meta": {"profile": [US_CORE_PATIENT]}}

        result = await validate_profile(payload, client=FakeValidatorClient())

        assert any("declared in meta.profile" in note for note in result.notes)

    async def test_a_non_list_meta_profile_is_ignored_rather_than_crashing(self) -> None:
        payload = {"resourceType": "Patient", "meta": {"profile": US_CORE_PATIENT}}

        result = await validate_profile(payload, client=FakeValidatorClient())

        assert result.notes == []


class TestInvariantLayer:
    async def test_all_invariants_true_passes(self) -> None:
        result = await validate_invariants(
            OBSERVATION, client=FakeValidatorClient(), resource_type="Observation"
        )

        assert result.layer_number == 4
        assert result.status is LayerStatus.PASSED
        assert result.issues == []

    async def test_the_pack_is_actually_evaluated(self) -> None:
        client = FakeValidatorClient()

        result = await validate_invariants(OBSERVATION, client=client, resource_type="Observation")

        assert client.fhirpath_calls, "no invariant was evaluated"
        assert any("Evaluated" in note for note in result.notes)

    async def test_a_failing_invariant_reports_its_id_source_and_expression(self) -> None:
        pack = load_invariants()
        target = next(inv for inv in pack.for_resource("Observation"))
        client = FakeValidatorClient(fhirpath_results={target.expression: False})

        result = await validate_invariants(OBSERVATION, client=client, resource_type="Observation")

        failed = next(i for i in result.issues if i.rule_id == target.id)
        assert target.human in failed.message
        assert target.expression in failed.message
        assert target.source in failed.message

    async def test_an_empty_result_is_inconclusive_not_a_pass(self) -> None:
        """A FHIRPath host that returns nothing has not demonstrated the invariant."""
        pack = load_invariants()
        target = next(
            inv for inv in pack.for_resource("Observation") if not inv.tolerate_evaluation_failure
        )
        client = FakeValidatorClient(fhirpath_results={target.expression: []})

        result = await validate_invariants(OBSERVATION, client=client, resource_type="Observation")

        inconclusive = next(i for i in result.issues if i.rule_id == target.id)
        assert inconclusive.severity is IssueSeverity.WARNING
        assert inconclusive.code == "incomplete"
        assert "not reported as passing" in inconclusive.message
        assert any("inconclusive" in note for note in result.notes)

    async def test_a_refused_expression_on_a_tolerant_rule_does_not_fail_the_layer(self) -> None:
        """The host cannot evaluate %resource, and bdl-3 anticipates exactly that.

        The rule must not pass, must not error, and must still be disclosed as
        inconclusive: an unevaluable invariant is not evidence of conformance.
        """
        pack = load_invariants()
        target = next(inv for inv in pack.for_resource("Bundle") if inv.id == "bdl-3")
        assert target.tolerate_evaluation_failure
        client = FakeValidatorClient(
            fhirpath_results={target.expression: FhirPathNotEvaluableError("refused")}
        )

        result = await validate_invariants(
            {"resourceType": "Bundle", "type": "collection", "entry": []},
            client=client,
            resource_type="Bundle",
        )

        assert result.status is LayerStatus.PASSED
        assert not any(i.rule_id == target.id for i in result.issues)
        assert any("inconclusive" in note for note in result.notes)

    async def test_a_refused_expression_on_a_strict_rule_is_reported(self) -> None:
        pack = load_invariants()
        target = next(
            inv for inv in pack.for_resource("Observation") if not inv.tolerate_evaluation_failure
        )
        client = FakeValidatorClient(
            fhirpath_results={target.expression: FhirPathNotEvaluableError("refused")}
        )

        result = await validate_invariants(OBSERVATION, client=client, resource_type="Observation")

        reported = next(i for i in result.issues if i.rule_id == target.id)
        assert reported.code == "incomplete"
        assert "would not evaluate" in reported.message

    async def test_an_outage_propagates_rather_than_downgrading_to_inconclusive(self) -> None:
        """A dead sidecar is a 503, not two hundred inconclusive invariants."""
        with pytest.raises(ValidatorUnavailableError):
            await validate_invariants(
                OBSERVATION,
                client=FakeValidatorClient(unavailable=True),
                resource_type="Observation",
            )

    async def test_bundle_entries_are_each_evaluated_with_their_own_type(self) -> None:
        payload: dict[str, Any] = {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [{"resource": OBSERVATION}, {"resource": {"resourceType": "Patient"}}],
        }
        pack = load_invariants()
        target = next(inv for inv in pack.for_resource("Observation"))
        client = FakeValidatorClient(fhirpath_results={target.expression: False})

        result = await validate_invariants(payload, client=client, resource_type="Bundle")

        expressions = {i.expression for i in result.issues}
        assert "Bundle.entry[0].resource" in expressions
        assert any("across 3 resource(s)" in note for note in result.notes)

    async def test_malformed_bundle_entries_are_skipped(self) -> None:
        payload: dict[str, Any] = {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": ["nonsense", {"resource": "also nonsense"}, {"no_resource": True}],
        }

        result = await validate_invariants(
            payload, client=FakeValidatorClient(), resource_type="Bundle"
        )

        assert any("across 1 resource(s)" in note for note in result.notes)

    async def test_the_evaluation_budget_is_enforced_and_disclosed(self) -> None:
        pack = load_invariants()
        capped = type(pack)(invariants=pack.invariants, max_evaluations=2)
        client = FakeValidatorClient()

        result = await validate_invariants(
            OBSERVATION, client=client, resource_type="Observation", pack=capped
        )

        assert len(client.fhirpath_calls) == 2
        assert any("not claimed to pass" in note for note in result.notes)


def test_the_invariant_pack_has_a_bounded_budget() -> None:
    pack = load_invariants()

    assert pack.max_evaluations > 0
    assert len(pack.invariants) > 0


def test_wildcard_invariants_apply_to_every_resource_type() -> None:
    pack = load_invariants()

    wildcards = [inv for inv in pack.invariants if "*" in inv.applies_to]
    for invariant in wildcards:
        assert invariant.applies("Observation")
        assert invariant.applies("SomeTypeNobodyThoughtOf")
