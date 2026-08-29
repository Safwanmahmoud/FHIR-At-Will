# Changelog

All notable changes to FHIR at Will will be documented here. The project uses
semantic versioning after its first public release.

## Unreleased

- Prepare the repository for public contribution and coordinated disclosure.
- Add the agentic `/v1/craft` and streaming craft workflows.
- Add grounded two-stage `/v1/NAR2FHIR` conversion.
- Add authenticated terminology search.

## 0.1.0 - 2026-08-28

Initial alpha:

- FHIR R4 validation cascade with structural, profile, terminology, invariant,
  plausibility, and routing layers.
- FHIR validator and terminology sidecar integration with fail-closed errors.
- BYOK LLM gateway, qualification, egress, and cost controls.
- API-key authentication, PostgreSQL tenant isolation, append-only audit data,
  health endpoints, metrics, tracing, and container builds.

This alpha is not a medical device and is not suitable for unsupervised
clinical use.
