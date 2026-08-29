"""Live NDJSON events from ``POST /v1/craft/stream``."""

from __future__ import annotations

import json

import httpx
from fastapi import FastAPI

from fhirbridge.api.deps import get_llm_gateway
from fhirbridge.llm.gateway import LlmToolCall, LlmToolTurn
from tests.fakes import FakeLlmGateway

BYOK_HEADERS = {
    "X-LLM-Provider": "openrouter",
    "X-LLM-Model": "openai/gpt-4o-mini",
    "X-LLM-API-Key": "sk-test",
    "X-PHI-Egress-Acknowledged": "true",
}


def _turn(tool_id: str, name: str, arguments: dict[str, object]) -> LlmToolTurn:
    call = LlmToolCall(id=tool_id, name=name, arguments=json.dumps(arguments))
    return LlmToolTurn(
        content="",
        tool_calls=(call,),
        assistant_message={
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": tool_id,
                    "type": "function",
                    "function": {"name": name, "arguments": call.arguments},
                }
            ],
        },
        model="openrouter/openai/gpt-4o-mini",
    )


async def test_stream_shows_tool_activity_and_live_draft(
    app: FastAPI,
    client: httpx.AsyncClient,
    all_dependencies_healthy: None,
) -> None:
    gateway = FakeLlmGateway(
        tool_turns=[
            _turn("call_1", "set_patient_demographics", {"gender": "male"}),
            _turn("call_2", "finish", {}),
        ]
    )
    app.dependency_overrides[get_llm_gateway] = lambda: gateway

    response = await client.post(
        "/v1/craft/stream",
        json={"text": "Synthetic male patient."},
        headers=BYOK_HEADERS,
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in response.text.splitlines()]

    assert events[0]["type"] == "started"
    assert events[0]["bundle"]["entry"][0]["resource"] == {"resourceType": "Patient"}

    tool_events = [event for event in events if event["type"] == "tool"]
    assert [(event["phase"], event["tool"]) for event in tool_events] == [
        ("start", "set_patient_demographics"),
        ("end", "set_patient_demographics"),
        ("start", "finish"),
        ("end", "finish"),
    ]

    draft = next(event for event in events if event["type"] == "draft")
    assert draft["tool"] == "set_patient_demographics"
    assert draft["bundle"]["entry"][0]["resource"]["gender"] == "male"

    complete = events[-1]
    assert complete["type"] == "complete"
    assert complete["bundle"] == draft["bundle"]
    assert complete["stop_reason"] == "finished"
    assert complete["iterations"] == 2
    assert complete["validated"] is True
    assert complete["report"] is not None


async def test_stream_can_explicitly_skip_validation_for_comparison_only_runs(
    app: FastAPI,
    client: httpx.AsyncClient,
    all_dependencies_healthy: None,
) -> None:
    gateway = FakeLlmGateway(tool_turns=[_turn("call_1", "finish", {})])
    app.dependency_overrides[get_llm_gateway] = lambda: gateway

    response = await client.post(
        "/v1/craft/stream",
        json={"text": "Synthetic patient.", "validate_output": False},
        headers=BYOK_HEADERS,
    )

    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines()]
    complete = events[-1]
    assert complete["type"] == "complete"
    assert complete["validated"] is False
    assert complete["report"] is None
    assert any(event.get("phase") == "validation_skipped" for event in events)
