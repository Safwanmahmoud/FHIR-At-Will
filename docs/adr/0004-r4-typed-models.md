# ADR 0004: Use R4B typed models with an R4 authority

- Status: Accepted with open review
- Date: 2026-08-21

## Context

The maintained `fhir.resources` 8.x package exposes R4B (4.3.0) and R5 models,
not native R4 (4.0.1) models. The service targets FHIR R4.

## Decision

Use R4B typed models for L1 parsing and round-tripping, constrained to the
project's R4 resource allowlist. Use the pinned HL7 validator in R4 mode as the
authority for R4 profile and invariant conformance. Reports disclose both model
and target FHIR versions.

## Consequences

L1 catches structural/model errors but cannot by itself establish R4
conformance. L2/L4 are required and fail closed when the validator is
unavailable. Revisit this decision if a maintained native-R4 model library or
generated model set meets the project's typing and maintenance requirements.
