"""The per-request BYOK invocation, parsed from ``X-LLM-*`` headers (AGENTS.md 7.1).

fhirbridge is BYOK by default: the server holds no LLM credential. The caller
supplies the provider, model and key on each request, and this module turns the
raw headers into a validated :class:`LlmInvocation`. Two properties matter here:

* The API key is wrapped in :class:`~pydantic.SecretStr` the moment it is read,
  so a stray ``repr`` or log line cannot spill it (principle 2.7).
* ``egress_host`` resolves the hostname traffic will actually reach, which is
  what the gateway checks against ``LLM_EGRESS_ALLOWLIST`` before any call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlparse

from pydantic import SecretStr

from fhirbridge.domain.errors import InvalidRequestError, LlmCredentialsRequiredError

HEADER_PROVIDER: Final[str] = "X-LLM-Provider"
HEADER_MODEL: Final[str] = "X-LLM-Model"
HEADER_BASE_URL: Final[str] = "X-LLM-Base-Url"
HEADER_API_KEY: Final[str] = "X-LLM-API-Key"
HEADER_EXTRA_HEADERS: Final[str] = "X-LLM-Extra-Headers"
HEADER_PHI_ACK: Final[str] = "X-PHI-Egress-Acknowledged"

# The dictation (speech-to-text) call carries its own credentials, because it is a
# different provider from extraction: litellm cannot transcribe through OpenRouter
# (the extraction default), so voice conversion routes audio to Gemini, OpenAI,
# Groq, or another STT-capable provider on a separate key. The PHI-egress
# acknowledgement is shared — one header covers every external hop in a request.
HEADER_STT_PROVIDER: Final[str] = "X-STT-Provider"
HEADER_STT_MODEL: Final[str] = "X-STT-Model"
HEADER_STT_BASE_URL: Final[str] = "X-STT-Base-Url"
HEADER_STT_API_KEY: Final[str] = "X-STT-API-Key"
HEADER_STT_EXTRA_HEADERS: Final[str] = "X-STT-Extra-Headers"
HEADER_STT_LANGUAGE: Final[str] = "X-STT-Language"

DEFAULT_PROVIDER: Final[str] = "openrouter"
"""Provider assumed when the caller sends a model and key but no provider."""

DEFAULT_STT_PROVIDER: Final[str] = "gemini"
"""Provider assumed for dictation when the caller sends a model and key but no provider."""

_PROVIDER_DEFAULT_BASE_URL: Final[dict[str, str]] = {
    "openrouter": "https://openrouter.ai/api/v1",
    "gemini": "https://generativelanguage.googleapis.com",
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
}
"""Where a provider's traffic goes when the caller supplies no explicit base URL.

