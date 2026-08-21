"""L3 terminology validation (AGENTS.md 10, principle 2.3).

The property under test throughout: **a question the terminology server could
not answer is never recorded as a pass.** Collapsing "unknown" into "valid" is
the single failure mode that would let unvalidated codes into a bundle, and
collapsing it into "invalid" would make the service unusable against a partially
loaded server. Both are wrong, and they are wrong in different directions.
"""

from __future__ import annotations

from typing import Any

import pytest

from fhirbridge.domain.errors import TerminologyUnavailableError
from fhirbridge.terminology.models import BindingStrength
from fhirbridge.validation.models import IssueSeverity, LayerStatus
from fhirbridge.validation.structural import validate_structure
from fhirbridge.validation.terminology import (
    Binding,
    BindingRegistry,
    load_bindings,
    validate_terminology,
)
from tests.fakes import FakeTerminologyClient
from tests.helpers import OBSERVATION

LOINC = "http://loinc.org"
UCUM = "http://unitsofmeasure.org"
OBSERVATION_STATUS_VS = "http://hl7.org/fhir/ValueSet/observation-status"


def typed(payload: dict[str, Any]) -> Any:
    result = validate_structure(payload)
    assert result.typed is not None, result.result.issues
    return result.typed


async def run(payload: dict[str, Any], client: FakeTerminologyClient, **kwargs: Any) -> Any:
    return await validate_terminology(typed(payload), client=client, **kwargs)


# --- The happy path, and what it actually checked --------------------------


async def test_a_confirmed_observation_passes() -> None:
    client = FakeTerminologyClient()

    result = await run(OBSERVATION, client)

    assert result.status is LayerStatus.PASSED
    assert result.issues == []


async def test_the_loinc_code_is_confirmed_against_its_own_code_system() -> None:
    client = FakeTerminologyClient()

    await run(OBSERVATION, client)

    assert any(
        call.system == LOINC and call.code == "8867-4" and call.value_set is None
        for call in client.calls
    )


async def test_a_primitive_code_is_checked_against_its_bound_value_set() -> None:
    """``Observation.status`` has a *required* binding, so membership is the check."""
    client = FakeTerminologyClient()

    await run(OBSERVATION, client)

    assert any(
        call.code == "preliminary" and call.value_set == OBSERVATION_STATUS_VS
        for call in client.calls
    )


async def test_a_ucum_unit_is_checked_as_a_unit_not_a_concept() -> None:
    client = FakeTerminologyClient()

    await run(OBSERVATION, client)

    assert any(call.system == UCUM and call.code == "/min" for call in client.calls)


async def test_the_notes_state_how_much_was_checked() -> None:
    result = await run(OBSERVATION, FakeTerminologyClient())

    assert any("terminology call(s) across" in note for note in result.notes)


# --- Invalid codes ---------------------------------------------------------


async def test_a_code_the_server_denies_is_a_blocking_error() -> None:
    client = FakeTerminologyClient(answers={"8867-4": False})

    result = await run(OBSERVATION, client)

    assert result.status is LayerStatus.FAILED
    issue = next(i for i in result.issues if "8867-4" in i.message)
    assert issue.severity is IssueSeverity.ERROR
    assert issue.code == "code-invalid"
    assert "CodeableConcept.text only" in issue.message


async def test_the_servers_own_explanation_is_carried_into_the_issue() -> None:
    client = FakeTerminologyClient(
        answers={"8867-4": False}, messages={"8867-4": "code was retired in 2019"}
    )

    result = await run(OBSERVATION, client)

    assert any("retired in 2019" in issue.message for issue in result.issues)


async def test_a_required_binding_violation_is_a_blocking_error() -> None:
    client = FakeTerminologyClient(membership={"preliminary": False})

    result = await run(OBSERVATION, client)

    assert result.status is LayerStatus.FAILED
    issue = next(i for i in result.issues if OBSERVATION_STATUS_VS in i.message)
    assert issue.severity is IssueSeverity.ERROR
    assert "required binding" in issue.message


