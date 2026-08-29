# Railway configuration

`.railway/railway.ts` defines the four-service Railway stack:

- **API** — public FastAPI service;
- **Validator** — private HL7 validator sidecar;
- **Postgres** — persistent tenant and API-key storage; and
- **Redis** — persistent cache/coordination service.

The checked-in definition contains no secret values. Railway generates and
seals `APP_DB_PASSWORD` from an alphanumeric 48-character template secret.

The first API pre-deploy creates the least-privileged PostgreSQL role, applies
migrations, creates the `Railway` tenant, and prints its API key once. Retrieve
that key from the first API deployment's **Pre-deploy logs** and save it
immediately; only its Argon2id hash is stored.

## Apply to a maintainer project

Railway IaC secret generators require Railway CLI `5.42.1` or newer.

```bash
railway upgrade --yes
railway link
railway config plan
railway config apply
```

Then enable public HTTP networking for **API** only. Never expose Validator,
Postgres, or Redis publicly.

## Create the reusable template

Generate a template from the tested project:

```bash
railway templates create --project <project-id> --environment production --json
```

In the template editor:

1. confirm that `APP_DB_PASSWORD` remains a sealed generated variable;
2. confirm that only API has public networking;
3. add descriptions to every user-editable variable;
4. deploy the draft into a fresh test project; and
5. publish it using `docs/railway-template.md` as the marketplace overview.

The default is intentionally a **sandbox**: it uses the public
`tx.fhir.org/r4` terminology endpoint and sets `FHIRBRIDGE_ENV=staging`. For a
production deployment, operators must configure an authenticated,
operator-controlled terminology service before changing the environment to
`production`. Production also requires `FHIRBRIDGE_EPHEMERAL_KEY`, containing
exactly 32 random bytes encoded as base64.
