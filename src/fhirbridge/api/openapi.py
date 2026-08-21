"""OpenAPI document customization.

The document is a committed contract: ``tests/contract`` snapshots it and CI
fails on an unreviewed diff, and the Python and TypeScript SDKs are generated
from it. So it must be **stable** — the same code must produce byte-identical
output on every run. That is why operation ids are derived from the route rather
than from FastAPI's default function-name mangling, and why nothing here embeds
a timestamp, a hostname or a random value.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from fhirbridge.domain.errors import ERROR_CODE_SYSTEM
from fhirbridge.version import CODE_VERSION

BEARER_SCHEME: Final[str] = "bearerAuth"

DESCRIPTION: Final[str] = """
Convert unstructured clinical source material into validated, provenance-tagged
FHIR R4 resources, with a mandatory human-review stage before anything is
finalized.

**What this is not:** not a medical device, not clinical decision support, and not
a FHIR repository. Nothing is inferred beyond what the source text asserts, and no
endpoint writes to an external FHIR server without an explicit, separate call.

**Fail-closed behaviour.** When the FHIR validator or the terminology server is
unavailable, requests fail with `503` rather than returning an unverified result.
An unvalidated code and an unvalidated bundle are worse than no answer.

**Errors.** Clinical and FHIR-semantic failures return an `OperationOutcome`
whose `issue.details.coding` carries a stable machine code from
`{code_system}`. Platform failures return
`{{"error": {{"code", "message", "trace_id", "details"}}}}`. Fetch the full code
list from `GET /v1/error-codes`.

**No PHI in URLs.** Every endpoint that accepts clinical content accepts it in a
request body. There is no `GET` endpoint taking clinical text as a query
parameter, and none will be added.
""".strip().format(code_system=ERROR_CODE_SYSTEM)

TAGS: Final[list[dict[str, Any]]] = [
    {
        "name": "validation",
        "description": (
            "Score any FHIR resource against profiles, terminology, invariants and "
            "plausibility rules. Needs no LLM credentials and retains nothing."
        ),
    },
    {
        "name": "terminology",
        "description": "Confirm and translate codes against the configured terminology server.",
    },
    {
        "name": "translation",
        "description": (
            "Structured-format conversion. Deliberately not implemented in v1: "
            "deterministic converters are the right tool for HL7 v2 and C-CDA."
        ),
    },
    {"name": "fhir-facade", "description": "The same engine, exposed as FHIR operations."},
    {"name": "platform", "description": "Health, versions, capabilities."},
]

_METHOD_ORDER: Final[tuple[str, ...]] = (
    "get",
    "put",
    "post",
    "delete",
    "options",
    "head",
    "patch",
    "trace",
)


def _operation_id(method: str, path: str) -> str:
    """Derive a stable, readable operation id from the route.

    FastAPI's default ids include the function name and a path hash, which makes
    generated SDK method names churn whenever a handler is renamed.
    """
    cleaned = (
        path.replace("/fhir/R4", "fhir")
        .replace("/v1", "")
        .replace("$", "")
        .replace("{", "")
        .replace("}", "")
        .replace("-", "_")
        .replace(".", "_")
        .strip("/")
    )
    segments = [segment for segment in cleaned.split("/") if segment]
    return "_".join([method.lower(), *segments]) if segments else f"{method.lower()}_root"


def build_openapi(app: FastAPI) -> dict[str, Any]:
    """Build the customized OpenAPI document."""
    schema = get_openapi(
        title="fhirbridge",
        version=CODE_VERSION,
        openapi_version="3.1.0",
        summary="Narrative-to-FHIR conversion with a verification harness.",
        description=DESCRIPTION,
        routes=app.routes,
        tags=TAGS,
        license_info={"name": "Apache-2.0", "identifier": "Apache-2.0"},
        contact={"name": "fhirbridge", "url": "https://github.com/fhirbridge/fhirbridge"},
    )

    components = schema.setdefault("components", {})
    components.setdefault("securitySchemes", {})[BEARER_SCHEME] = {
        "type": "http",
        "scheme": "bearer",
        "description": (
            "A scoped API key (`fhirb_<prefix>_<secret>`) or an OAuth2 client-credentials "
            "access token. API keys are for sandbox use; production deployments should use "
            "OAuth2 or SMART Backend Services."
        ),
    }

    for path, operations in schema.get("paths", {}).items():
        for method, operation in operations.items():
            if method not in _METHOD_ORDER:
                continue
            operation["operationId"] = _operation_id(method, path)
            operation.setdefault("security", [{BEARER_SCHEME: []}])

    # Endpoints that must be reachable before a client holds any credential.
    for path in (
        "/livez",
        "/readyz",
        "/version",
        "/metrics",
        "/v1/error-codes",
        "/fhir/R4/metadata",
    ):
        operations = schema.get("paths", {}).get(path)
        if operations:
            for operation in operations.values():
                operation["security"] = []

    sorted_schema: dict[str, Any] = _sorted(schema)
    return sorted_schema


def _sorted(value: Any) -> Any:
    """Recursively sort mapping keys so the snapshot is diff-stable.

    ``paths`` keeps method ordering by HTTP verb rather than alphabetically,
    because a reader scanning a diff expects GET before POST.
    """
    if isinstance(value, dict):
        items = sorted(value.items())
        if all(key in _METHOD_ORDER for key in value):
            items = sorted(value.items(), key=lambda item: _METHOD_ORDER.index(item[0]))
        return {key: _sorted(item) for key, item in items}
    if isinstance(value, list):
        return [_sorted(item) for item in value]
    return value


def install_openapi(app: FastAPI) -> None:
    """Replace ``app.openapi`` with the customized, cached builder."""

    def openapi() -> dict[str, Any]:
        if not app.openapi_schema:
            app.openapi_schema = build_openapi(app)
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]  # documented FastAPI extension point


__all__ = ["BEARER_SCHEME", "DESCRIPTION", "TAGS", "build_openapi", "install_openapi"]