async def test_a_weaker_binding_violation_only_warns() -> None:
    """A real code that is simply outside an *extensible* ValueSet is not an error.

    Extensible bindings exist precisely so a local concept can be used, so this
    must not block. The code itself is still confirmed against its CodeSystem.
    """
    payload = OBSERVATION | {
        "category": [{"coding": [{"system": "http://example.org/local", "code": "local-vitals"}]}]
    }
    registry = BindingRegistry(
        bindings={
            "Observation.category": Binding(
                path="Observation.category",
                value_set="http://hl7.org/fhir/ValueSet/observation-category",
                strength=BindingStrength.EXTENSIBLE,
                kind="codeable_concept",
            )
        },
        unit_systems=frozenset({UCUM}),
    )
    client = FakeTerminologyClient(membership={"local-vitals": False})

    result = await validate_terminology(typed(payload), client=client, bindings=registry)

    assert IssueSeverity.ERROR not in {i.severity for i in result.issues}
    assert result.status is LayerStatus.PASSED
    assert any("does not satisfy the extensible binding" in i.message for i in result.issues)


async def test_a_code_absent_from_its_own_code_system_is_an_error_whatever_the_binding() -> None:
    """Binding strength governs *membership*, not existence.

    A code the CodeSystem does not contain cannot be justified at all, so a weak
    binding must not soften it. This is the loophole principle 2.3 closes.
    """
    payload = OBSERVATION | {
        "category": [{"coding": [{"system": "http://example.org/local", "code": "invented"}]}]
    }
    registry = BindingRegistry(
        bindings={
            "Observation.category": Binding(
                path="Observation.category",
                value_set="http://hl7.org/fhir/ValueSet/observation-category",
                strength=BindingStrength.EXAMPLE,
                kind="codeable_concept",
            )
        },
        unit_systems=frozenset({UCUM}),
    )
    client = FakeTerminologyClient(answers={"invented": False})

    result = await validate_terminology(typed(payload), client=client, bindings=registry)

    assert result.status is LayerStatus.FAILED
    assert any(i.severity is IssueSeverity.ERROR for i in result.issues)


async def test_a_coding_with_a_code_but_no_system_is_an_error() -> None:
    """Without a system there is no CodeSystem to confirm against, so the code
    cannot be justified at all — text-only is the correct output."""
    payload = OBSERVATION | {"code": {"coding": [{"code": "8867-4"}], "text": "heart rate"}}
    client = FakeTerminologyClient()

    result = await run(payload, client)

    issue = next(i for i in result.issues if "no system" in i.message)
    assert issue.severity is IssueSeverity.ERROR
    assert "Emit CodeableConcept.text instead" in issue.message


async def test_a_text_only_codeable_concept_needs_no_terminology_call() -> None:
    payload = OBSERVATION | {"code": {"text": "heart rate, by report"}}
    client = FakeTerminologyClient()

    result = await run(payload, client)

    assert "8867-4" not in client.codes_checked()
    assert result.status is LayerStatus.PASSED


# --- "The server does not know that" is not a pass -------------------------


async def test_an_unknown_value_set_on_a_required_binding_is_an_error() -> None:
    client = FakeTerminologyClient(unknown_value_sets={OBSERVATION_STATUS_VS})

    result = await run(OBSERVATION, client)

    issue = next(i for i in result.issues if "does not know" in i.message)
    assert issue.severity is IssueSeverity.ERROR
    assert issue.code == "not-found"
    assert issue.machine_code == "unknown-value-set"
    assert "This is not a pass" in issue.message
    assert "docs/terminology-setup.md" in issue.message
    assert result.status is LayerStatus.FAILED


async def test_an_unknown_code_system_is_reported_not_swallowed() -> None:
    client = FakeTerminologyClient(unknown_systems={LOINC})

    result = await run(OBSERVATION, client)

    assert any("does not know CodeSystem" in issue.message for issue in result.issues)


async def test_unanswerable_checks_are_counted_in_the_notes() -> None:
    client = FakeTerminologyClient(unknown_value_sets={OBSERVATION_STATUS_VS})

    result = await run(OBSERVATION, client)

    assert any("could not be answered" in note for note in result.notes)


# --- Outage: fail closed --------------------------------------------------


async def test_an_outage_propagates_rather_than_becoming_an_issue() -> None:
    """A dead terminology server must surface as 503, not as a clean report.

    This is principle 2.4. The layer deliberately does not catch this.
    """
    client = FakeTerminologyClient(unavailable=True)

    with pytest.raises(TerminologyUnavailableError):
        await run(OBSERVATION, client)


# --- Coverage honesty -----------------------------------------------------


