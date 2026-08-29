"""The OpenAPI document is a committed contract (AGENTS.md).

The snapshot diff must be reviewed whenever the API changes; an accidental
rename is a breaking change for every client.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI

from fhirbridge.api.openapi import BEARER_SCHEME, build_openapi

pytestmark = pytest.mark.contract

SNAPSHOT = Path(__file__).parent / "openapi.snapshot.json"


def _document(app: FastAPI) -> dict[str, object]:
    return build_openapi(app)


def test_snapshot_matches(app: FastAPI) -> None:
    current = json.dumps(_document(app), indent=2, sort_keys=True) + "\n"

    if not SNAPSHOT.exists():  # pragma: no cover - first run only
        SNAPSHOT.write_text(current, encoding="utf-8")
        pytest.fail(f"wrote a new snapshot to {SNAPSHOT.name}; review and commit it")

    assert current == SNAPSHOT.read_text(encoding="utf-8"), (
        "the OpenAPI contract changed. Review the diff, then run "
        "`uv run python scripts/export_openapi.py` and commit the snapshot."
    )


def test_document_is_deterministic(app: FastAPI) -> None:
    """Principle 2.8 applies to our own artifacts too."""
    first = json.dumps(_document(app), sort_keys=True)
    second = json.dumps(_document(app), sort_keys=True)

    assert first == second


def test_operation_ids_are_unique_and_derived_from_the_route(app: FastAPI) -> None:
    document = _document(app)
    paths: dict[str, dict[str, dict[str, object]]] = document["paths"]  # type: ignore[assignment]

    ids = [
        operation["operationId"]
        for operations in paths.values()
        for operation in operations.values()
    ]

    assert len(ids) == len(set(ids))
    assert "post_validate" in ids
    assert "post_NAR2FHIR" in ids
    assert "post_terminology_validate_code" in ids
    # No FastAPI-style name mangling, which churns when a handler is renamed.
    assert not any("__" in str(identifier) for identifier in ids)


def test_security_is_declared_on_every_non_public_operation(app: FastAPI) -> None:
    document = _document(app)
    paths: dict[str, dict[str, dict[str, object]]] = document["paths"]  # type: ignore[assignment]
    public = {"/livez", "/readyz", "/version", "/metrics", "/v1/error-codes", "/fhir/R4/metadata"}

    for path, operations in paths.items():
        for method, operation in operations.items():
            expected: list[object] = [] if path in public else [{BEARER_SCHEME: []}]
            assert operation.get("security") == expected, f"{method.upper()} {path}"


def test_error_shapes_are_documented(app: FastAPI) -> None:
    """A client cannot handle `503 terminology-unavailable` it was never told about."""
    document = _document(app)
    paths: dict[str, dict[str, dict[str, object]]] = document["paths"]  # type: ignore[assignment]

    validate = paths["/v1/validate"]["post"]
    responses: dict[str, object] = validate["responses"]  # type: ignore[assignment]
    assert {"200", "400", "422", "503"} <= set(responses)


def test_no_endpoint_accepts_clinical_text_in_a_query_string(app: FastAPI) -> None:
    document = _document(app)
    paths: dict[str, dict[str, dict[str, object]]] = document["paths"]  # type: ignore[assignment]

    for path, operations in paths.items():
        for method, operation in operations.items():
            for parameter in operation.get("parameters", []) or []:  # type: ignore[union-attr]
                assert not (
                    parameter.get("in") == "query"
                    and parameter.get("name") in {"resource", "text", "code", "bundle"}
                ), f"{method.upper()} {path}"
