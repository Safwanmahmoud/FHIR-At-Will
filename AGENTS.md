# Engineering contract

This document records the safety and architecture rules referenced by source
comments and tests. It applies to human and automated contributors.

## Core principles

1. The project is a conversion and verification service, not a general FHIR
   repository or clinical decision-support system.
2. Generated content is untrusted until the validation report is inspected.
3. Terminology must be verified through deterministic tools; a model must not
   invent or silently accept a clinical code.
4. Required dependencies fail closed. Unavailable validation, terminology, or
   isolation checks never produce a pass.
5. Clinically critical ambiguity routes to review or rejection.
6. PHI belongs only in request and response bodies. Never place it in URLs,
   logs, metrics, exceptions, traces, or durable audit details.
7. Credentials are secrets: never store, log, echo, or serialize plaintext
   keys. Caller-supplied keys require protected transport.
8. Every verdict records enough code, model, prompt, tool, terminology,
   profile, and validator versions to explain how it was produced.
9. Machine-derived resources carry provenance tags. Human review must never be
   inferred from successful automated validation.
10. Rules may identify impossible or inconsistent values but must not prescribe
    treatment or diagnose a patient.

## LLM and egress

- BYOK credentials arrive per request in `X-LLM-*` headers.
- External clinical-data egress requires an explicit allowlist and, by default,
  `X-PHI-Egress-Acknowledged: true`.
- Production forbids insecure transport and prompt/completion capture.
- Tool-calling loops have iteration and cost limits.
- Provider errors are normalized without including provider responses that may
  contain prompts, outputs, or keys.

## Storage and tenancy

- The API connects as a least-privileged role subject to PostgreSQL row-level
  security. Schema ownership and migrations use a separate role.
- Every tenant-owned table carries `tenant_id`; tenant context is established
  transactionally.
- Audit records are append-only at the database layer and use a hash chain.
- Public identifiers are opaque and must not encode tenant or clinical data.

## Validation and API contracts

- Every report contains all eight layers; checks that did not run are marked
  `skipped` or `not_applicable`.
- Stable error codes are public API. Clinical errors use `OperationOutcome`;
  platform errors use the documented JSON envelope.
- No `GET` endpoint accepts clinical text or resources in query parameters.
- OpenAPI, requirements exports, rule packs, prompts, and migrations are
  reviewed versioned artifacts.
- Tests use synthetic data and intentionally fake credentials only.

## Observability

Log decisions, identifiers, counts, durations, and status—not resource bodies,
validator messages, prompts, completions, provider headers, or credentials.
Metric labels must be bounded and PHI-free. Central redaction is defense in
depth, not permission to log sensitive values.

## Contribution gate

Run the commands in [CONTRIBUTING.md](CONTRIBUTING.md). Security, contract, and
tenant-isolation tests are release gates. Update public documentation whenever
behavior, configuration, or operational responsibility changes.