async def test_paths_with_no_known_binding_are_reported_as_deferred_to_l2() -> None:
    registry = BindingRegistry(bindings={}, unit_systems=frozenset({UCUM}))
    client = FakeTerminologyClient()

    result = await validate_terminology(typed(OBSERVATION), client=client, bindings=registry)

    assert any("deferred to L2" in note for note in result.notes)


async def test_a_unit_in_an_unrecognized_system_is_deferred_not_guessed() -> None:
    payload = OBSERVATION | {
        "valueQuantity": {"value": 72, "system": "http://example.org/units", "code": "bpm"}
    }
    client = FakeTerminologyClient()

    await run(payload, client)

    assert "bpm" not in client.codes_checked()


async def test_a_unit_code_without_a_system_warns_rather_than_erroring() -> None:
    payload = OBSERVATION | {"valueQuantity": {"value": 72, "unit": "beats/minute", "code": "/min"}}
    client = FakeTerminologyClient()

    result = await run(payload, client)

    issue = next(i for i in result.issues if "unit cannot be" in i.message)
    assert issue.severity is IssueSeverity.WARNING
    assert "http://unitsofmeasure.org" in issue.message


async def test_the_check_budget_is_enforced_and_disclosed() -> None:
    client = FakeTerminologyClient()

    result = await run(OBSERVATION, client, max_checks=1)

    assert len(client.calls) == 1
    assert any("Stopped after 1 terminology checks" in note for note in result.notes)
    assert any("were not checked" in note for note in result.notes)


# --- Display disagreement -------------------------------------------------


async def test_a_display_that_disagrees_with_the_server_warns() -> None:
    """A reviewer confirms what they read. If the display is wrong, they are
    confirming something other than what the code means."""
    payload = OBSERVATION | {
        "code": {"coding": [{"system": LOINC, "code": "8867-4", "display": "Body weight"}]}
    }
    client = FakeTerminologyClient(displays={"8867-4": "Heart rate"})

    result = await run(payload, client)

    issue = next(i for i in result.issues if "differs from the terminology" in i.message)
    assert issue.severity is IssueSeverity.WARNING
    assert issue.expression is not None and issue.expression.endswith(".display")


async def test_a_display_matching_case_insensitively_does_not_warn() -> None:
    payload = OBSERVATION | {
        "code": {"coding": [{"system": LOINC, "code": "8867-4", "display": "heart RATE"}]}
    }
    client = FakeTerminologyClient(displays={"8867-4": "Heart rate"})

    result = await run(payload, client)

    assert not any("differs from" in issue.message for issue in result.issues)


# --- Bundles --------------------------------------------------------------


async def test_bundle_entries_are_walked_and_located_by_index() -> None:
    payload = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{"resource": OBSERVATION}, {"resource": OBSERVATION}],
    }
    client = FakeTerminologyClient(answers={"8867-4": False})

    result = await run(payload, client)

    expressions = {issue.expression for issue in result.issues}
    assert any(expr and expr.startswith("Bundle.entry[0]") for expr in expressions)
    assert any(expr and expr.startswith("Bundle.entry[1]") for expr in expressions)


async def test_bundle_type_is_checked_against_its_required_binding() -> None:
    payload = {"resourceType": "Bundle", "type": "collection", "entry": []}
    client = FakeTerminologyClient()

    await run(payload, client)

    assert "http://hl7.org/fhir/ValueSet/bundle-type" in client.value_sets_checked()


# --- The binding pack itself ----------------------------------------------


def test_the_binding_pack_loads_and_declares_ucum_as_a_unit_system() -> None:
    registry = load_bindings()

    assert UCUM in registry.unit_systems
    assert len(registry.bindings) > 20


def test_the_critical_domain_bindings_are_present_and_required() -> None:
    """Allergy is a critical domain, so its status bindings must actually bite."""
    registry = load_bindings()

    for path in (
        "AllergyIntolerance.clinicalStatus",
        "AllergyIntolerance.verificationStatus",
        "AllergyIntolerance.criticality",
    ):
        binding = registry.get(path)
        assert binding is not None, path
        assert binding.strength is BindingStrength.REQUIRED, path


def test_only_required_bindings_are_treated_as_blocking() -> None:
    assert BindingStrength.REQUIRED.is_blocking
    assert not BindingStrength.EXTENSIBLE.is_blocking
    assert not BindingStrength.PREFERRED.is_blocking
    assert not BindingStrength.EXAMPLE.is_blocking
