# Deployment

FHIR at Will is self-hostable alpha software. A production deployment requires
controls beyond the included development Compose stack.

## Local stack

Copy `.env.example` to `.env`, choose distinct database-owner and application
passwords, then provision the application role before starting the API:

```bash
docker compose --profile setup run --rm bootstrap
docker compose up -d
```

Check `/livez`, `/readyz`, and `/version`. Readiness verifies PostgreSQL row
isolation and required validation dependencies.

## Database role

Migrations and bootstrap run as the schema owner. The API must use a distinct
role without `SUPERUSER` or `BYPASSRLS`; PostgreSQL exempts those roles from row
level security. Keep `REQUIRE_RLS_ENFORCEMENT=true`.

## Validator sidecar

The validator has no authentication and can fetch external resources. Keep it
on a private network and expose only the API. The supplied image pins the
validator JAR by version and SHA-256 and downloads the configured IG package
during build.

Production requires HTTPS for a non-loopback `VALIDATOR_URL`. Treat a validator
or terminology outage as an API outage: the service intentionally returns
`503` rather than weakening verification.

## Implementation guides

`DEFAULT_IG_PACKAGES` must match packages baked into the validator image.
Changing an IG set requires a new image and validation of the resulting
profiles. Operators are responsible for the license terms of downloaded HL7
packages; see [terminology setup](terminology-setup.md) and the root `NOTICE`.

## Production minimums

- Terminate TLS before any API key or clinical request reaches the service.
- Set `FHIRBRIDGE_ENV=production` and a strong
  `FHIRBRIDGE_EPHEMERAL_KEY`.
- Use a private, authenticated production terminology service rather than
  `tx.fhir.org`.
- Keep `ALLOW_INSECURE_TRANSPORT=false` and
  `DEBUG_CAPTURE_LLM_IO=false`.
- Restrict `LLM_EGRESS_ALLOWLIST`; do not use `*`.
- Protect metrics, traces, logs, backups, and database access.
- Define retention, incident response, key rotation, and provider agreements.

The included Compose file is a development topology, not a compliance
certification or production reference architecture.
