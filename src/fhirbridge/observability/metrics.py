"""Prometheus metrics (AGENTS.md 15).

Every label value here is drawn from a closed, low-cardinality set: route
templates, layer names, severities, dependency names. Never label a metric with
a patient identifier, a document id, a code display, or any free text — that is
both a PHI leak (principle 2.6) and a cardinality bomb.
"""

from __future__ import annotations

from typing import Final

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, Info, generate_latest
from prometheus_client.openmetrics.exposition import CONTENT_TYPE_LATEST as OPENMETRICS_CONTENT_TYPE

REGISTRY: Final = CollectorRegistry(auto_describe=True)
"""A dedicated registry so tests can assert on a clean snapshot."""

CONTENT_TYPE: Final[str] = "text/plain; version=0.0.4; charset=utf-8"

BUILD_INFO: Final = Info(
    "fhirbridge_build",
    "Build and pin information for this process.",
    registry=REGISTRY,
)

# --- HTTP ------------------------------------------------------------------
HTTP_REQUESTS: Final = Counter(
    "fhirbridge_http_requests_total",
    "HTTP requests handled, by route template and status class.",
    labelnames=("method", "route", "status"),
    registry=REGISTRY,
)

HTTP_DURATION: Final = Histogram(
    "fhirbridge_http_request_duration_seconds",
    "HTTP request latency by route template.",
    labelnames=("method", "route"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
    registry=REGISTRY,
)

# --- Validation cascade ----------------------------------------------------
VALIDATION_RUNS: Final = Counter(
    "fhirbridge_validation_runs_total",
    "Validation cascade runs by terminal routing outcome.",
    labelnames=("outcome",),
    registry=REGISTRY,
)

VALIDATION_LAYER_DURATION: Final = Histogram(
    "fhirbridge_validation_layer_duration_seconds",
    "Latency of each validation layer.",
    labelnames=("layer",),
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 15.0, 60.0),
    registry=REGISTRY,
)

VALIDATION_ISSUES: Final = Counter(
    "fhirbridge_validation_issues_total",
    "Validation issues emitted, by layer and severity.",
    labelnames=("layer", "severity"),
    registry=REGISTRY,
)

VALIDATION_LAYER_SKIPPED: Final = Counter(
    "fhirbridge_validation_layer_skipped_total",
    "Validation layers skipped, by layer and reason.",
    labelnames=("layer", "reason"),
    registry=REGISTRY,
)

# --- Dependencies (validator sidecar, terminology server, db, redis) -------
DEPENDENCY_UP: Final = Gauge(
    "fhirbridge_dependency_up",
    "1 when the dependency answered its last health probe, else 0.",
    labelnames=("dependency",),
    registry=REGISTRY,
)

RLS_ENFORCED: Final = Gauge(
    "fhirbridge_rls_enforced",
    "1 when Postgres reports row-level security is active for the connected role, "
    "else 0. A 0 here means tenant isolation is not being enforced by the database "
    "and should page someone.",
    registry=REGISTRY,
)

DEPENDENCY_DURATION: Final = Histogram(
    "fhirbridge_dependency_request_duration_seconds",
    "Outbound dependency call latency.",
    labelnames=("dependency", "operation"),
    buckets=(0.005, 0.025, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
    registry=REGISTRY,
)

DEPENDENCY_FAILURES: Final = Counter(
    "fhirbridge_dependency_failures_total",
    "Outbound dependency failures by normalized reason.",
    labelnames=("dependency", "operation", "reason"),
    registry=REGISTRY,
)

# --- Terminology -----------------------------------------------------------
TERMINOLOGY_VALIDATE_CODE: Final = Counter(
    "fhirbridge_terminology_validate_code_total",
    "$validate-code calls by result.",
    labelnames=("result",),
    registry=REGISTRY,
)

TERMINOLOGY_CACHE: Final = Counter(
    "fhirbridge_terminology_cache_total",
    "Terminology lookup cache outcomes.",
    labelnames=("operation", "outcome"),
    registry=REGISTRY,
)

# --- Pipeline (declared now, exercised from M3) ---------------------------
SPAN_VERIFICATION_FAILURES: Final = Counter(
    "fhirbridge_span_verification_failures_total",
    "Facts dropped because normalized_text[start:end] != quote (principle 2.2).",
    labelnames=("kind",),
    registry=REGISTRY,
)


def set_build_info(**labels: str) -> None:
    """Publish immutable build/pin information exactly once per process."""
    BUILD_INFO.info(labels)


def render() -> bytes:
    """Serialize the registry in the Prometheus text exposition format."""
    return generate_latest(REGISTRY)


__all__ = [
    "BUILD_INFO",
    "CONTENT_TYPE",
    "DEPENDENCY_DURATION",
    "DEPENDENCY_FAILURES",
    "DEPENDENCY_UP",
    "HTTP_DURATION",
    "HTTP_REQUESTS",
    "OPENMETRICS_CONTENT_TYPE",
    "REGISTRY",
    "RLS_ENFORCED",
    "SPAN_VERIFICATION_FAILURES",
    "TERMINOLOGY_CACHE",
    "TERMINOLOGY_VALIDATE_CODE",
    "VALIDATION_ISSUES",
    "VALIDATION_LAYER_DURATION",
    "VALIDATION_LAYER_SKIPPED",
    "VALIDATION_RUNS",
    "render",
    "set_build_info",
]
