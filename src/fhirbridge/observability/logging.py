"""Structured JSON logging with mandatory secret redaction (AGENTS.md 15).

Every log record gets the correlation identifiers from
:mod:`fhirbridge.observability.context` and passes through
:class:`RedactionFilter`. Redaction is applied twice on purpose: the filter
catches ``msg``/``args``/``extra`` before formatting, and the formatter redacts
the fully-assembled payload, which is the only place a stringified traceback
(with its stringified locals) can be reached.
"""

from __future__ import annotations

import json
import logging
import logging.config
import sys
from typing import Any, Final

from fhirbridge.observability.context import current_context
from fhirbridge.observability.redaction import redact_object, redact_text

_RESERVED: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

# Third-party loggers that are known to print request bodies, headers or keys at
# DEBUG. Pinned at WARNING/INFO regardless of the service log level (AGENTS.md 7.8).
_NOISY_LOGGERS: Final[dict[str, int]] = {
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "hpack": logging.WARNING,
    "litellm": logging.WARNING,
    "LiteLLM": logging.WARNING,
    "openai": logging.WARNING,
    "anthropic": logging.WARNING,
    "urllib3": logging.WARNING,
    "asyncio": logging.WARNING,
    "aiosqlite": logging.WARNING,
    "sqlalchemy.engine": logging.WARNING,
    "uvicorn.access": logging.WARNING,
    "uvicorn.error": logging.INFO,
    "arq": logging.INFO,
}


class RedactionFilter(logging.Filter):
    """Redacts credential-shaped substrings from a record before formatting."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        else:
            record.msg = redact_object(record.msg)

        if record.args:
            if isinstance(record.args, dict):
                record.args = redact_object(record.args)  # type: ignore[assignment]
            else:
                record.args = tuple(redact_object(arg) for arg in record.args)

        for key, value in list(record.__dict__.items()):
            if key not in _RESERVED and not key.startswith("_"):
                record.__dict__[key] = redact_object(value)
        return True


class JsonFormatter(logging.Formatter):
    """Emits one redacted JSON object per record."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%03dZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(current_context())

        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = {
                "type": getattr(record.exc_info[0], "__name__", "Exception"),
                "traceback": self.formatException(record.exc_info),
            }
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        safe = redact_object(payload)
        return json.dumps(safe, default=str, ensure_ascii=False, sort_keys=False)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        import datetime as _dt

        moment = _dt.datetime.fromtimestamp(record.created, tz=_dt.UTC)
        return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}Z"


class PlainFormatter(logging.Formatter):
    """Human-readable formatter for local development. Still redacted."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        context = current_context()
        suffix = " " + " ".join(f"{k}={v}" for k, v in context.items()) if context else ""
        return redact_text(base + suffix)


def configure_logging(*, level: str = "INFO", json_logs: bool = True) -> None:
    """Install the root handler, redaction filter and third-party log clamps.

    Idempotent: safe to call from both the API and the worker entrypoints.
    """
    formatter: logging.Formatter = (
        JsonFormatter()
        if json_logs
        else PlainFormatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    )

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(RedactionFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    for name, clamp in _NOISY_LOGGERS.items():
        logger = logging.getLogger(name)
        logger.setLevel(max(clamp, root.level))
        logger.handlers.clear()
        logger.propagate = True

    _silence_litellm_verbose()


def _silence_litellm_verbose() -> None:
    """Turn off litellm's global verbose flag if the package is installed.

    litellm's ``set_verbose`` prints full request payloads, including the API
    key, to stdout without going through ``logging`` at all.
    """
    try:  # pragma: no cover - litellm arrives with M2
        import litellm
    except ImportError:
        return
    for attribute in ("set_verbose", "turn_off_message_logging", "suppress_debug_info"):
        if hasattr(litellm, attribute):
            value = attribute != "set_verbose"
            try:
                setattr(litellm, attribute, value)
            except Exception:
                logging.getLogger(__name__).warning("could not set litellm.%s", attribute)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


__all__ = [
    "JsonFormatter",
    "PlainFormatter",
    "RedactionFilter",
    "configure_logging",
    "get_logger",
]
