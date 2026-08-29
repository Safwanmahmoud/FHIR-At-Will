# ADR 0006: Add storage with its consuming milestone

- Status: Accepted
- Date: 2026-08-21

## Context

The roadmap anticipates documents, conversions, reviews, delivery, and
evaluation data, but their behavior and retention requirements evolve with the
features that consume them.

## Decision

M0/M1 define only tenancy, authentication, idempotency, audit, row-level
security, and reusable storage primitives. Add later tables in the milestone
that implements and tests their readers, writers, authorization, retention, and
migrations.

## Consequences

The schema avoids untested speculative tables and premature compatibility
constraints. Future milestones must include migrations, tenant-isolation tests,
retention behavior, and operational documentation with each new data model.
