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

The public project and product are named **FHIR at Will**. The installable Python
package, import namespace, API title, and container service retain the shorter
technical name **`fhirbridge`**.

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

- **Narrative → FHIR** — bring an OpenRouter key and use `POST /v1/NAR2FHIR` to
   generate a FHIR Bundle.

## What works today

| Capability | Status |
|---|---|
| Validate a FHIR R4 resource or Bundle | Implemented |
| Profile and invariant validation with the HL7 validator | Implemented |
| Terminology validation within the validation cascade | Implemented |
| Clinical plausibility rules | Implemented |
| Grounded BYOK narrative extraction with deterministic FHIR assembly (`/v1/NAR2FHIR`) | Implemented |
| Dictated-audio conversion via speech-to-text (`/v1/VOICE2FHIR`) | Implemented |
| FHIR `OperationOutcome` validation response | Implemented |
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

## Deploy on Railway

The repository includes a four-service Railway definition for the API, private
validator sidecar, PostgreSQL, and Redis. See the
[Railway template guide](docs/railway-template.md) for the generated-secret
configuration, first API-key retrieval, sandbox safety boundary, and
marketplace overview.

The one-click marketplace button will be added here after the public template
is published.

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
    "category": [
        {
            "coding": [
                {
                    "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                    "code": "vital-signs",
                    "display": "Vital Signs",
                }
            ]
        }
    ],
    "code": {
        "coding": [
            {
                "system": "http://loinc.org",
                "code": "8867-4",
                "display": "Heart rate",
            }
        ]
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
print(report["status"])  # auto
print(report["conformant"])  # True
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

## NAR2FHIR: convert narrative to FHIR

`POST /v1/NAR2FHIR` is synchronous, stateless, and BYOK. It makes **one** model
call, which extracts catalog-constrained resource types, keys, and values, each
tagged with an `instance` key identifying which real-world thing it describes.
Assembly into typed FHIR is then deterministic Python: no model sees the Bundle, so
the same entities always produce the same Bundle.

That boundary is deliberate. Choosing a FHIR datatype has one correct answer and
does not need a model, while a model asked to do it may invent a
`Coding.system`/`code` pair or nest a string where an object belongs. Assembly
therefore refuses rather than approximates — `"62-year-old"` does not become a
`birthDate`, and `"128/82 mmHg"` does not become a `Quantity` of 128 — and coded
concepts carry `text` only, leaving code assertions to `/v1/validate`.

It does not validate the generated Bundle. The response returns:

- `bundle` — the generated FHIR R4 Bundle;
- `validated` — always `false`;
- `assembly` — every element dropped, inferred, wired, or in conflict, with a
  reason. PHI-free: it names entry indexes and element names, never values;
- `llm` — model, token, cost, latency, and qualification metadata; and
- `conversion_id` — an opaque correlation identifier, not a persisted job.

Read `assembly` before the Bundle. FHIR requires elements a narrative rarely
states — `Observation.status`, `Encounter.class`, `MedicationRequest.intent` — and
assembly fills those from a reviewed constant table, marking the resource
`machine-inferred` and listing each one as an `inferred` note. Such a value is
reproducible and auditable but is not evidence about the patient.

There is no `profiles` field on the request. Assembly validates nothing, so it
cannot honor a profile; pass profiles to `POST /v1/validate` instead.

### Extraction rules

Where the narrative's shape and FHIR's shape disagree, a reviewed rule pack in
[`src/fhirbridge/llm/extraction_rules.py`](src/fhirbridge/llm/extraction_rules.py)
tells the model what to do. Rules are rendered into the extraction prompt and pinned
by the prompt fingerprint, so adding one means appending to `EXTRACTION_RULES` and
bumping `PROMPT_SET_VERSION`.

| Rule | Effect |
|---|---|
| An age is not a birth date | `62-year-old` becomes an `Age` Observation of `62 years`; `Patient.birthDate` is never computed from an age |
| One measurement per value | `128/82 mmHg` becomes separate systolic and diastolic Observations |
| Resolve dates only against a stated anchor | Relative dates resolve only when the narrative states the anchor, at the precision the phrase supports |
| Never turn a denial or a relative's history into a diagnosis | A denied condition becomes `verificationStatus: refuted`; a family member's condition is dropped |
| Split a medication phrase | `metformin` and `500 mg by mouth twice daily` land in separate elements |
| One instance per real-world thing | Each distinct measurement, condition, encounter, and medication gets its own `instance` |

A rule may not license a guess. Where a fact cannot be represented, the rule says to
leave the source wording so coercion refuses it and `assembly` names the gap — a
reported gap being a better outcome than a plausible fabrication.

Submit the returned `bundle` separately to `POST /v1/validate` before trusting it.

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
    f"{base_url}/v1/NAR2FHIR",
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
assert result["validated"] is False
print(result["llm"]["model"], result["llm"]["cost_usd"])

# Read what could not be grounded before reading the Bundle.
for note in result["assembly"]:
    print(note["action"], note["resource_type"], note["element"], note["detail"])

validation = httpx.post(
    f"{base_url}/v1/validate",
    headers={"Authorization": f"Bearer {api_key}"},
    json={"resource": result["bundle"]},
    timeout=300,
)
validation.raise_for_status()
print(validation.json()["status"])
```

The model must support structured JSON output. Prose, truncated JSON, or output outside
the required schema returns `422 llm-schema-violation`. Model availability and
capabilities vary by provider.

## VOICE2FHIR: convert dictated audio to FHIR

`POST /v1/VOICE2FHIR` is `NAR2FHIR` with a transcription step in front. It transcribes
dictated clinical audio verbatim, then runs the transcript through the exact same
grounded extraction and deterministic assembly, so nothing about the conversion changes
because the narrative arrived as speech. It returns everything `NAR2FHIR` does, plus:

- `transcript` — the verbatim text the model heard, and the input to extraction; and
- `transcription` — the dictation call's provider, model, token, cost, and latency.

The transcript is returned on purpose. Dictation can mishear a clinically decisive word
(`no chest pain` becoming `chest pain`), and a reviewer cannot catch that from the Bundle
alone. Read it against the audio before trusting the result.

Dictation is a **separate** BYOK call. litellm cannot transcribe through OpenRouter, so
audio goes to a speech-to-text provider (Gemini by default, also OpenAI, Groq, WatsonX,
...) on its own key, supplied in `X-STT-*` headers alongside the `X-LLM-*` extraction
headers. Both calls pass the same provider, egress-allowlist, and PHI-acknowledgement
gates; the qualification tier is not applied to dictation, because that gate ranks models
that reason over clinical meaning, not ones that transcribe. Add the dictation provider's
host to `LLM_EGRESS_ALLOWLIST` (Gemini is `generativelanguage.googleapis.com`).

| Header | Purpose |
|---|---|
| `X-STT-Provider` | Speech-to-text provider id; defaults to `gemini` |
| `X-STT-Model` | Provider transcription model id; required |
| `X-STT-API-Key` | Caller-owned provider key; required |
| `X-STT-Base-Url` | Optional endpoint override |
| `X-STT-Extra-Headers` | Optional JSON object of provider headers |
| `X-STT-Language` | Optional spoken-language hint |

The audio is uploaded as `multipart/form-data` (never a query parameter) and, like the
transcript, never reaches a log. Example:

```python
with open("dictation.wav", "rb") as audio:
    response = httpx.post(
        f"{base_url}/v1/VOICE2FHIR",
        headers={
            "Authorization": f"Bearer {api_key}",
            "X-LLM-Provider": "openrouter",
            "X-LLM-Model": "openai/gpt-4.1-nano",
            "X-LLM-API-Key": "sk-or-...",  # your extraction key
            "X-STT-Provider": "gemini",
            "X-STT-Model": "gemini-2.5-flash",
            "X-STT-API-Key": "...",  # your dictation key; never commit it
            "X-PHI-Egress-Acknowledged": "true",
        },
        files={"audio": ("dictation.wav", audio, "audio/wav")},
        timeout=300,
    )
response.raise_for_status()
result = response.json()

# Verify the dictation before trusting anything built from it.
print(result["transcript"])
```

Supported audio formats are wav, mp3, m4a/mp4, aac, flac, ogg/opus, aiff, and webm; other
uploads return `415`. Audio above `MAX_UPLOAD_BYTES` returns `413`, and audio with no
discernible speech returns `422`. `/v1/VOICE2FHIR` requires the `conversions:write` scope.

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
| `POST` | `/v1/NAR2FHIR` | Grounded BYOK extraction, deterministic FHIR assembly (unvalidated) |
| `POST` | `/v1/VOICE2FHIR` | Transcribe dictated audio, then convert as `/v1/NAR2FHIR` (unvalidated) |

`/v1/NAR2FHIR` and `/v1/VOICE2FHIR` require the `conversions:write` scope.

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
uv sync --group dev
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
The notebook checks only the two primary workflows: validation and BYOK
narrative-to-FHIR conversion (`/v1/NAR2FHIR`). Use synthetic resources because
notebook outputs are persisted in the file.

## Repository layout

| Path | Contents |
|---|---|
| `src/fhirbridge/api/` | FastAPI app, auth, schemas, middleware, and routers |
| `src/fhirbridge/validation/` | Cascade orchestration, reports, and rule packs |
| `src/fhirbridge/llm/` | BYOK gateway (completion and dictation), policy gates, qualification, extraction rules, the shared narrative-to-FHIR pipeline, and prompts |
| `src/fhirbridge/fhir/` | Typed models, deterministic Bundle assembly, `OperationOutcome`, validator client |
| `src/fhirbridge/terminology/` | Terminology client and result models |
| `src/fhirbridge/storage/` | SQLAlchemy models, tenant sessions, and RLS checks |
| `src/fhirbridge/observability/` | Logging, redaction, metrics, and tracing |
| `docker/` | API and validator images |
| `alembic/` | Database migrations |
| `scripts/bootstrap.py` | App-role, tenant, and API-key provisioning |
| `tests/` | Unit, contract, security, and integration suites |
| `notebooks/` | Executable API smoke test and a step-by-step walkthrough of `/v1/NAR2FHIR` |

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

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a change. Keep changes
small and verification-first:

1. add or update tests;
2. run formatting, lint, types, and relevant test suites;
3. update OpenAPI snapshots when the public contract changes;
4. never log resource bodies, issue messages, prompts, or credentials; and
5. never turn an unavailable verifier into a successful response.

Community participation follows the [Code of Conduct](CODE_OF_CONDUCT.md).
Report vulnerabilities privately according to [SECURITY.md](SECURITY.md).

## License

Licensed under the [Apache License 2.0](LICENSE). Third-party standards,
terminology, and asset notices are documented in [NOTICE](NOTICE).
