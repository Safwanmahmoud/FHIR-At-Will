# API notes

Interactive OpenAPI documentation is served at `/docs`; the machine-readable
contract is `/openapi.json`. The committed snapshot in
`tests/contract/openapi.snapshot.json` detects accidental contract drift.

All compute endpoints require a bearer API key. Health, version, metrics, error
catalogue, and CapabilityStatement endpoints that are intentionally public are
identified in OpenAPI.

Validation findings are returned in a successful report even when a resource
is non-conformant. HTTP errors represent malformed requests, policy failures,
authentication/authorization failures, or unavailable dependencies.

## Not implemented in v1

HL7 v2, C-CDA, and tabular conversion routes, along with the FHIR `$convert`
and `$extract` facade operations, return `501`. They are explicit capability
stubs and must not be interpreted as partial conversion support.

The project is not a general FHIR server: it does not expose resource CRUD,
search, or persistence APIs. Human review and delivery workflows are planned
and are not part of this alpha release.
