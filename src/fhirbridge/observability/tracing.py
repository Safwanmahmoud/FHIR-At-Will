"""OpenTelemetry setup (AGENTS.md 15).

Tracing is opt-in: without ``OTEL_EXPORTER_OTLP_ENDPOINT`` the SDK is not
installed at all and every span becomes a no-op, which keeps the default
deployment free of a network dependency.

Span attributes are identifiers, provider names, token counts and costs. Prompt
and completion content is never attached to a span unless
``DEBUG_CAPTURE_LLM_IO=true``, which is refused outright in production by
:mod:`fhirbridge.config`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.trace import Span, Tracer

if TYPE_CHECKING:
    from fastapi import FastAPI

    from fhirbridge.config import Settings

logger = logging.getLogger(__name__)

_configured = False


def configure_tracing(settings: Settings) -> None:
    """Install the OTLP exporter when an endpoint is configured."""
    global _configured
    if _configured or not settings.otel_exporter_otlp_endpoint:
        return

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    from fhirbridge.version import CODE_VERSION

    resource = Resource.create(
        {
            "service.name": settings.service_name,
            "service.version": CODE_VERSION,
            "deployment.environment": str(settings.environment),
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint))
    )
    trace.set_tracer_provider(provider)
    _configured = True
    logger.info("tracing_configured", extra={"endpoint": settings.otel_exporter_otlp_endpoint})


def instrument_app(app: FastAPI, settings: Settings) -> None:
    """Instrument the ASGI app, if tracing is configured.

    Health and metrics routes are excluded: they are polled every few seconds by
    infrastructure and would otherwise dominate the trace volume without ever
    being read.
    """
    if not settings.otel_exporter_otlp_endpoint:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls="livez,readyz,metrics",
    )
    logger.info("tracing_instrumented")


def get_tracer(name: str = "fhirbridge") -> Tracer:
    return trace.get_tracer(name)


def current_trace_id() -> str | None:
    """The active W3C trace id, or None when tracing is not configured."""
    span = trace.get_current_span()
    context = span.get_span_context()
    if not context.is_valid:
        return None
    return format(context.trace_id, "032x")


def set_safe_attributes(span: Span, **attributes: Any) -> None:
    """Attach attributes, dropping None and refusing anything not a scalar.

    Refusing non-scalars is a cheap guard against someone attaching a whole
    request body (and therefore PHI) to a span.
    """
    for key, value in attributes.items():
        if value is None:
            continue
        if isinstance(value, str | bool | int | float):
            span.set_attribute(key, value)
        else:
            span.set_attribute(key, str(value))


__all__ = [
    "configure_tracing",
    "current_trace_id",
    "get_tracer",
    "instrument_app",
    "set_safe_attributes",
]
