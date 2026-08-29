# ADR 0003: Build immutable validator IG sets

- Status: Accepted
- Date: 2026-08-21

## Context

The HL7 validator's HTTP server no longer supports safely loading implementation
guides at runtime. Validation results must identify a reproducible validator and
IG set.

## Decision

Pin the validator JAR by version and SHA-256. Download the configured IG package
and dependency closure during the validator image build, verify required cache
entries, and pass the fixed IG set at startup. Do not expose the validator
outside the private service network.

## Consequences

Changing IGs requires a new image. Builds need registry access, and operators
must comply with package licenses. In exchange, one running image cannot change
its conformance rules behind an existing version claim.
