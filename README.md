<p align="center">
  <img src="assets/fhir-at-will-logo.png" alt="FHIR at Will logo" width="720">
</p>

<p align="center">
  <strong>Generate FHIR. Verify every result.</strong><br>
  Open source · self-hostable · FHIR R4 · BYOK/BYOM
</p>

# FHIR at Will

FHIR at Will is a verification-first API for FHIR R4. It validates existing FHIR
resources and can turn clinical narrative into a FHIR Bundle using a caller-supplied
model and provider key.

The generated Bundle is never presented as trusted output. It is returned beside a
structured report covering conformance, terminology, clinical plausibility, skipped
checks, version provenance, and the final routing decision.

> [!WARNING]
> This project is alpha software and is not a medical device. Use synthetic data in
> public demos. Self-hosting alone does not make a deployment HIPAA or GDPR compliant.

## Try it

- [Interactive playground](https://fhiratwill.com/playground.html)
- [Website and API guide](https://fhiratwill.com/docs.html)
- [Source code](https://github.com/Safwanmahmoud/FHIR-It-Will)

The hosted playground supports:

1. **Validate a resource** — submit a FHIR R4 resource or Bundle to the live
   eight-layer cascade.
2. **Narrative → FHIR** — bring an OpenRouter key, choose a tool-calling model, and let
   the agentic `POST /v1/craft` endpoint build a Bundle through validated tool edits,
   then run the assembled Bundle through the cascade.

The playground is a thin [landing-page proxy](https://github.com/Safwanmahmoud/FHIR-at-Will-Landing):
it forwards the visitor's BYOK headers to `/v1/craft` and renders the response,
including the agent's tool-call trace. Correction now happens inside the API — the
agent re-validates every edit and self-corrects — so there is no separate landing-side
repair loop, and every model call passes through this API's provider allowlist,
qualification, and budget gates.

## What works today

| Capability | Status |
|---|---|
| Validate a FHIR R4 resource or Bundle | Implemented |
| Profile and invariant validation with the HL7 validator | Implemented |
| Terminology validation and ConceptMap translation | Implemented |
| Clinical plausibility rules | Implemented |
| BYOK narrative-to-FHIR conversion (`/v1/convert`, single-pass) | Implemented |
| BYOK agentic narrative-to-FHIR (`/v1/craft`, tool-driven) | Implemented |
| PHI-free LLM credential/connectivity probe | Implemented |
| FHIR `OperationOutcome` and `$validate` facade | Implemented |
| API-key authentication and tenant-aware PostgreSQL RLS | Implemented |
| JSON logs, Prometheus metrics, and OpenTelemetry hooks | Implemented |
| Persisted conversion jobs and source documents | Planned for M3 |
| Source-span fidelity and coverage scoring | Planned for M3 |
| Core API repair endpoint | Planned for M3 |
| Human review and delivery workflows | Planned for M4–M6 |

The current build targets:

- FHIR `4.0.1`;
- US Core `hl7.fhir.us.core#9.0.0`;
- HL7 validator CLI `6.10.2`; and
- a configurable terminology server (`https://tx.fhir.org/r4` by default).

`GET /v1/capabilities` reports implemented and unavailable functionality at runtime.

## How validation works

Every report contains all eight layers. A check that could not run is marked
`skipped` or `not_applicable`; it is never allowed to look like a pass.

| Layer | Check | Current state |
|---:|---|---|
| L1 | Structural FHIR R4 parsing and resource allowlist | Implemented |
| L2 | Declared and requested profile conformance | Implemented |
| L3 | Code validity and ValueSet bindings | Implemented |
| L4 | FHIRPath invariants | Implemented |
| L5 | Physiological, temporal, and dose plausibility | Implemented |
| L6 | Source-to-output fidelity | Not applicable until M3 |
| L7 | Omitted clinical mention coverage | Not applicable until M3 |
| L8 | Auto-accept, review, or reject routing | Implemented from available signals |

The response separates two related decisions:

- `conformant` is `true` when no blocking issue was found by a layer that ran.
- `status` is `auto`, `needs_review`, or `reject`.

A conformant resource can still need review because of warnings. A non-conformant
resource is returned as a successful HTTP `200` validation report; HTTP errors are
reserved for invalid requests, policy failures, and unavailable dependencies.

FHIR conformance also does not prove clinical correctness. For example, a heart rate
of `44000 /min` may be structurally valid but is rejected by L5 as physiologically
impossible.

L1 uses the `fhir.resources` R4B typed models for parsing and round-tripping. The
pinned HL7 validator remains the conformance authority for FHIR R4 `4.0.1` in L2/L4.

## Architecture

```mermaid
flowchart LR
    Client[API client] -->|Bearer API key| API[FastAPI]
    Playground[Hosted playground] -->|server-held FHIR key| API

    API --> Cascade[Validation cascade]
    Cascade --> Models[L1 typed FHIR models]
    Cascade --> Validator[L2 + L4<br>HL7 validator sidecar]
    Cascade --> Terminology[L3<br>FHIR terminology server]
    Cascade --> Rules[L5<br>versioned rule pack]
    Cascade --> Routing[L8 routing]

    API -->|X-LLM-* BYOK headers| Gateway[LLM policy gateway]
    Gateway -->|caller's key| Provider[LLM provider]
    Gateway --> Cascade

    API --> Agent[Craft agent loop]
    Agent -->|tool calls| Gateway
    Agent --> Tools[Deterministic tools<br>structural + terminology gated]
    Tools --> Terminology
    Agent -->|assembled Bundle| Cascade

    API --> DB[(PostgreSQL<br>tenant RLS)]
    API --> Observability[Logs / metrics / traces]
```

The validator has no authentication and can fetch external resources, so it must stay
on a private network. PostgreSQL migrations run as the database owner, while the API
runs as a separate least-privileged role. `/readyz` refuses readiness if row-level
security does not apply to that role.

## Quick start with Docker

### Prerequisites

- Docker Engine or Docker Desktop with Compose v2;
- at least 4 GB of memory for the validator build/runtime; and
- network access while the validator image caches its pinned IG package.

### 1. Configure

```bash
cp .env.example .env
```

Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Change both development passwords in `.env`:

```dotenv
POSTGRES_PASSWORD=choose-an-owner-password
APP_DB_PASSWORD=choose-a-different-app-password
```

The API must use `APP_DB_PASSWORD`, never the PostgreSQL owner password.

### 2. Provision the database and first API key

```bash
docker compose --profile setup run --rm bootstrap
```

This applies migrations, creates the least-privileged application role, provisions a
tenant, and prints an API key. The key is shown once; only its Argon2id hash is stored.
The bootstrap key receives `documents:write`, `conversions:write`, `facts:read`, and
`reviews:write`; it deliberately excludes PHI-read, submission, credential-management,
and administrator scopes.

If that plaintext key is lost, issue a replacement for the existing tenant instead of
running bootstrap again:

```bash
DATABASE_URL=postgresql+asyncpg://owner:...@host/db \
  uv run python scripts/issue_api_key.py --tenant-slug local-development
```

The replacement is unscoped by default, which is sufficient for validation and the API
smoke test. Repeat `--scope <scope>` to grant additional permissions. The command must
use the schema-owner connection, prints the new key once, and does not revoke old keys.

### 3. Start the stack

```bash
docker compose up -d
docker compose ps
```

The API is available at `http://localhost:8000`. PostgreSQL, Redis, and the validator
remain private to the Compose network. Redis is provisioned for future asynchronous M3
jobs but is not used by the current synchronous M0–M2 request path.

### 4. Check the deployment

```bash
curl http://localhost:8000/livez
curl http://localhost:8000/readyz
curl http://localhost:8000/version
```

- `/livez` checks the API process.
- `/readyz` checks PostgreSQL isolation, the validator, and terminology.
- `/version` returns the code, FHIR, IG, and validator versions stamped into reports.

OpenAPI is available at `http://localhost:8000/docs`.

## Validate a resource

All compute endpoints require a Bearer API key:

```http
Authorization: Bearer fhirb_...
```

The example below is a conformant heart-rate Observation. It uses the UCUM display
`/min`; `"beats/minute"` is not the canonical display for that code and may be rejected
by terminology validation.

```python
import httpx

base_url = "http://localhost:8000"
api_key = "fhirb_..."  # value printed by bootstrap

observation = {
    "resourceType": "Observation",
    "text": {
        "status": "generated",
        "div": '<div xmlns="http://www.w3.org/1999/xhtml">Heart rate 72/min</div>',
    },
    "status": "final",
    "category": [{
        "coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/observation-category",
            "code": "vital-signs",
            "display": "Vital Signs",
        }]
    }],
    "code": {
        "coding": [{
            "system": "http://loinc.org",
            "code": "8867-4",
            "display": "Heart rate",
        }]
    },
    "subject": {"reference": "Patient/example"},
    "performer": [{"reference": "Practitioner/example"}],
    "effectiveDateTime": "2024-01-15T09:30:00Z",
    "valueQuantity": {
        "value": 72,
        "unit": "/min",
        "system": "http://unitsofmeasure.org",
        "code": "/min",
    },
}

response = httpx.post(
    f"{base_url}/v1/validate",
    headers={"Authorization": f"Bearer {api_key}"},
    json={"resource": observation},
    timeout=180,
)
response.raise_for_status()

report = response.json()
print(report["status"])       # auto
print(report["conformant"])   # True
for layer in report["layers"]:
    print(layer["layer_number"], layer["layer"], layer["status"])
```

Request options:

```json
{
  "resource": {"resourceType": "Patient"},
  "profiles": ["http://example.org/StructureDefinition/my-profile"],
  "layers": ["structural", "profile", "terminology"],
  "severity_overrides": {"fb-plaus-heart-rate": "warning"},
  "max_terminology_checks": 500
}
```

A bare FHIR resource is also accepted with
`Content-Type: application/fhir+json`.

## Convert narrative to FHIR

`POST /v1/convert` is synchronous, stateless, and BYOK. It makes one model call,
validates the generated Bundle, and returns:

- `bundle` — the generated FHIR R4 Bundle;
- `report` — the full validation result;
- `llm` — model, token, cost, latency, and qualification metadata; and
- `conversion_id` — an opaque correlation identifier, not a persisted job.

The service does not hold an LLM key. Supply invocation details on every request:

| Header | Purpose |
|---|---|
| `X-LLM-Provider` | Provider id; defaults to `openrouter` |
| `X-LLM-Model` | Provider model id; required |
| `X-LLM-API-Key` | Caller-owned provider key; required |
| `X-LLM-Base-Url` | Optional endpoint override |
| `X-LLM-Extra-Headers` | Optional JSON object of provider headers |
| `X-PHI-Egress-Acknowledged` | Must be `true` for external clinical-data egress |

Before using an external provider in local development, explicitly enable that egress.
For Compose, add a `docker-compose.override.yml`:

```yaml
services:
  api:
    environment:
      # Local HTTP only. Never enable insecure transport with real PHI or keys.
      ALLOW_INSECURE_TRANSPORT: "true"
      LLM_EGRESS_ALLOWLIST: openrouter.ai
      # Unknown models resolve to "unqualified".
      MIN_QUALIFICATION_TIER: unqualified
```

Then recreate the API:

```bash
docker compose up -d --force-recreate api
```

Example:

```python
response = httpx.post(
    f"{base_url}/v1/convert",
    headers={
        "Authorization": f"Bearer {api_key}",
        "X-LLM-Provider": "openrouter",
        "X-LLM-Model": "openai/gpt-4.1-nano",
        "X-LLM-API-Key": "sk-or-...",  # your key; never commit it
        "X-PHI-Egress-Acknowledged": "true",
    },
    json={
        "text": (
            "62-year-old male seen for follow-up. "
            "Blood pressure 128/82 mmHg. Takes metformin 500 mg twice daily."
        )
    },
    timeout=300,
)
response.raise_for_status()

result = response.json()
print(result["report"]["status"])       # inspect before using result["bundle"]
print(result["report"]["conformant"])
print(result["llm"]["model"], result["llm"]["cost_usd"])
```

The model must support structured JSON output. Prose, truncated JSON, or output outside
the required schema returns `422 llm-schema-violation`. Model availability and
capabilities vary by provider.

Use `POST /v1/llm/probe` with the same headers to verify credentials, policy, and
connectivity using a PHI-free prompt before sending clinical content.

## Craft narrative to FHIR (agentic)

`POST /v1/craft` is the recommended narrative path. Instead of asking the model for a
whole Bundle in one shot, it gives the model a set of deterministic tools and lets it
build the record step by step. Each tool validates its own edit before committing:

- **Structural gate** — every candidate resource must parse through the L1 typed model.
- **Terminology gate** — every clinical `Coding` the model introduces (LOINC, SNOMED CT,
  RxNorm, UCUM) must be confirmed by `$validate-code`. An unverifiable code fails closed.

A rejected edit is returned to the model with the reason so it can retry; the draft can
never enter a non-conformant state. When the agent finishes, the assembled Bundle is run
through the same L1–L5 cascade as every other path. The model chooses *what* to assert;
the tools guarantee *validity*.

It uses the same BYOK `X-LLM-*` headers and `conversions:write` scope as `/v1/convert`,
but **the model must support tool calling** (on OpenRouter, `supported_parameters`
includes `tools`). The response adds:

- `bundle` — the assembled FHIR R4 Bundle;
- `report` — the full validation result;
- `trace` — the ordered tool calls, each marked accepted or rejected;
- `iterations` and `stop_reason` — how the loop ended (`finished`, `max_iterations`,
  `budget_exhausted`, or `no_tool_calls`);
- `toolset_version` and `llm` — provenance across every model call the agent made.

The loop is bounded by `MAX_AGENT_ITERATIONS` (default `24`) and the same
`MAX_COST_USD_PER_CONVERSION` budget as single-pass conversion.

```python
response = httpx.post(
    f"{base_url}/v1/craft",
    headers={
        "Authorization": f"Bearer {api_key}",
        "X-LLM-Provider": "openrouter",
        "X-LLM-Model": "meta-llama/llama-3.3-70b-instruct",  # must support tools
        "X-LLM-API-Key": "sk-or-...",  # your key; never commit it
        "X-PHI-Egress-Acknowledged": "true",
    },
    json={
        "text": (
            "62-year-old male seen for follow-up. "
            "Blood pressure 128/82 mmHg. Takes metformin 500 mg twice daily."
        )
    },
    timeout=300,
)
response.raise_for_status()

result = response.json()
print(result["report"]["status"], result["report"]["conformant"])
print(result["iterations"], result["stop_reason"])
for step in result["trace"]:
    print(step.get("tool"), "ok" if step.get("ok") else step.get("error"))
```

`/v1/convert` remains available as the single-shot path for models without tool calling.

## API surface

### Public

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/livez` | Process liveness |
| `GET` | `/readyz` | Dependency and RLS readiness |
| `GET` | `/version` | Reproducibility pins |
| `GET` | `/metrics` | Prometheus metrics |
| `GET` | `/v1/error-codes` | Error catalogue as a FHIR `CodeSystem` |
| `GET` | `/fhir/R4/metadata` | FHIR `CapabilityStatement` |
| `GET` | `/docs` | Interactive OpenAPI documentation |
| `GET` | `/openapi.json` | OpenAPI contract |

### Authenticated

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/health/dependencies` | Detailed dependency health |
| `GET` | `/v1/capabilities` | Implemented and planned capabilities |
| `GET` | `/v1/igs` | Preloaded implementation guides |
| `POST` | `/v1/validate` | Structured validation report |
| `POST` | `/v1/validate/outcome` | Validation as `OperationOutcome` |
| `POST` | `/v1/convert` | Single-pass BYOK narrative conversion and validation |
| `POST` | `/v1/craft` | Agentic BYOK narrative conversion via validated tools |
| `POST` | `/v1/llm/probe` | PHI-free provider probe |
| `POST` | `/v1/terminology/validate-code` | Code and ValueSet validation |
| `POST` | `/v1/terminology/map` | ConceptMap `$translate` |
| `POST` | `/fhir/R4/$validate` | FHIR-native validation operation |

`/v1/convert`, `/v1/craft`, and `/v1/llm/probe` require the `conversions:write` scope.

HL7 v2, C-CDA, and tabular conversion stubs return `501`, as do the FHIR facade
operations `/fhir/R4/$convert` and `/fhir/R4/$extract`. Deterministic source formats
should be mapped with purpose-built tooling before the result is submitted for FHIR
validation.

## Fail-closed behavior

The API does not silently downgrade verification:

- unavailable validator or terminology dependencies return `503`;
- an unknown profile returns `422 ig-not-loaded`;
- blocked LLM egress returns `451`;
- unqualified, over-budget, or malformed model output returns `422`;
- provider rate limits return `429` and may include `Retry-After`; and
- every report names skipped or not-applicable layers.

Platform errors use an `error.code`, `error.message`, and `error.trace_id` JSON
envelope. Clinical and dependency errors may use a FHIR `OperationOutcome`. Every
response includes `X-Request-Id` and `X-Trace-Id`.

## Important configuration

See [`.env.example`](.env.example) for the complete development configuration.

| Variable | Purpose | Default |
|---|---|---|
| `DATABASE_URL` | Least-privileged PostgreSQL connection | Required |
| `REDIS_URL` | Required configuration reserved for future M3 jobs | Required |
| `VALIDATOR_URL` | Private validator sidecar | `http://localhost:8081` |
| `TERMINOLOGY_URL` | FHIR terminology server | `https://tx.fhir.org/r4` |
| `DEFAULT_IG_PACKAGES` | IGs named in reports | `hl7.fhir.us.core#9.0.0` |
| `VALIDATOR_VERSION` | Validator version named in reports | `6.10.2` |
| `FHIRBRIDGE_ENV` | `development`, `staging`, or `production` | `development` |
| `FHIRBRIDGE_EPHEMERAL_KEY` | Ephemeral encryption key required in production | Unset |
| `REQUIRE_RLS_ENFORCEMENT` | Fail readiness if RLS is bypassed | `true` |
| `LLM_MODE` | Credential mode; this build supports BYOK | `byok` |
| `ALLOW_INSECURE_TRANSPORT` | Permit credentials over HTTP | `false` |
| `LLM_EGRESS_ALLOWLIST` | Permitted external LLM hosts | Empty; blocks all |
| `LLM_ALLOWED_PROVIDERS` | Permitted provider ids | `*` |
| `LOCAL_ONLY_MODE` | Restrict LLM calls to loopback hosts | `false` |
| `REQUIRE_PHI_EGRESS_ACK` | Require explicit external PHI acknowledgement | `true` |
| `MIN_QUALIFICATION_TIER` | Minimum model tier | `bronze` |
| `MAX_COST_USD_PER_CONVERSION` | Worst-case model cost cap | `1.00` USD |
| `MAX_AGENT_ITERATIONS` | Tool-calling turn cap for `/v1/craft` | `24` |

The public `tx.fhir.org` service has no SLA. It receives codes rather than complete
resources, but those codes may still be sensitive in context. Production deployments
should use an appropriately licensed terminology service with suitable availability
and privacy guarantees.

Production mode additionally requires `FHIRBRIDGE_EPHEMERAL_KEY`, rejects the public
`tx.fhir.org` default, forbids insecure transport and LLM I/O capture, and requires
HTTPS for a non-loopback validator URL.

## Security and privacy

- API keys are stored as Argon2id hashes.
- Provider keys are wrapped as secrets and are not stored, logged, or returned.
- Validation resources and conversion narratives are processed and dropped.
- Validation and conversion responses use `Cache-Control: no-store`.
- Tenant tables use PostgreSQL row-level security.
- Readiness checks that the API role is actually subject to RLS.
- Terminology requests use POST bodies rather than query strings.
- Logs record decisions and counts, not resource bodies or validator messages that may
  quote clinical values.
- The validator is designed for a private network only.

Operators remain responsible for TLS, access control, backups, retention, terminology
licensing, provider agreements, incident response, and all infrastructure compliance.

## Development

Python `3.12` and [`uv`](https://docs.astral.sh/uv/) are the supported path:

```bash
uv sync
uv run pytest -q -m "not integration"
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

Integration tests start PostgreSQL through testcontainers. Validator and terminology
sidecar tests run only when their URLs are supplied:

```bash
uv run pytest -m integration
```

The executable notebook smoke test is at
[`notebooks/api_smoke_test.ipynb`](notebooks/api_smoke_test.ipynb):

```bash
uv sync --group notebook
uv run jupyter lab notebooks/api_smoke_test.ipynb
```

Set `FHIRBRIDGE_BASE`, `FHIRBRIDGE_API_KEY`, and `OPENROUTER_API_KEY` before running it.
The notebook checks only the two primary workflows: validation and BYOK agentic
narrative-to-FHIR crafting (`/v1/craft`). Use synthetic resources because notebook
outputs are persisted in the file.

## Repository layout

| Path | Contents |
|---|---|
| `src/fhirbridge/api/` | FastAPI app, auth, schemas, middleware, and routers |
| `src/fhirbridge/validation/` | Cascade orchestration, reports, and rule packs |
| `src/fhirbridge/agent/` | Craft agent: draft state, deterministic tools, and loop |
| `src/fhirbridge/llm/` | BYOK gateway, policy gates, qualification, and prompts |
| `src/fhirbridge/fhir/` | Typed models, `OperationOutcome`, and validator client |
| `src/fhirbridge/terminology/` | Terminology client and result models |
| `src/fhirbridge/storage/` | SQLAlchemy models, tenant sessions, and RLS checks |
| `src/fhirbridge/observability/` | Logging, redaction, metrics, and tracing |
| `docker/` | API and validator images |
| `alembic/` | Database migrations |
| `scripts/bootstrap.py` | App-role, tenant, and API-key provisioning |
| `tests/` | Unit, contract, security, and integration suites |

## Roadmap

| Milestone | Goal | Status |
|---|---|---|
| M0 | Platform, auth, storage, health, containers | Implemented |
| M1 | Validation cascade, terminology, plausibility | Implemented |
| M2 | BYOK gateway, synchronous + agentic conversion, probe | Implemented |
| M3 | Documents, facts, staged generation, fidelity, coverage, repair | Planned |
| M4 | Human review workflow | Planned |
| M5 | Goldset-based model qualification and calibrated routing | Planned |
| M6 | Delivery integrations and operational hardening | Planned |

## Contributing

Keep changes small and verification-first:

1. add or update tests;
2. run formatting, lint, types, and relevant test suites;
3. update OpenAPI snapshots when the public contract changes;
4. never log resource bodies, issue messages, prompts, or credentials; and
5. never turn an unavailable verifier into a successful response.

## License

[Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0)
