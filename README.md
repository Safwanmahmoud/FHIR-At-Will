# FHIR-It-Will

`fhirbridge` — a self-hostable, BYOK service that converts clinical source material into
validated, provenance-tagged FHIR R4 resources.

The distinguishing feature is not the model call. It is the harness around it:
verification, workflow and audit. Nothing is emitted that has not been scored against a
published implementation guide, and every dependency that cannot be reached causes a
refusal rather than a quietly unverified result.

## Status

M0 (skeleton) and M1 (validation) are implemented. `POST /v1/validate` scores a resource
or Bundle through an eight-layer cascade against `hl7.fhir.us.core#9.0.0`. No LLM call
exists yet, deliberately: you cannot build a trustworthy generator before you can measure
one.

`/v1/translate/*` and `/fhir/R4/$convert` answer `501` on purpose. HL7 v2 and CDA
conversion are deterministic problems that mature tools already solve.

## Installing

`uv` is the supported path, because it installs from `uv.lock` and therefore reproduces
the exact resolution that was tested:

```bash
uv sync                      # runtime + dev
uv sync --group notebook     # adds JupyterLab, for notebooks/
```

For environments without `uv`, the lockfile is also exported to hash-pinned requirements
files. These are **generated artifacts** — regenerate them with `uv export` rather than
editing them, and `tests/contract/test_requirements_export.py` fails if they drift from
`uv.lock`.

```bash
pip install -r requirements.txt                             # runtime only
pip install -r requirements.txt -r requirements-dev.txt      # plus test/lint tooling
pip install -r requirements.txt -r requirements-notebook.txt  # plus JupyterLab
```

`requirements-dev.txt` and `requirements-notebook.txt` hold only their own group, so they
are installed *alongside* `requirements.txt`, not instead of it. The optional `ocr` extra
(`pytesseract`) is not exported; install it directly if you need it.

## Running locally

```bash
cp .env.example .env          # set APP_DB_PASSWORD at minimum
docker compose --profile setup run --rm bootstrap   # migrations, app role, first API key
docker compose up -d
```

The bootstrap step prints an API key exactly once. Only its Argon2id hash is stored, so a
lost key is re-issued, never recovered.

Then confirm the stack is actually ready, rather than merely running:

```bash
curl localhost:8000/readyz
```

`/livez` says the process is alive; `/readyz` says every dependency it needs is
reachable, and returns `503` naming the failure if not.

## Trying the API

`notebooks/api_smoke_test.ipynb` exercises a running deployment end to end and asserts
that it behaves as specified — health, the authentication boundary, all eight cascade
layers, terminology, the FHIR facade, and both error envelopes.

## Layout

| Path | Contents |
|---|---|
| `src/fhirbridge/` | The service. `api/`, `validation/`, `terminology/`, `fhir/`, `storage/`, `observability/` |
| `src/fhirbridge/validation/rules/` | The L4 invariant and L5 plausibility rule packs, as YAML |
| `alembic/` | Migrations |
| `docker/` | Images for the API and the HAPI validator sidecar |
| `scripts/bootstrap.py` | Provisions a database, the least-privileged app role, a tenant and a key |
| `tests/` | `unit/`, `integration/`, `contract/`, `security/` |
| `notebooks/` | API smoke test |

## Licence

Apache-2.0.
