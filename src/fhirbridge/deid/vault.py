"""Request-local reversible surrogate storage.

The mapping is deliberately random and in-memory only. It must never be logged,
persisted, returned, or sent to a provider.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from typing import Any, Final

from fhirbridge.deid.spans import IdentifierClass
from fhirbridge.domain.errors import PhiMinimizationFailedError

_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"\[\[[A-Z]+_[A-Z0-9]+\]\]")


@dataclass(slots=True)
class Vault:
    """A finite per-request map from opaque surrogate to original text."""

    _surrogate_to_original: dict[str, str] = field(default_factory=dict)
    _original_to_surrogate: dict[tuple[IdentifierClass, str], str] = field(default_factory=dict)

    def surrogate_for(self, original: str, identifier_class: IdentifierClass) -> str:
        key = (identifier_class, original)
        existing = self._original_to_surrogate.get(key)
        if existing is not None:
            return existing
        while True:
            token = f"[[{identifier_class.value.upper()}_{secrets.token_hex(6).upper()}]]"
            if token not in self._surrogate_to_original:
                break
        self._surrogate_to_original[token] = original
        self._original_to_surrogate[key] = token
        return token

    def restore(self, text: str) -> str:
        restored = text
        for surrogate in sorted(self._surrogate_to_original, key=len, reverse=True):
            restored = restored.replace(surrogate, self._surrogate_to_original[surrogate])
        return restored

    def assert_originals_absent(self, payload: Any) -> None:
        rendered = _flatten_text(payload).casefold()
        for original in self._surrogate_to_original.values():
            if original and original.casefold() in rendered:
                raise PhiMinimizationFailedError()

    def assert_surrogates_absent(self, text: str) -> None:
        if any(token in text for token in self._surrogate_to_original) or _TOKEN_RE.search(text):
            raise PhiMinimizationFailedError()

    @property
    def size(self) -> int:
        return len(self._surrogate_to_original)

    def clear(self) -> None:
        self._surrogate_to_original.clear()
        self._original_to_surrogate.clear()


def _flatten_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(_flatten_text(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return "\n".join(_flatten_text(item) for item in value)
    return ""


__all__ = ["Vault"]
