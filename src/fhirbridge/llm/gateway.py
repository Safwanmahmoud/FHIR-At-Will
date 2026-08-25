"""The BYOK/BYOM provider gateway (AGENTS.md 7).

This is the harness around the model call, and it is the point of the whole
milestone: the value is not that we can call an LLM, it is that every call is
gated by policy the operator controls and that its output is measured before it
is trusted.

Order of operations for a call:

1. **Authorize** (:meth:`LlmGateway.authorize`) — provider allowed, egress host
   on the allowlist, PHI egress acknowledged, model qualified. All of this is
   pure and runs before any network call, so a blocked request costs nothing and
   leaks nothing.
2. **Budget** — a best-effort pre-flight estimate refuses a call that would
   exceed ``MAX_COST_USD_PER_CONVERSION``; ``max_tokens`` bounds the worst case.
3. **Call** — litellm is imported lazily (it is heavy and unused by validate-only
   deployments) and provider exceptions are mapped onto the stable error
   catalogue.
4. **Parse** — the completion is required to be a single JSON object; anything
   else is ``llm-schema-violation``, not a silently-accepted string.

Prompt and completion content is never logged unless ``DEBUG_CAPTURE_LLM_IO`` is
set, which production forbids (principle 2.6).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final

from fhirbridge.config import QualificationTier, Settings
from fhirbridge.domain.errors import (
    BudgetExceededError,
    EgressBlockedError,
    FhirbridgeError,
    LlmAuthFailedError,
    LlmContentFilteredError,
    LlmContextExceededError,
    LlmQuotaExhaustedError,
    LlmRateLimitedError,
    LlmSchemaViolationError,
    ModelNotQualifiedError,
    PhiEgressNotAcknowledgedError,
)
from fhirbridge.llm.invocation import LlmInvocation
from fhirbridge.llm.qualification import resolve_tier

logger = logging.getLogger(__name__)

_LOOPBACK: Final[frozenset[str]] = frozenset({"localhost", "127.0.0.1", "::1"})

DEFAULT_MAX_TOKENS: Final[int] = 4096
"""Bounds the worst-case cost of a single completion when pricing is unknown."""

_PROBE_MAX_TOKENS: Final[int] = 16
_PROBE_PROMPT: Final[str] = "Reply with the single word: ok"

_LLM_CALL_TIMEOUT_S: Final[float] = 120.0


@dataclass(frozen=True, slots=True)
class LlmResult:
    """The outcome of a JSON completion."""

    resource: dict[str, Any]
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    cost_usd: Decimal | None = None
    latency_ms: int = 0
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True)
class LlmProbeResult:
    """The outcome of a connectivity probe. Carries no PHI."""

    model: str
    tier: QualificationTier
    latency_ms: int
    cost_usd: Decimal | None = None
    sample: str | None = None


@dataclass(frozen=True, slots=True)
class LlmToolCall:
    """One tool the model asked to run, with its raw JSON argument string."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class LlmToolTurn:
    """One assistant turn in a tool-calling loop.

    ``assistant_message`` is the OpenAI-shaped message to append to the running
    history before the tool results, so the next call sees its own tool_calls.
    """

    content: str
    tool_calls: tuple[LlmToolCall, ...]
    assistant_message: dict[str, Any]
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    cost_usd: Decimal | None = None
    latency_ms: int = 0
    finish_reason: str | None = None


