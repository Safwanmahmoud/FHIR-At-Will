"""The craft agent's tool-calling loop (AGENTS.md 7, principle 2.3).

The loop is deliberately dumb: it authorizes once, then repeatedly asks the model
for the next tool call and executes it against the deterministic tool set. It
never inspects clinical content or second-guesses the model's choices — its only
jobs are to enforce the hard bounds (iterations, budget) and to run the full
cascade on whatever draft the model finishes with. Every guarantee about the
*validity* of that draft is owned by the tools, not by this loop.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from fhirbridge.agent.draft import DraftState
from fhirbridge.agent.tools import TOOL_SCHEMAS, ToolContext, dispatch_tool
from fhirbridge.config import Settings
from fhirbridge.llm.gateway import LlmGateway
from fhirbridge.llm.invocation import LlmInvocation
from fhirbridge.llm.prompts import NARRATIVE_TO_DRAFT_AGENT, PROMPT_SET_VERSION
from fhirbridge.terminology.interface import TerminologyClient
from fhirbridge.validation.cascade import ValidationCascade, ValidationSpec
from fhirbridge.validation.models import ValidationLayer, ValidationReport
from fhirbridge.version import AGENT_TOOLSET_VERSION

logger = logging.getLogger(__name__)

_MAX_EMPTY_TURNS = 2
"""How many content-only (no tool call) turns to nudge before giving up."""

_MAX_TOOL_RESULT_CHARS = 4000
"""Bound one tool result fed back into the prompt, so a search cannot blow up context."""

CraftEventSink = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class CraftResult:
    """The outcome of one crafting run."""

    bundle: dict[str, Any]
    report: ValidationReport
    trace: list[dict[str, Any]]
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    cost_usd: Decimal | None = None
    latency_ms: int = 0
    iterations: int = 0
    finished: bool = False
    stop_reason: str = "finished"


@dataclass(slots=True)
class CraftAgent:
    """Drives a model through the tools to build a validated FHIR Bundle."""

    gateway: LlmGateway
    settings: Settings

    async def run(
        self,
        invocation: LlmInvocation,
        *,
        terminology: TerminologyClient,
        cascade: ValidationCascade,
        narrative: str,
        profiles: tuple[str, ...] = (),
        layers: frozenset[ValidationLayer] | None = None,
        max_terminology_checks: int = 500,
        ig_packages: tuple[str, ...] = (),
        conversion_id: str | None = None,
        on_event: CraftEventSink | None = None,
        authorize: bool = True,
    ) -> CraftResult:
        # Authorize once: the gates are pure and the invocation is constant, so
        # re-checking every turn would only repeat work.
        if authorize:
            self.gateway.authorize(invocation, sending_phi=True)

        draft = DraftState.new()
        await _emit(
            on_event,
            {
                "type": "started",
                "conversion_id": conversion_id,
                "bundle": draft.to_bundle(),
            },
        )
        ctx = ToolContext(
            draft=draft,
            terminology=terminology,
            cascade=cascade,
            profiles=profiles,
            layers=layers,
            max_terminology_checks=max_terminology_checks,
            ig_packages=ig_packages,
            conversion_id=conversion_id,
        )

        profile_text = ", ".join(profiles) if profiles else "none"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": NARRATIVE_TO_DRAFT_AGENT.system},
            {
                "role": "user",
                "content": NARRATIVE_TO_DRAFT_AGENT.render_user(
                    narrative=narrative, profiles=profile_text
                ),
            },
        ]

        trace: list[dict[str, Any]] = []
        usage_total: dict[str, int] = {}
        cost_total: Decimal | None = None
        latency_total = 0
        model = invocation.model
        empty_turns = 0
        finished = False
        stop_reason = "max_iterations"
        max_iterations = self.settings.max_agent_iterations
        cost_cap = self.settings.max_cost_usd_per_conversion

        iterations = 0
        for iterations in range(1, max_iterations + 1):
            await _emit(
                on_event,
                {
                    "type": "status",
                    "phase": "llm",
                    "iteration": iterations,
                    "message": "Waiting for the model to choose its next tool",
                },
            )
            turn = await self.gateway.complete_with_tools(
                invocation,
                messages=messages,
                tools=TOOL_SCHEMAS,
                tool_choice="auto",
                authorize=False,
            )
            model = turn.model or model
            _accumulate(usage_total, turn.usage)
            latency_total += turn.latency_ms
            if turn.cost_usd is not None:
                cost_total = turn.cost_usd if cost_total is None else cost_total + turn.cost_usd

            messages.append(turn.assistant_message)

            if not turn.tool_calls:
                empty_turns += 1
                trace.append({"iteration": iterations, "event": "no_tool_call"})
                await _emit(
                    on_event,
                    {
                        "type": "status",
                        "phase": "no_tool_call",
                        "iteration": iterations,
                        "message": "The model returned no tool call; prompting it to continue",
                    },
                )
                if empty_turns > _MAX_EMPTY_TURNS:
                    stop_reason = "no_tool_calls"
                    break
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Use the provided tools to add every fact the note supports, "
                            "then call finish. Do not reply with prose."
                        ),
                    }
                )
                continue

            empty_turns = 0
            for call in turn.tool_calls:
                await _emit(
                    on_event,
                    {
                        "type": "tool",
                        "phase": "start",
                        "iteration": iterations,
                        "tool": call.name,
                    },
                )
                args, parse_error = _parse_arguments(call.arguments)
                if parse_error is not None:
                    outcome_content: dict[str, Any] = {"ok": False, "errors": [parse_error]}
                    trace.append(
                        {
                            "iteration": iterations,
                            "tool": call.name,
                            "ok": False,
                            "error": parse_error,
                        }
                    )
                    _append_tool_result(messages, call.id, outcome_content)
                    await _emit(
                        on_event,
                        {
                            "type": "tool",
                            "phase": "end",
                            "iteration": iterations,
                            "tool": call.name,
                            "ok": False,
                            "finish": False,
                            "error": parse_error,
                        },
                    )
                    continue

                bundle_before = draft.to_bundle()
                outcome = await dispatch_tool(call.name, args, ctx)
                trace.append(
                    {
                        "iteration": iterations,
                        "tool": call.name,
                        "ok": outcome.ok,
                        "finish": outcome.finish,
                        "error": outcome.error,
                    }
                )
                _append_tool_result(messages, call.id, outcome.content)
                await _emit(
                    on_event,
                    {
                        "type": "tool",
                        "phase": "end",
                        "iteration": iterations,
                        "tool": call.name,
                        "ok": outcome.ok,
                        "finish": outcome.finish,
                        "error": outcome.error,
                    },
                )
                bundle_after = draft.to_bundle()
                if bundle_after != bundle_before:
                    await _emit(
                        on_event,
                        {
                            "type": "draft",
                            "iteration": iterations,
                            "tool": call.name,
                            "bundle": bundle_after,
                        },
                    )
                if outcome.finish:
                    finished = True

            if finished:
                stop_reason = "finished"
                break

            if cost_total is not None and cost_total > cost_cap:
                stop_reason = "budget_exhausted"
                trace.append({"iteration": iterations, "event": "budget_exhausted"})
                await _emit(
                    on_event,
                    {
                        "type": "status",
                        "phase": "budget_exhausted",
                        "iteration": iterations,
                        "message": "The conversion cost budget was exhausted",
                    },
                )
                break

        await _emit(
            on_event,
            {
                "type": "status",
                "phase": "validation",
                "iteration": iterations,
                "message": "Running the final validation cascade",
            },
        )
        report = await cascade.run(
            draft.to_bundle(),
            ValidationSpec(
                profiles=profiles,
                layers=layers,
                max_terminology_checks=max_terminology_checks,
                ig_packages=ig_packages,
                conversion_id=conversion_id,
            ),
        )
        report.versions.model = {"craft": model}
        report.versions.prompt_set = PROMPT_SET_VERSION

        logger.info(
            "craft_completed",
            extra={
                "conversion_id": conversion_id,
                "iterations": iterations,
                "stop_reason": stop_reason,
                "decision": str(report.status),
                "resource_count": report.resource_count,
                "toolset": AGENT_TOOLSET_VERSION,
            },
        )

        return CraftResult(
            bundle=draft.to_bundle(),
            report=report,
            trace=trace,
            model=model,
            usage=usage_total,
            cost_usd=cost_total,
            latency_ms=latency_total,
            iterations=iterations,
            finished=finished,
            stop_reason=stop_reason,
        )


async def _emit(sink: CraftEventSink | None, event: dict[str, Any]) -> None:
    if sink is not None:
        await sink(event)


def _accumulate(total: dict[str, int], addition: dict[str, int]) -> None:
    for key, value in addition.items():
        if isinstance(value, int):
            total[key] = total.get(key, 0) + value


def _parse_arguments(raw: str) -> tuple[dict[str, Any], str | None]:
    text = (raw or "").strip()
    if not text:
        return {}, None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, f"tool arguments were not valid JSON: {exc}"
    if not isinstance(parsed, dict):
        return {}, "tool arguments must be a JSON object"
    return parsed, None


def _append_tool_result(
    messages: list[dict[str, Any]], call_id: str, content: dict[str, Any]
) -> None:
    body = json.dumps(content, separators=(",", ":"))
    if len(body) > _MAX_TOOL_RESULT_CHARS:
        body = body[:_MAX_TOOL_RESULT_CHARS] + '..."}'
    messages.append({"role": "tool", "tool_call_id": call_id, "content": body})


__all__ = ["CraftAgent", "CraftEventSink", "CraftResult"]
