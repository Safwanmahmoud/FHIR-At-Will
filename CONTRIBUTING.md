# Contributing

FHIR at Will welcomes focused fixes, tests, documentation, and design
discussion. By contributing, you agree that your contribution is licensed
under the Apache License 2.0.

## Before opening a change

1. Open an issue for substantial API, storage, safety, or architecture changes.
2. Never use real patient data or credentials in issues, tests, commits, or
   notebooks.
3. Keep changes small and use conventional commit subjects such as `feat:`,
   `fix:`, `docs:`, `test:`, `refactor:`, and `chore:`.
4. Preserve fail-closed behavior: an unavailable verifier must never become a
   successful result.

## Development setup

Python 3.12, `uv`, and Docker with Compose v2 are supported.

```bash
uv sync --group dev
uv run pytest -q -m "not integration"
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pip-audit
```

Integration tests require Docker and, for sidecar coverage, configured
validator and terminology endpoints:

```bash
uv run pytest -q -m integration
```

## Public contracts

OpenAPI and exported requirements are committed contracts. When a deliberate
change makes the OpenAPI contract test fail, run
`uv run python scripts/export_openapi.py`, inspect the complete diff, and
include it in the same change. Regenerate requirements with the command in each
exported file's header. Do not update a snapshot only to make CI green.

Notebooks must be committed with every `execution_count` set to `null` and all
`outputs` arrays empty. Use synthetic clinical examples only.

## Pull requests

Explain the behavior change, safety/privacy impact, and validation performed.
Reference related issues with `#<issue-number>`. Changes to authentication,
tenant isolation, clinical routing, terminology, LLM egress, or logging should
include a regression test.

Security vulnerabilities must follow [SECURITY.md](SECURITY.md), not the public
issue tracker.