@dataclass(slots=True)
class LlmGateway:
    """Policy-enforcing entry point for every LLM call."""

    settings: Settings

    # --- Policy (pure, pre-network) ---------------------------------------

    def authorize(self, invocation: LlmInvocation, *, sending_phi: bool) -> QualificationTier:
        """Enforce every policy gate, returning the resolved qualification tier.

        Raises the specific catalogue error for the first gate that fails.
        """
        if not self.settings.provider_allowed(invocation.provider):
            raise EgressBlockedError(
                "This LLM provider is not permitted by server policy.",
                safe_context={"provider": invocation.provider},
            )

        host = invocation.egress_host
        self._check_egress(host)

        if (
            sending_phi
            and self.settings.require_phi_egress_ack
            and host not in _LOOPBACK
            and not invocation.phi_egress_acknowledged
        ):
            raise PhiEgressNotAcknowledgedError(
                "Set X-PHI-Egress-Acknowledged: true to send clinical content to an "
                "external provider.",
                safe_context={"host": host},
            )

        tier = resolve_tier(invocation.model)
        if not tier.satisfies(self.settings.min_qualification_tier):
            raise ModelNotQualifiedError(
                "This model is below the configured minimum qualification tier. Qualify "
                "it, choose a qualified model, or lower MIN_QUALIFICATION_TIER.",
                safe_context={
                    "model": invocation.model,
                    "tier": str(tier),
                    "required": str(self.settings.min_qualification_tier),
                },
            )
        return tier

    def _check_egress(self, host: str) -> None:
        if self.settings.local_only_mode:
            if host not in _LOOPBACK:
                raise EgressBlockedError(
                    "LOCAL_ONLY_MODE is on; only loopback LLM endpoints are permitted.",
                    safe_context={"host": host},
                )
            return
        allowed = {_normalize_host(entry) for entry in self.settings.llm_egress_allowlist}
        if not host or host not in allowed:
            raise EgressBlockedError(
                "The target LLM host is not in LLM_EGRESS_ALLOWLIST.",
                safe_context={"host": host},
            )

    # --- Completions ------------------------------------------------------

    async def complete_json(
        self,
        invocation: LlmInvocation,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LlmResult:
        """Authorize, call the provider, and parse a single JSON object back."""
        self.authorize(invocation, sending_phi=True)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        self._enforce_budget(invocation, messages, max_tokens)

        response, latency_ms = await self._acompletion(
            invocation, messages, max_tokens=max_tokens, json_mode=True
        )
        content = _first_content(response)
        resource = _extract_json_object(content)
        return LlmResult(
            resource=resource,
            model=_response_model(response, invocation),
            usage=_usage(response),
            cost_usd=_completion_cost(response),
            latency_ms=latency_ms,
            finish_reason=_finish_reason(response),
        )

    async def complete_with_tools(
        self,
        invocation: LlmInvocation,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: str = "auto",
        max_tokens: int = DEFAULT_MAX_TOKENS,
        authorize: bool = True,
    ) -> LlmToolTurn:
        """Run one tool-calling turn: the model may answer or ask to run tools.

        ``authorize`` defaults on so a single call is safe on its own; the agent
        loop authorizes once up front and passes ``authorize=False`` thereafter,
        since the gates are pure and the invocation does not change between turns.
        """
        if authorize:
            self.authorize(invocation, sending_phi=True)
        self._enforce_budget(invocation, messages, max_tokens)

        response, latency_ms = await self._acompletion(
            invocation,
            messages,
            max_tokens=max_tokens,
            json_mode=False,
            tools=tools,
            tool_choice=tool_choice,
        )
        content = _first_content(response)
        tool_calls = _tool_calls(response)
        return LlmToolTurn(
            content=content,
            tool_calls=tool_calls,
            assistant_message=_assistant_message(content, tool_calls),
            model=_response_model(response, invocation),
            usage=_usage(response),
            cost_usd=_completion_cost(response),
            latency_ms=latency_ms,
            finish_reason=_finish_reason(response),
        )

    async def probe(self, invocation: LlmInvocation) -> LlmProbeResult:
        """Verify connectivity and credentials with a trivial, PHI-free prompt."""
        tier = self.authorize(invocation, sending_phi=False)
        messages = [{"role": "user", "content": _PROBE_PROMPT}]
        response, latency_ms = await self._acompletion(
            invocation, messages, max_tokens=_PROBE_MAX_TOKENS, json_mode=False
        )
        return LlmProbeResult(
            model=_response_model(response, invocation),
            tier=tier,
            latency_ms=latency_ms,
            cost_usd=_completion_cost(response),
            sample=_first_content(response).strip()[:200] or None,
        )

    # --- Provider transport (lazy litellm) --------------------------------

    async def _acompletion(
        self,
        invocation: LlmInvocation,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        json_mode: bool,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> tuple[Any, int]:
        import litellm  # lazy: heavy import, and validate-only deployments never call an LLM

        kwargs = self._call_kwargs(
            invocation,
            messages,
            max_tokens=max_tokens,
            json_mode=json_mode,
            tools=tools,
            tool_choice=tool_choice,
        )
        if self.settings.debug_capture_llm_io:
            logger.warning("llm_request_captured", extra={"messages": messages})

        started = time.perf_counter()
        try:
            response = await litellm.acompletion(**kwargs)
        except Exception as exc:
            # Broad by necessity: litellm raises a wide family of provider errors.
            # Known ones map onto the catalogue; anything else is re-raised and
            # surfaces as a generic 500 rather than a wrong, confident code.
            mapped = _map_llm_exception(exc)
            if mapped is not None:
                raise mapped from exc
            raise
        latency_ms = int((time.perf_counter() - started) * 1000)
        return response, latency_ms

    def _call_kwargs(
        self,
        invocation: LlmInvocation,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        json_mode: bool,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": _litellm_model(invocation),
            "messages": messages,
            "api_key": invocation.api_key.get_secret_value(),
            "temperature": 0,
            "max_tokens": max_tokens,
            "timeout": _LLM_CALL_TIMEOUT_S,
        }
        if invocation.base_url:
            kwargs["api_base"] = invocation.base_url
        if invocation.extra_headers:
            kwargs["extra_headers"] = dict(invocation.extra_headers)
        if tools:
            # Tool calling and forced JSON output are mutually exclusive across
            # providers, so a tools call never also sets response_format.
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"
        elif json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        return kwargs

    def _enforce_budget(
        self, invocation: LlmInvocation, messages: list[dict[str, str]], max_tokens: int
    ) -> None:
        estimate = self._estimate_max_cost(invocation, messages, max_tokens)
        if estimate is None:
            return
        cap = self.settings.max_cost_usd_per_conversion
        if estimate > cap:
            raise BudgetExceededError(
                "The estimated worst-case cost of this call exceeds MAX_COST_USD_PER_CONVERSION.",
                safe_context={"estimate_usd": float(estimate), "cap_usd": float(cap)},
            )

    def _estimate_max_cost(
        self, invocation: LlmInvocation, messages: list[dict[str, str]], max_tokens: int
    ) -> Decimal | None:
        """Best-effort worst-case cost, or ``None`` when pricing is unknown.

        Returning ``None`` deliberately does not block the call: an unknown price
        is not evidence of an over-budget one, and ``max_tokens`` still bounds the
        exposure. The post-call actual cost is always recorded on the result.
        """
        try:
            import litellm

            model = _litellm_model(invocation)
            prompt_tokens = int(litellm.token_counter(model=model, messages=messages))
            prompt_cost, completion_cost = litellm.cost_per_token(
                model=model, prompt_tokens=prompt_tokens, completion_tokens=max_tokens
            )
        except Exception:
            # Pricing and token counting are advisory; an unknown price is not
            # evidence of an over-budget one, so never fail the call on it.
            return None
        total = (prompt_cost or 0.0) + (completion_cost or 0.0)
        if total <= 0:
            return None
        return Decimal(str(total))


def _litellm_model(invocation: LlmInvocation) -> str:
    """The model id litellm expects.

    OpenRouter is addressed through litellm's ``openrouter/`` routing prefix; for
    any other provider the caller is expected to pass a litellm-compatible model
    id (for example ``anthropic/claude-3.5-sonnet`` or ``gpt-4o``).
    """
    if invocation.provider == "openrouter" and not invocation.model.startswith("openrouter/"):
        return f"openrouter/{invocation.model}"
    return invocation.model


def _normalize_host(entry: str) -> str:
    from urllib.parse import urlparse

    text = entry.strip().lower()
    if not text:
        return ""
    if "://" in text:
        return (urlparse(text).hostname or "").lower()
    return text.split("/", 1)[0].split(":", 1)[0]


def _first_content(response: Any) -> str:
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError):
        return ""
    return content if isinstance(content, str) else ""


def _tool_calls(response: Any) -> tuple[LlmToolCall, ...]:
    """Extract the tool calls a model requested, tolerating provider variance."""
    try:
        raw = response.choices[0].message.tool_calls or []
    except (AttributeError, IndexError, TypeError):
        return ()
    calls: list[LlmToolCall] = []
    for item in raw:
        function = getattr(item, "function", None)
        name = getattr(function, "name", None)
        if not isinstance(name, str) or not name:
            continue
        arguments = getattr(function, "arguments", "") or ""
        call_id = getattr(item, "id", None) or f"call_{len(calls)}"
        calls.append(
            LlmToolCall(
                id=str(call_id),
                name=name,
                arguments=arguments if isinstance(arguments, str) else "",
            )
        )
    return tuple(calls)


def _assistant_message(content: str, tool_calls: tuple[LlmToolCall, ...]) -> dict[str, Any]:
    """Rebuild the assistant message to feed back into the running history."""
    message: dict[str, Any] = {"role": "assistant", "content": content or ""}
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in tool_calls
        ]
    return message


