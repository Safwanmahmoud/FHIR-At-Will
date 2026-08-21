"""The validator sidecar transport (AGENTS.md 4, principle 2.4).

The load-bearing test in this file is
:func:`test_an_unresolvable_profile_is_not_a_pass`. A validator started without
US Core loaded returns a clean-looking OperationOutcome for a resource that
claims a US Core profile. Reading that as conformance would let the service
publish a conformance claim it never actually checked.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from fhirbridge.domain.errors import IgNotLoadedError, ValidatorUnavailableError
from fhirbridge.fhir.validator_client import ValidatorClient
from tests.helpers import OBSERVATION, US_CORE_PATIENT, VALIDATOR_URL, fhir_json, operation_outcome


@pytest.fixture
def client() -> ValidatorClient:
    return ValidatorClient(base_url=VALIDATOR_URL, timeout_s=5.0)


def error(message: str, expression: str | None = None) -> dict[str, object]:
    issue: dict[str, object] = {
        "severity": "error",
        "code": "structure",
        "diagnostics": message,
    }
    if expression:
        issue["expression"] = [expression]
    return issue


# --- validateResource -----------------------------------------------------


async def test_a_clean_outcome_yields_no_issues(
    client: ValidatorClient, mock_http: respx.MockRouter
) -> None:
    mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(
        return_value=fhir_json(operation_outcome())
    )

    outcome = await client.validate_resource(OBSERVATION)

    assert outcome.errors == ()
    assert outcome.warnings == ()
    assert len(outcome.informational) == 1


async def test_profiles_are_sent_as_repeated_query_parameters(
    client: ValidatorClient, mock_http: respx.MockRouter
) -> None:
    """The resource goes in the body; only the profile canonical URL is a
    parameter, and a canonical URL is not PHI."""
    route = mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(
        return_value=fhir_json(operation_outcome())
    )

    await client.validate_resource(OBSERVATION, profiles=[US_CORE_PATIENT, "http://other"])

    request = route.calls[0].request
    assert request.url.params.get_list("profile") == [US_CORE_PATIENT, "http://other"]
    assert b"8867-4" in request.content


async def test_the_best_practice_flag_is_forwarded(
    client: ValidatorClient, mock_http: respx.MockRouter
) -> None:
    route = mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(
        return_value=fhir_json(operation_outcome())
    )

    await client.validate_resource(OBSERVATION, best_practice="hint")

    assert route.calls[0].request.url.params["bestPractice"] == "hint"


async def test_issues_are_sorted_most_severe_first(
    client: ValidatorClient, mock_http: respx.MockRouter
) -> None:
    payload = operation_outcome(
        {"severity": "information", "code": "informational", "diagnostics": "note"},
        {"severity": "warning", "code": "business-rule", "diagnostics": "hmm"},
        {"severity": "error", "code": "structure", "diagnostics": "broken"},
        {"severity": "fatal", "code": "exception", "diagnostics": "very broken"},
    )
    mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(return_value=fhir_json(payload))

    outcome = await client.validate_resource(OBSERVATION)

    assert [issue.severity for issue in outcome.issues] == [
        "fatal",
        "error",
        "warning",
        "information",
    ]


async def test_a_location_is_used_when_no_expression_is_given(
    client: ValidatorClient, mock_http: respx.MockRouter
) -> None:
    payload = operation_outcome(
        {"severity": "error", "code": "structure", "location": ["Observation.status"]}
    )
    mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(return_value=fhir_json(payload))

    outcome = await client.validate_resource(OBSERVATION)

    assert outcome.issues[0].expression == "Observation.status"


async def test_line_and_column_extensions_are_parsed(
    client: ValidatorClient, mock_http: respx.MockRouter
) -> None:
    payload = operation_outcome(
        {
            "severity": "error",
            "code": "structure",
            "diagnostics": "bad",
            "extension": [
                {
                    "url": ("http://hl7.org/fhir/StructureDefinition/operationoutcome-issue-line"),
                    "valueInteger": 12,
                },
                {
                    "url": "http://hl7.org/fhir/StructureDefinition/operationoutcome-issue-col",
                    "valueInteger": 7,
                },
            ],
        }
    )
    mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(return_value=fhir_json(payload))

    outcome = await client.validate_resource(OBSERVATION)

    assert (outcome.issues[0].line, outcome.issues[0].column) == (12, 7)


async def test_a_bundle_of_outcomes_is_flattened(
    client: ValidatorClient, mock_http: respx.MockRouter
) -> None:
    """Batch validation returns a Bundle; every nested issue must still count."""
    payload = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [
            {"resource": operation_outcome(error("first"))},
            {"resource": operation_outcome(error("second"))},
        ],
    }
    mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(return_value=fhir_json(payload))

    outcome = await client.validate_resource(OBSERVATION)

    assert {issue.message for issue in outcome.errors} == {"first", "second"}


async def test_the_code_is_used_when_there_is_no_diagnostic_text(
    client: ValidatorClient, mock_http: respx.MockRouter
) -> None:
    payload = operation_outcome({"severity": "error", "code": "invariant"})
    mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(return_value=fhir_json(payload))

    outcome = await client.validate_resource(OBSERVATION)

    assert outcome.issues[0].message == "invariant"


# --- Unresolvable profiles are not passes ---------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "Profile reference '{p}' could not be resolved, so has not been checked",
        "Unable to resolve profile {p}",
        "The profile {p} is not known",
        "StructureDefinition {p} could not be found",
    ],
)
async def test_an_unresolvable_profile_is_not_a_pass(
    client: ValidatorClient, mock_http: respx.MockRouter, message: str
) -> None:
    payload = operation_outcome(
        {
            "severity": "warning",
            "code": "not-found",
            "diagnostics": message.format(p=US_CORE_PATIENT),
        }
    )
    mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(return_value=fhir_json(payload))

    with pytest.raises(IgNotLoadedError) as caught:
        await client.validate_resource({"resourceType": "Patient"}, profiles=[US_CORE_PATIENT])

    assert caught.value.safe_context["profile"] == US_CORE_PATIENT
    assert "Load the implementation guide" in caught.value.detail


async def test_a_resolution_complaint_about_a_different_profile_is_not_our_problem(
    client: ValidatorClient, mock_http: respx.MockRouter
) -> None:
    payload = operation_outcome(
        {
            "severity": "warning",
            "code": "not-found",
            "diagnostics": "Profile reference 'http://elsewhere/other' could not be resolved",
        }
    )
    mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(return_value=fhir_json(payload))

    outcome = await client.validate_resource(
        {"resourceType": "Patient"}, profiles=[US_CORE_PATIENT]
    )

    assert len(outcome.warnings) == 1


async def test_no_profile_request_means_no_resolution_check(
    client: ValidatorClient, mock_http: respx.MockRouter
) -> None:
    payload = operation_outcome(
        {"severity": "warning", "code": "not-found", "diagnostics": "could not be resolved"}
    )
    mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(return_value=fhir_json(payload))

    outcome = await client.validate_resource(OBSERVATION)

    assert len(outcome.warnings) == 1


# --- Fail closed ----------------------------------------------------------


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_a_server_error_fails_closed(
    client: ValidatorClient, mock_http: respx.MockRouter, status: int
) -> None:
    mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(return_value=httpx.Response(status))

    with pytest.raises(ValidatorUnavailableError):
        await client.validate_resource(OBSERVATION)


@pytest.mark.parametrize("status", [404, 405])
async def test_a_missing_endpoint_says_to_check_the_sidecar_mode(
    client: ValidatorClient, mock_http: respx.MockRouter, status: int
) -> None:
    """404 here almost always means the jar is not in ``server`` mode, or is an
    older version whose endpoints differ."""
    mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(return_value=httpx.Response(status))

    with pytest.raises(ValidatorUnavailableError) as caught:
        await client.validate_resource(OBSERVATION)

    assert "'server' mode" in caught.value.detail


async def test_a_timeout_fails_closed_and_reports_the_configured_limit(
    client: ValidatorClient, mock_http: respx.MockRouter
) -> None:
    mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(ValidatorUnavailableError) as caught:
        await client.validate_resource(OBSERVATION)

    assert caught.value.safe_context["timeout_s"] == 5.0


async def test_a_transport_failure_fails_closed(
    client: ValidatorClient, mock_http: respx.MockRouter
) -> None:
    mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(
        side_effect=httpx.ConnectError("refused")
    )

    with pytest.raises(ValidatorUnavailableError):
        await client.validate_resource(OBSERVATION)


async def test_a_non_json_body_fails_closed(
    client: ValidatorClient, mock_http: respx.MockRouter
) -> None:
    mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(
        return_value=httpx.Response(200, content=b"OK")
    )

    with pytest.raises(ValidatorUnavailableError):
        await client.validate_resource(OBSERVATION)


async def test_a_non_operation_outcome_body_fails_closed(
    client: ValidatorClient, mock_http: respx.MockRouter
) -> None:
    mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(
        return_value=fhir_json({"resourceType": "Patient"})
    )

    with pytest.raises(ValidatorUnavailableError) as caught:
        await client.validate_resource(OBSERVATION)

    assert caught.value.safe_context["resource_type"] == "Patient"


async def test_a_json_array_body_fails_closed(
    client: ValidatorClient, mock_http: respx.MockRouter
) -> None:
    mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(
        return_value=httpx.Response(200, json=["not", "a", "resource"])
    )

    with pytest.raises(ValidatorUnavailableError):
        await client.validate_resource(OBSERVATION)


# --- FHIRPath -------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ([True], True),
        ([False], False),
        ([], False),
        ([True, True], False),
        (["true"], True),
        ({"result": [True]}, True),
        ({"result": True}, True),
        ({"resourceType": "Parameters", "parameter": [{"name": "r", "valueBoolean": True}]}, True),
        # The exact shape validator_cli.jar 6.10.2 returns: the request echoed
        # back beside the answer. Counting the echo as a value would make a
        # satisfied invariant read as violated, which is why `result` wins.
        (
            {
                "resourceType": "Parameters",
                "parameter": [
                    {"name": "expression", "valueString": "Patient.gender.exists()"},
                    {"name": "result", "valueString": "true"},
                ],
            },
            True,
        ),
        (
            {
                "resourceType": "Parameters",
                "parameter": [
                    {"name": "expression", "valueString": "Patient.deceased.exists()"},
                    {"name": "result", "valueString": "false"},
                ],
            },
            False,
        ),
    ],
)
async def test_fhirpath_truthiness_across_response_shapes(
    client: ValidatorClient, mock_http: respx.MockRouter, body: object, expected: bool
) -> None:
    """The endpoint's response shape is not contractually specified, so the
    plausible encodings are all accepted rather than one being guessed."""
    mock_http.post(f"{VALIDATOR_URL}/fhirpath").mock(return_value=httpx.Response(200, json=body))

    outcome = await client.evaluate_fhirpath(OBSERVATION, "1 = 1")

    assert outcome.is_true is expected


@pytest.mark.parametrize(("text", "expected"), [("true", True), ("false", False), ("  ", False)])
async def test_fhirpath_accepts_a_plain_text_answer(
    client: ValidatorClient, mock_http: respx.MockRouter, text: str, expected: bool
) -> None:
    mock_http.post(f"{VALIDATOR_URL}/fhirpath").mock(
        return_value=httpx.Response(
            200, content=text.encode(), headers={"Content-Type": "text/plain"}
        )
    )

    outcome = await client.evaluate_fhirpath(OBSERVATION, "1 = 1")

    assert outcome.is_true is expected


async def test_an_empty_body_is_an_empty_collection_not_an_outage(
    client: ValidatorClient, mock_http: respx.MockRouter
) -> None:
    """An empty FHIRPath collection is a real answer from a live server.

    Reading it as unavailability would return 503 for every resource carrying an
    empty-collection invariant. L4 already refuses to treat empty as a pass, so
    nothing is overclaimed by accepting it here.
    """
    mock_http.post(f"{VALIDATOR_URL}/fhirpath").mock(return_value=httpx.Response(200, content=b""))

    outcome = await client.evaluate_fhirpath(OBSERVATION, "Observation.nothing.exists()")

    assert outcome.values == ()
    assert outcome.is_true is False


async def test_the_expression_travels_as_a_parameter_and_the_resource_as_the_body(
    client: ValidatorClient, mock_http: respx.MockRouter
) -> None:
    route = mock_http.post(f"{VALIDATOR_URL}/fhirpath").mock(
        return_value=httpx.Response(200, json=[True])
    )

    await client.evaluate_fhirpath(OBSERVATION, "Observation.status.exists()")

    request = route.calls[0].request
    assert request.url.params["expression"] == "Observation.status.exists()"
    assert b"8867-4" not in str(request.url).encode()


# --- Health ---------------------------------------------------------------


async def test_health_reports_ready_when_every_required_profile_resolves(
    client: ValidatorClient, mock_http: respx.MockRouter
) -> None:
    mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(
        return_value=fhir_json(operation_outcome())
    )

    health = await client.health(required_profiles=[US_CORE_PATIENT])

    assert health.ready is True
    assert health.profiles_loaded == (US_CORE_PATIENT,)
    assert health.profiles_missing == ()


async def test_health_is_not_ready_when_an_ig_is_missing(
    client: ValidatorClient, mock_http: respx.MockRouter
) -> None:
    """Reachable but unable to resolve US Core is *not* ready: every conformance
    claim we would make against it is void."""
    responses = [
        fhir_json(operation_outcome()),
        fhir_json(
            operation_outcome(
                {
                    "severity": "warning",
                    "code": "not-found",
                    "diagnostics": f"Profile reference '{US_CORE_PATIENT}' could not be resolved",
                }
            )
        ),
    ]
    mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(side_effect=responses)

    health = await client.health(required_profiles=[US_CORE_PATIENT])

    assert health.reachable is True
    assert health.ready is False
    assert health.profiles_missing == (US_CORE_PATIENT,)
    assert health.detail is not None
    assert "-ig" in health.detail


async def test_health_reports_unreachable_rather_than_raising(
    client: ValidatorClient, mock_http: respx.MockRouter
) -> None:
    mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(side_effect=httpx.ConnectError("down"))

    health = await client.health()

    assert health.reachable is False
    assert health.ready is False


async def test_health_reports_unreachable_when_the_profile_probe_fails(
    client: ValidatorClient, mock_http: respx.MockRouter
) -> None:
    mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(
        side_effect=[fhir_json(operation_outcome()), httpx.Response(503)]
    )

    health = await client.health(required_profiles=[US_CORE_PATIENT])

    assert health.reachable is False


# --- Lifecycle ------------------------------------------------------------


async def test_the_client_owns_and_closes_its_own_transport() -> None:
    async with ValidatorClient(base_url=VALIDATOR_URL) as owner:
        assert owner.http is not None

    assert owner.http.is_closed


async def test_an_injected_transport_is_not_closed_by_us() -> None:
    """Closing a caller's shared pool would break every other user of it."""
    injected = httpx.AsyncClient()
    client = ValidatorClient(base_url=VALIDATOR_URL, client=injected)

    await client.aclose()

    assert not injected.is_closed
    await injected.aclose()


def test_the_base_url_is_normalized_without_a_trailing_slash() -> None:
    assert ValidatorClient(base_url=f"{VALIDATOR_URL}/").base_url == VALIDATOR_URL


def test_the_repr_omits_the_transport() -> None:
    assert "AsyncClient" not in repr(ValidatorClient(base_url=VALIDATOR_URL))
