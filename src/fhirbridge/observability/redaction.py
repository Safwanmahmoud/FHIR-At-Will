"""Secret redaction (AGENTS.md 7.8).

This is a defence in depth, not the primary control. The primary control is that
secrets live only in :class:`pydantic.SecretStr` and never reach a log call. This
module exists because third-party libraries (``httpx``, ``litellm``, provider
SDKs) do not share that discipline, and because tracebacks stringify locals.

Patterns are deliberately broad. A false positive costs a garbled log line; a
false negative leaks a customer's API key.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final

REDACTED: Final[str] = "[REDACTED]"

_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    # --- Provider-specific key shapes ------------------------------------
    # OpenAI (incl. sk-proj-, sk-svcacct-) and Anthropic (sk-ant-).
    ("openai_or_anthropic_key", re.compile(r"\bsk-[A-Za-z0-9_-]{12,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    # AWS access key ids: AKIA/ASIA/ABIA/ACCA + 16 base32 chars.
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")),
    ("google_oauth_token", re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}")),
    ("groq_key", re.compile(r"\bgsk_[A-Za-z0-9]{20,}")),
    ("xai_key", re.compile(r"\bxai-[A-Za-z0-9]{20,}")),
    ("mistral_or_generic_hex_key", re.compile(r"\b(?:r8_|hf_|gh[pousr]_)[A-Za-z0-9]{16,}")),
    ("openrouter_key", re.compile(r"\bsk-or-v1-[A-Za-z0-9]{16,}")),
    # --- Bearer / basic credentials in headers or URLs -------------------
    (
        "authorization_header",
        re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}"),
    ),
    # JWTs: three dot-separated base64url segments starting with a JSON header.
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}"),
    ),
    ("url_userinfo", re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")),
    # --- Structured assignments (JSON, kwargs, query strings, headers) ---
    (
        "keyed_assignment",
        re.compile(
            r"""(?ix)
            (
              ["']?
              (?: api[_-]?key | apikey
                | secret(?:[_-]?(?:key|access[_-]?key))?
                | access[_-]?token | refresh[_-]?token | id[_-]?token
                | client[_-]?secret | password | passwd
                | authorization | auth[_-]?token
                | x-llm-api-key | x-api-key | anthropic-api-key
                | master[_-]?key | ephemeral[_-]?key
                | aws[_-]?secret[_-]?access[_-]?key
              )
              ["']?
              \s* (?: [:=] | =\> ) \s*
              ["']?
            )
            ( [^\s,;&"'})\]]{4,} )
            """
        ),
    ),
)

_KEYED_ASSIGNMENT_NAME: Final = "keyed_assignment"

_SENSITIVE_KEY_NAMES: Final[frozenset[str]] = frozenset(
    {
        "api_key",
        "apikey",
        "api-key",
        "authorization",
        "auth_token",
        "access_token",
        "refresh_token",
        "id_token",
        "client_secret",
        "password",
        "passwd",
        "secret",
        "secret_key",
        "master_key",
        "ephemeral_key",
        "fhirbridge_master_key",
        "fhirbridge_ephemeral_key",
        "x-api-key",
        "x-llm-api-key",
        "anthropic-api-key",
        "aws_secret_access_key",
        "extra_headers",
        "x-llm-extra-headers",
    }
)


def redact_text(value: str) -> str:
    """Replace anything that looks like a credential with ``[REDACTED]``."""
    result = value
    for name, pattern in _PATTERNS:
        if name == _KEYED_ASSIGNMENT_NAME:
            result = pattern.sub(lambda m: f"{m.group(1)}{REDACTED}", result)
        else:
            result = pattern.sub(REDACTED, result)
    return result


def is_sensitive_key(key: str) -> bool:
    """True when a mapping key names a field that must never be logged."""
    return key.strip().lower().replace(" ", "") in _SENSITIVE_KEY_NAMES


def redact_object(value: object, *, _depth: int = 0) -> object:
    """Recursively redact strings, and drop values under sensitive keys entirely.

    Depth is bounded so a self-referential structure cannot hang a log call.
    """
    if _depth > 8:
        return "[TRUNCATED]"
    match value:
        case str():
            return redact_text(value)
        case bool() | int() | float() | None:
            return value
        case dict():
            return {
                str(k): (
                    REDACTED if is_sensitive_key(str(k)) else redact_object(v, _depth=_depth + 1)
                )
                for k, v in value.items()
            }
        case list() | tuple() | set() | frozenset():
            return [redact_object(v, _depth=_depth + 1) for v in value]
        case _:
            return redact_text(repr(value))


def contains_secret_like(value: str, *, extra: Iterable[str] = ()) -> bool:
    """True when ``value`` still looks like it contains a credential.

    Used by the security test suite to assert that no log record, error body,
    trace attribute or database column carries a secret.
    """
    if any(literal and literal in value for literal in extra):
        return True
    return any(pattern.search(value) for _, pattern in _PATTERNS)


__all__ = [
    "REDACTED",
    "contains_secret_like",
    "is_sensitive_key",
    "redact_object",
    "redact_text",
]
