import {
  defineRailway,
  github,
  group,
  postgres,
  project,
  redis,
  service,
} from "railway/iac";

const repository = "Safwanmahmoud/FHIR-At-Will";
const branch = "main";

export default defineRailway(() => {
  const database = postgres("Postgres");
  const cache = redis("Redis");

  const validator = service("Validator", {
    source: github(repository, { branch }),
    env: {
      RAILWAY_DOCKERFILE_PATH: "docker/validator/Dockerfile",
      VALIDATOR_PORT: "8081",
      FHIR_VERSION: "4.0",
      IG_PACKAGES: "hl7.fhir.us.core#9.0.0",
      TX_SERVER: "https://tx.fhir.org/r4",
      JAVA_OPTS: "-Xmx3g -XX:MaxRAMPercentage=75",
    },
  });

  const api = service("API", {
    source: github(repository, { branch }),
    preDeploy: [
      'DATABASE_URL="$OWNER_DATABASE_URL" alembic upgrade head',
      'DATABASE_URL="$OWNER_DATABASE_URL" python scripts/bootstrap.py --tenant-name "$BOOTSTRAP_TENANT_NAME" --app-role "$APP_DB_USER" --app-password "$APP_DB_PASSWORD" --only-if-absent',
    ],
    healthcheck: "/livez",
    healthcheckTimeout: 300,
    env: {
      RAILWAY_DOCKERFILE_PATH: "docker/api/Dockerfile",

      // Set APP_DB_PASSWORD to a generated alphanumeric template secret. The
      // URL references the same value so bootstrap and runtime cannot drift.
      APP_DB_USER: "fhirbridge_app",
      APP_DB_PASSWORD: {
        description:
          "Generated password for the least-privileged PostgreSQL runtime role.",
        generator:
          'secret(48, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")',
        isSealed: true,
      },
      OWNER_DATABASE_URL:
        "postgresql+asyncpg://${{Postgres.PGUSER}}:${{Postgres.PGPASSWORD}}@${{Postgres.PGHOST}}:${{Postgres.PGPORT}}/${{Postgres.PGDATABASE}}",
      DATABASE_URL:
        "postgresql+asyncpg://fhirbridge_app:${{API.APP_DB_PASSWORD}}@${{Postgres.PGHOST}}:${{Postgres.PGPORT}}/${{Postgres.PGDATABASE}}",
      REDIS_URL: cache.env.REDIS_URL,

      VALIDATOR_URL: "http://${{Validator.RAILWAY_PRIVATE_DOMAIN}}:8081",
      TERMINOLOGY_URL: {
        value: "https://tx.fhir.org/r4",
        description:
          "Sandbox terminology endpoint. Use an operator-controlled service before production.",
      },
      TERMINOLOGY_AUTH_MODE: "none",
      DEFAULT_IG_PACKAGES: "hl7.fhir.us.core#9.0.0",
      DEFAULT_FHIR_VERSION: "4.0.1",
      VALIDATOR_VERSION: "6.10.2",

      // The public terminology service makes this a sandbox deployment. Move
      // to production only after configuring an operator-controlled service.
      FHIRBRIDGE_ENV: {
        value: "staging",
        description:
          "Sandbox default. Production additionally requires controlled terminology and an ephemeral key.",
      },
      REQUIRE_RLS_ENFORCEMENT: "true",
      ALLOW_INSECURE_TRANSPORT: "false",
      CREDENTIAL_STORAGE: "disabled",

      LLM_MODE: "byok",
      LLM_ALLOWED_PROVIDERS: "*",
      LLM_EGRESS_ALLOWLIST: {
        value: "openrouter.ai",
        description:
          "Comma-separated LLM endpoint hosts callers may reach with their own provider keys.",
      },
      LOCAL_ONLY_MODE: "false",
      REQUIRE_PHI_EGRESS_ACK: "true",
      MIN_QUALIFICATION_TIER: "unqualified",
      MAX_COST_USD_PER_CONVERSION: {
        value: "1.00",
        description: "Maximum estimated LLM spend permitted for one conversion.",
      },

      BOOTSTRAP_TENANT_NAME: {
        value: "Railway",
        description: "Display name for the initial tenant created on first deploy.",
      },
      JSON_LOGS: "true",
      LOG_LEVEL: "INFO",
      VALIDATOR_TIMEOUT_S: "120",
      TERMINOLOGY_TIMEOUT_S: "30",
    },
  });

  return project("FHIR at Will", {
    resources: [
      group("Application", [api, validator]),
      group("Data", [database, cache]),
    ],
  });
});
