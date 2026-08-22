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

DEFAULT_PROVIDER: Final[str] = "openrouter"
"""Provider assumed when the caller sends a model and key but no provider."""

_PROVIDER_DEFAULT_BASE_URL: Final[dict[str, str]] = {
    "openrouter": "https://openrouter.ai/api/v1",
}
"""Where a provider's traffic goes when the caller supplies no explicit base URL.

Only used to resolve the egress host for the allowlist check; litellm knows the
real base URLs itself.
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


def _parse_extra_headers(raw: str | None) -> dict[str, str]:
    """Parse the optional ``X-LLM-Extra-Headers`` JSON object of string values.

    OpenRouter, for instance, reads ``HTTP-Referer`` and ``X-Title`` from here.
    Anything that is not a flat object of strings is rejected loudly rather than
    silently dropped, so a client learns its header was ignored.
    """
    if raw is None or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidRequestError(
            "X-LLM-Extra-Headers must be a JSON object of string values.",
            safe_context={"header": HEADER_EXTRA_HEADERS},
        ) from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in parsed.items()
    ):
        raise InvalidRequestError(
            "X-LLM-Extra-Headers must be a JSON object of string values.",
            safe_context={"header": HEADER_EXTRA_HEADERS},
        )
    return {str(key): str(value) for key, value in parsed.items()}


__all__ = [
    "DEFAULT_PROVIDER",
    "HEADER_API_KEY",
    "HEADER_BASE_URL",
    "HEADER_EXTRA_HEADERS",
    "HEADER_MODEL",
    "HEADER_PHI_ACK",
    "HEADER_PROVIDER",
    "LlmInvocation",
]
