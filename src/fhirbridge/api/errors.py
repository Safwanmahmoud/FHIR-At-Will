"""Exception handlers (AGENTS.md 12).

Domain errors render as a FHIR ``OperationOutcome``; platform errors render as
``{"error": {code, message, trace_id, details}}``.

The subtle one is request validation. FastAPI's ``RequestValidationError``
carries an ``input`` field holding the offending value — which, for this service,
is clinical narrative. Echoing it would put PHI in an error body, and from there
into any client that logs error responses. :func:`_sanitize_violations` keeps only
the location, the type and the message.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from fhirbridge.domain.errors import (
    ERROR_SPECS,
    DomainError,
    ErrorCategory,
    ErrorCode,
    FhirbridgeError,
    InvalidRequestError,
    PlatformError,
)
from fhirbridge.fhir.operation_outcome import (
    FHIR_JSON_MEDIA_TYPE,
    outcome_for_error,
    platform_error_envelope,
)
from fhirbridge.observability.context import (
    REQUEST_ID_HEADER,
    TRACE_ID_HEADER,
    get_trace_id,
)

logger = logging.getLogger(__name__)

DOCS_BASE = "https://github.com/fhirbridge/fhirbridge/blob/main/docs"

DOCUMENTATION: dict[ErrorCode, str] = {
    ErrorCode.LLM_CREDENTIALS_REQUIRED: f"{DOCS_BASE}/byok.md#supplying-credentials",
    ErrorCode.INSECURE_TRANSPORT: f"{DOCS_BASE}/byok.md#transport-security",
    ErrorCode.EGRESS_BLOCKED: f"{DOCS_BASE}/byok.md#egress-policy",
    ErrorCode.PHI_EGRESS_NOT_ACKNOWLEDGED: f"{DOCS_BASE}/byok.md#phi-egress",
    ErrorCode.MODEL_NOT_QUALIFIED: f"{DOCS_BASE}/model-compatibility.md",
    ErrorCode.CREDENTIAL_EXPIRED: f"{DOCS_BASE}/byok.md#ephemeral-keys-and-async-jobs",
    ErrorCode.TERMINOLOGY_UNAVAILABLE: f"{DOCS_BASE}/terminology-setup.md",
    ErrorCode.VALIDATOR_UNAVAILABLE: f"{DOCS_BASE}/deployment.md#validator-sidecar",
    ErrorCode.IG_NOT_LOADED: f"{DOCS_BASE}/deployment.md#implementation-guides",
    ErrorCode.NOT_IMPLEMENTED: f"{DOCS_BASE}/api.md#not-implemented-in-v1",
}

_CODE_BY_STATUS: dict[int, ErrorCode] = {
    400: ErrorCode.INVALID_REQUEST,
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.INVALID_REQUEST,
    409: ErrorCode.IDEMPOTENCY_CONFLICT,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
    415: ErrorCode.UNSUPPORTED_MEDIA_TYPE,
    422: ErrorCode.INVALID_REQUEST,
    429: ErrorCode.RATE_LIMITED,
    500: ErrorCode.INTERNAL_ERROR,
    501: ErrorCode.NOT_IMPLEMENTED,
}


def render_error(
    error: FhirbridgeError,
    *,
    extra_details: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
    request: Request | None = None,
) -> JSONResponse:
    """Render any :class:`FhirbridgeError` into its response envelope.

    ``request`` is how the correlation ids survive a 500. Starlette's
    ``ServerErrorMiddleware`` is the outermost layer, so by the time the
    unhandled-exception handler runs, ``CorrelationMiddleware`` has already
    unwound and the context variable is empty — leaving an operator with a
    500 and nothing to grep for. ``request.state`` still holds the ids.
    """
    trace_id = _correlation_id(request, "trace_id") or get_trace_id()
    documentation = DOCUMENTATION.get(error.code)

    if error.category is ErrorCategory.DOMAIN:
        body: dict[str, Any] = outcome_for_error(
            error, trace_id=trace_id, documentation_url=documentation
        )
        media_type = FHIR_JSON_MEDIA_TYPE
    else:
        body = platform_error_envelope(error, trace_id=trace_id)
        if documentation:
            body["error"]["documentation_url"] = documentation
        if extra_details:
            body["error"]["details"].update(extra_details)
        media_type = "application/json"

    headers: dict[str, str] = dict(extra_headers or {})
    if error.retry_after_s is not None:
        headers["Retry-After"] = str(error.retry_after_s)
    request_id = _correlation_id(request, "request_id")
    if trace_id:
        headers.setdefault(TRACE_ID_HEADER, trace_id)
    if request_id:
        headers.setdefault(REQUEST_ID_HEADER, request_id)

    return JSONResponse(
        status_code=error.http_status,
        content=body,
        media_type=media_type,
        headers=headers,
    )


def _correlation_id(request: Request | None, name: str) -> str | None:
    if request is None:
        return None
    value = getattr(request.state, name, None)
    return value if isinstance(value, str) else None


def error_for_status(status_code: int, detail: str | None = None) -> FhirbridgeError:
    """Map a bare HTTP status onto the error catalogue.

    The original status is carried through rather than re-derived from the code:
    several statuses share ``invalid-request``, and letting the code decide would
    silently rewrite a 405 or a 422 into a 400.
    """
    code = _CODE_BY_STATUS.get(status_code, ErrorCode.INTERNAL_ERROR)
    spec = ERROR_SPECS[code]
    cls = DomainError if spec.category is ErrorCategory.DOMAIN else PlatformError
    return cls(detail or spec.title, code=code, http_status=status_code)


def _sanitize_violations(raw: list[Any]) -> list[dict[str, str]]:
    """Keep only PHI-free fields from pydantic's error list."""
    violations: list[dict[str, str]] = []
    for item in raw[:50]:
        if not isinstance(item, dict):
            continue
        violations.append(
            {
                "location": ".".join(str(part) for part in item.get("loc", ())),
                "type": str(item.get("type", "value_error")),
                "message": str(item.get("msg", "invalid value")),
            }
        )
    return violations


