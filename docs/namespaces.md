# Canonical namespaces

FHIR at Will currently uses project canonical identifiers under
`https://fhirbridge.org/`.

| Canonical | Purpose |
|---|---|
| `https://fhirbridge.org/CodeSystem/errors` | Stable API error codes |
| `https://fhirbridge.org/CodeSystem/provenance-tags` | Machine-generation and review provenance tags |

These URLs are identifiers; clients must not require an HTTP fetch to interpret
a response. Definitions are published by the running API where applicable,
including `GET /v1/error-codes`, and by this repository's source and OpenAPI
contract.

The namespace is project-owned, not an HL7 namespace. Codes are versioned API
surface and will not be repurposed. Renaming a code or changing its established
meaning requires a breaking release and migration guidance.

Until dereferenceable web pages are published at the canonical host, this file
is the authoritative human-readable namespace policy.