Only used to resolve the egress host for the allowlist check; litellm knows the
real base URLs itself. Entries beyond OpenRouter exist so a dictation provider's
host resolves for the allowlist without the caller hand-typing a base URL; it only
ever *enables* an allowlist match, never bypasses one.
"""

_TRUE_TOKENS: Final[frozenset[str]] = frozenset({"true", "1", "yes", "on"})


def _as_bool(value: str | None) -> bool:
    return value is not None and value.strip().lower() in _TRUE_TOKENS


@dataclass(frozen=True, slots=True)
class LlmInvocation:
    """A single caller-supplied LLM request context."""

    provider: str
    model: str
    api_key: SecretStr
    base_url: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    phi_egress_acknowledged: bool = False

    @property
    def egress_host(self) -> str:
        """The hostname this invocation's traffic will reach, lowercased.

        Empty when neither a base URL nor a known provider default resolves one;
        the gateway treats an unresolvable host as blocked rather than allowed.
        """
        target = self.base_url or _PROVIDER_DEFAULT_BASE_URL.get(self.provider, "")
        if not target:
            return ""
        return (urlparse(target).hostname or "").lower()

    @classmethod
    def from_headers(
        cls,
        *,
        provider: str | None,
        model: str | None,
        api_key: str | None,
        base_url: str | None = None,
        extra_headers: str | None = None,
        phi_ack: str | None = None,
    ) -> LlmInvocation:
        """Build an invocation from raw header strings, or raise.

        A missing key raises ``llm-credentials-required`` rather than a generic
        validation error: BYOK means the absence of a key is the expected,
        documented failure, not a malformed request.
        """
        key = (api_key or "").strip()
        if not key:
            raise LlmCredentialsRequiredError(
                "Supply your LLM API key in the X-LLM-API-Key header (BYOK)."
            )
        resolved_model = (model or "").strip()
        if not resolved_model:
            raise InvalidRequestError(
                "The X-LLM-Model header is required, e.g. 'openai/gpt-4o-mini'.",
                safe_context={"header": HEADER_MODEL},
            )
        return cls(
            provider=(provider or DEFAULT_PROVIDER).strip().lower(),
            model=resolved_model,
            api_key=SecretStr(key),
            base_url=(base_url or "").strip() or None,
            extra_headers=_parse_extra_headers(extra_headers),
            phi_egress_acknowledged=_as_bool(phi_ack),
        )


@dataclass(frozen=True, slots=True)
class SttInvocation:
    """A single caller-supplied speech-to-text request context.

    Deliberately a sibling of :class:`LlmInvocation` rather than the same type: the
    two calls in a voice conversion go to different providers on different keys, and
    conflating them would let one credential stand in for the other. It carries the
    same shape the gateway transport needs (``provider``/``model``/``api_key``/
    ``base_url``/``extra_headers``) so one code path can drive both, plus an optional
    ``language`` hint that only transcription uses.
    """

    provider: str
    model: str
    api_key: SecretStr
    base_url: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    phi_egress_acknowledged: bool = False
    language: str | None = None

    @property
    def egress_host(self) -> str:
        """The hostname this invocation's traffic will reach, lowercased."""
        target = self.base_url or _PROVIDER_DEFAULT_BASE_URL.get(self.provider, "")
        if not target:
            return ""
        return (urlparse(target).hostname or "").lower()

    @classmethod
    def from_headers(
        cls,
        *,
        provider: str | None,
        model: str | None,
        api_key: str | None,
        base_url: str | None = None,
        extra_headers: str | None = None,
        phi_ack: str | None = None,
        language: str | None = None,
    ) -> SttInvocation:
        """Build a dictation invocation from raw header strings, or raise."""
        key = (api_key or "").strip()
        if not key:
            raise LlmCredentialsRequiredError(
                "Supply your speech-to-text API key in the X-STT-API-Key header (BYOK)."
            )
        resolved_model = (model or "").strip()
        if not resolved_model:
            raise InvalidRequestError(
                "The X-STT-Model header is required, e.g. 'gemini-2.5-flash'.",
                safe_context={"header": HEADER_STT_MODEL},
            )
        return cls(
            provider=(provider or DEFAULT_STT_PROVIDER).strip().lower(),
            model=resolved_model,
            api_key=SecretStr(key),
            base_url=(base_url or "").strip() or None,
            extra_headers=_parse_extra_headers(extra_headers, header=HEADER_STT_EXTRA_HEADERS),
            phi_egress_acknowledged=_as_bool(phi_ack),
            language=(language or "").strip() or None,
        )


def _parse_extra_headers(raw: str | None, *, header: str = HEADER_EXTRA_HEADERS) -> dict[str, str]:
    """Parse the optional ``X-LLM-Extra-Headers`` JSON object of string values.

    OpenRouter, for instance, reads ``HTTP-Referer`` and ``X-Title`` from here.
    Anything that is not a flat object of strings is rejected loudly rather than
    silently dropped, so a client learns its header was ignored.
    """
    if raw is None or not raw.strip():
        return {}
    message = f"{header} must be a JSON object of string values."
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidRequestError(message, safe_context={"header": header}) from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in parsed.items()
    ):
        raise InvalidRequestError(message, safe_context={"header": header})
    return {str(key): str(value) for key, value in parsed.items()}


__all__ = [
    "DEFAULT_PROVIDER",
    "DEFAULT_STT_PROVIDER",
    "HEADER_API_KEY",
    "HEADER_BASE_URL",
    "HEADER_EXTRA_HEADERS",
    "HEADER_MODEL",
    "HEADER_PHI_ACK",
    "HEADER_PROVIDER",
    "HEADER_STT_API_KEY",
    "HEADER_STT_BASE_URL",
    "HEADER_STT_EXTRA_HEADERS",
    "HEADER_STT_LANGUAGE",
    "HEADER_STT_MODEL",
    "HEADER_STT_PROVIDER",
    "LlmInvocation",
    "SttInvocation",
]
