# Terminology setup

Terminology validation is a required verification dependency. If the configured
service cannot answer a required check, FHIR at Will fails closed.

## Development

The development default is `https://tx.fhir.org/r4`. It has no availability,
privacy, or performance guarantee. Requests send codes and ValueSet context,
not complete resources, but combinations of codes may still be sensitive.

## Production

Production configuration rejects the public `tx.fhir.org` default. Use a
service you operate or a contracted provider with the code systems, value sets,
implementation guides, authentication, privacy terms, and availability your
deployment requires.

Configure:

- `TERMINOLOGY_URL`
- `TERMINOLOGY_AUTH_MODE=none|basic|bearer`
- `TERMINOLOGY_USERNAME` and `TERMINOLOGY_PASSWORD` for basic auth
- `TERMINOLOGY_TOKEN` for bearer auth
- `TERMINOLOGY_TIMEOUT_S`
- `TERMINOLOGY_CACHE_TTL`

Credentials are secret and must be supplied through deployment secret
management rather than committed environment files.

## Licensing

FHIR at Will does not distribute complete SNOMED CT, LOINC, RxNorm, UCUM, or
other terminology releases. Small codes and displays in examples and rules do
not replace a terminology license. Operators must obtain and comply with the
rights required for the content they load or query. See the root `NOTICE` for
upstream references.

## Failure behavior

Transport failures, timeouts, malformed responses, and unavailable required
content return a disclosed failure rather than an assumed pass. An unknown
profile or ValueSet is a configuration/content problem and should be corrected
instead of suppressed.