def install_error_handlers(app: FastAPI) -> None:
    """Register every exception handler on ``app``."""

    @app.exception_handler(FhirbridgeError)
    async def _fhirbridge_error(request: Request, exc: Exception) -> JSONResponse:
        error = exc if isinstance(exc, FhirbridgeError) else InvalidRequestError()
        log = logger.warning if error.http_status < 500 else logger.error
        log(
            "request_failed",
            extra={
                "error_code": str(error.code),
                "http_status": error.http_status,
                "safe_context": error.safe_context,
            },
        )
        return render_error(error, request=request)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
        raw = list(exc.errors()) if isinstance(exc, RequestValidationError) else []
        logger.info("request_validation_failed", extra={"violation_count": len(raw)})
        return render_error(
            InvalidRequestError(
                "The request body or parameters did not match the expected schema."
            ),
            extra_details={"violations": _sanitize_violations(raw)},
            request=request,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: Exception) -> JSONResponse:
        status_code = 500
        # Starlette attaches ``Allow`` to its 405 and ``WWW-Authenticate`` to some
        # 401s. Those headers are part of the HTTP contract, so they survive the
        # re-render into our envelope.
        headers: dict[str, str] = {}
        if isinstance(exc, StarletteHTTPException):
            status_code = exc.status_code
            headers = dict(exc.headers or {})
        error = error_for_status(status_code)
        logger.info(
            "request_rejected",
            extra={"error_code": str(error.code), "http_status": status_code},
        )
        return render_error(error, extra_headers=headers, request=request)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # The client-facing message is deliberately generic: an exception string
        # can contain anything the failing code was holding, including document
        # text (principle 2.6). The trace_id is how an operator finds the log.
        logger.exception("unhandled_exception", extra={"exception_type": type(exc).__name__})
        return render_error(
            PlatformError(
                "An internal error occurred. Quote the trace_id when reporting it.",
                code=ErrorCode.INTERNAL_ERROR,
            ),
            request=request,
        )


__all__ = ["DOCUMENTATION", "error_for_status", "install_error_handlers", "render_error"]
