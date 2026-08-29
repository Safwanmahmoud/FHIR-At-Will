# Deploy and Host FHIR at Will with Railway

Deploy FHIR at Will as a ready-to-use FHIR R4 validation and
narrative-to-FHIR API. The template provisions the API, a pinned HL7 validator
sidecar, PostgreSQL with row-level tenant isolation, and Redis. LLM credentials
remain bring-your-own-key and are never bundled with the deployment.

> **Clinical safety:** FHIR at Will is alpha software, not a medical device.
> Start with synthetic data. Self-hosting does not by itself make a deployment
> HIPAA, GDPR, or other regulatory-framework compliant.

## About Hosting FHIR at Will

FHIR at Will validates FHIR R4 resources through a layered, fail-closed
pipeline and can construct Bundles from clinical narrative using a
caller-supplied model and provider key. Railway runs the public API while
keeping the validator and data services on its private network. PostgreSQL
migrations, least-privileged role provisioning, and initial tenant creation run
automatically before the API starts.

The template defaults to sandbox mode because it uses HL7's public
`tx.fhir.org/r4` terminology endpoint. Before processing real clinical data,
configure an authenticated terminology service that you operate, review all
egress and retention controls, and change `FHIRBRIDGE_ENV` to `production`.

## Common Use Cases

- Validate FHIR R4 resources and Bundles against US Core.
- Add a `$validate` endpoint to an integration or test environment.
- Prototype BYOK narrative-to-FHIR conversion with OpenRouter.
- Evaluate validation reports, provenance, and clinical plausibility routing.
- Build a private terminology-backed deployment for an approved environment.

## Dependencies for FHIR at Will Hosting

- FastAPI/Python API built from the repository's pinned lockfile.
- HL7 FHIR validator CLI `6.10.2` with US Core `9.0.0`.
- Railway PostgreSQL for tenant, API-key, and audit data.
- Railway Redis for cache and coordination.
- A FHIR R4 terminology server; the sandbox default is `tx.fhir.org`.
- An optional caller-provided LLM provider key for conversion endpoints.

### After Deployment

1. Open the API service's first **Pre-deploy logs**.
2. Copy the one-time `api_key` printed by the bootstrap step and store it in a
   secret manager. It cannot be recovered from the database.
3. Open `https://<your-api-domain>/docs` for the interactive API reference.
4. Call `/livez` to check the process and `/readyz` to verify PostgreSQL, the
   validator, terminology, and row-level-security enforcement.
5. Send the bootstrap key as `Authorization: Bearer <api_key>`.

If the bootstrap key scrolls out of view, do not search the database for it:
only its Argon2id hash is stored. Issue a replacement key through an approved
operator workflow.

### Deployment Dependencies

- [FHIR at Will source and documentation](https://github.com/Safwanmahmoud/FHIR-It-Will)
- [Railway template deployment guide](https://docs.railway.com/templates/deploy)
- [HL7 FHIR R4 specification](https://hl7.org/fhir/R4/)
- [HL7 terminology service](https://tx.fhir.org/)

### Implementation Details

Only the API receives a public domain. It reaches the validator, PostgreSQL,
and Redis through Railway private-network references. The API's pre-deploy
container connects as the PostgreSQL owner only long enough to apply migrations
and provision the `fhirbridge_app` role. The runtime connection uses that
least-privileged role so PostgreSQL row-level security remains effective.

The validator image pins both the validator version and SHA-256 digest and
warms its implementation-guide cache during the build. Expect the validator to
require substantially more memory and build time than the API.

### Why Deploy FHIR at Will on Railway?

Railway deploys the complete API, validator, PostgreSQL, and Redis topology in
one project while providing private networking, generated secrets, deployment
logs, health checks, and persistent managed data services. This keeps the
one-click sandbox convenient without flattening the security boundary between
the public API and its unauthenticated validator sidecar.