def _finish_reason(response: Any) -> str | None:
    try:
        reason = response.choices[0].finish_reason
    except (AttributeError, IndexError, TypeError):
        return None
    return reason if isinstance(reason, str) else None


def _response_model(response: Any, invocation: LlmInvocation) -> str:
    model = getattr(response, "model", None)
    return model if isinstance(model, str) and model else invocation.model


def _usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    out: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(usage, key, None)
        if isinstance(value, int):
            out[key] = value
    return out


def _completion_cost(response: Any) -> Decimal | None:
    try:
        import litellm

        # Typed as float upstream, but treated as optional here: litellm has
        # returned None for unpriced models across versions, and a wrong non-None
        # is worse than a missing cost.
        cost: Any = litellm.completion_cost(completion_response=response)
    except Exception:
        # Cost is advisory provenance metadata, never fatal to the response.
        return None
    if cost is None:
        return None
    try:
        return Decimal(str(cost))
    except (ValueError, ArithmeticError):
        return None


def _extract_json_object(content: str) -> dict[str, Any]:
    """Parse a single JSON object out of a completion, tolerating a code fence."""
    text = _strip_code_fence(content.strip())
    if not text:
        raise LlmSchemaViolationError("The model returned an empty completion.")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LlmSchemaViolationError("The model did not return a valid JSON object.") from exc
    if not isinstance(parsed, dict):
        raise LlmSchemaViolationError("The model returned JSON that was not an object.")
    return parsed


