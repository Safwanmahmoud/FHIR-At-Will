"""Request-scoped correlation identifiers.

Only identifiers live here. Never put document text, patient identifiers or
credentials into the log context: everything in it is written to every log line.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Final, TypedDict

REQUEST_ID_HEADER: Final[str] = "X-Request-Id"
TRACE_ID_HEADER: Final[str] = "X-Trace-Id"
"""Header names, defined here so both the middleware that sets them and the error
renderer that echoes them can import them without a cycle."""

_trace_id: ContextVar[str | None] = ContextVar("fhirbridge_trace_id", default=None)
_request_id: ContextVar[str | None] = ContextVar("fhirbridge_request_id", default=None)
_tenant_id: ContextVar[str | None] = ContextVar("fhirbridge_tenant_id", default=None)
_conversion_id: ContextVar[str | None] = ContextVar("fhirbridge_conversion_id", default=None)
_stage: ContextVar[str | None] = ContextVar("fhirbridge_stage", default=None)

_VARS: Final[dict[str, ContextVar[str | None]]] = {
    "trace_id": _trace_id,
    "request_id": _request_id,
    "tenant_id": _tenant_id,
    "conversion_id": _conversion_id,
    "stage": _stage,
}


class LogContext(TypedDict, total=False):
    """The identifier-only fields stamped onto every log record."""

    trace_id: str
    request_id: str
    tenant_id: str
    conversion_id: str
    stage: str


def current_context() -> LogContext:
    """Snapshot the set correlation identifiers."""
    result: LogContext = {}
    for name, var in _VARS.items():
        value = var.get()
        if value is not None:
            result[name] = value  # type: ignore[literal-required]  # keys match LogContext
    return result


def get_trace_id() -> str | None:
    return _trace_id.get()


def get_tenant_id() -> str | None:
    return _tenant_id.get()


@contextmanager
def bind(**values: str | None) -> Iterator[None]:
    """Bind correlation identifiers for the duration of the block."""
    tokens: list[tuple[ContextVar[str | None], Token[str | None]]] = []
    for name, value in values.items():
        var = _VARS.get(name)
        if var is None:
            raise KeyError(f"unknown log context field {name!r}")
        tokens.append((var, var.set(value)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def set_context(**values: str | None) -> None:
    """Bind identifiers without a scope. Prefer :func:`bind` where possible."""
    for name, value in values.items():
        var = _VARS.get(name)
        if var is None:
            raise KeyError(f"unknown log context field {name!r}")
        var.set(value)


__all__ = [
    "REQUEST_ID_HEADER",
    "TRACE_ID_HEADER",
    "LogContext",
    "bind",
    "current_context",
    "get_tenant_id",
    "get_trace_id",
    "set_context",
]