def _strip_code_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    # Drop the opening ``` (with optional language) and a closing fence if present.
    lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _map_llm_exception(exc: Exception) -> FhirbridgeError | None:
    """Map a litellm exception onto the stable catalogue, or ``None`` to re-raise.

    Matched by class name so litellm is not imported here (this runs in the
    ``except`` of the lazy-import call site). Returning ``None`` lets a genuinely
    unexpected error surface as a generic 500 rather than a wrong, confident code.
    """
    name = type(exc).__name__
    text = str(exc).lower()
    match name:
        case "AuthenticationError" | "PermissionDeniedError":
            return LlmAuthFailedError("The LLM provider rejected the supplied API key.")
        case "RateLimitError":
            if any(marker in text for marker in ("quota", "insufficient_quota", "billing")):
                return LlmQuotaExhaustedError(
                    "The LLM provider reports the account quota is exhausted."
                )
            return LlmRateLimitedError(
                "The LLM provider rate-limited this request.", retry_after_s=5
            )
        case "ContextWindowExceededError":
            return LlmContextExceededError("The input exceeds the model's context window.")
        case "ContentPolicyViolationError":
            return LlmContentFilteredError(
                "The LLM provider's content filter blocked this request."
            )
        case "Timeout" | "APIConnectionError" | "ServiceUnavailableError" | "InternalServerError":
            # A transient upstream failure. The only retryable LLM code is
            # rate-limited, so it carries the retry signal even though the cause
            # differs; the message says so plainly.
            return LlmRateLimitedError(
                "The LLM provider is temporarily unavailable; retry shortly.",
                retry_after_s=5,
            )
        case _:
            return None


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "LlmGateway",
    "LlmProbeResult",
    "LlmResult",
    "LlmToolCall",
    "LlmToolTurn",
]
