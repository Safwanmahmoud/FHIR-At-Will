# Bring-your-own-key LLM access

FHIR at Will accepts caller-owned provider credentials for narrative conversion.
The service does not include a provider key.

## Supplying credentials

Send `X-LLM-Provider`, `X-LLM-Model`, and `X-LLM-API-Key` on each conversion or
probe request. `X-LLM-Base-Url` and `X-LLM-Extra-Headers` are optional and remain
subject to egress policy. Do not place credentials in URLs, notebooks, source
files, logs, or issue reports.

## Transport security

Provider keys and FHIR API keys must travel over TLS. Plain HTTP is refused
unless `ALLOW_INSECURE_TRANSPORT=true`, which is a local-development escape
hatch and is forbidden when `FHIRBRIDGE_ENV=production`.

## Egress policy

External hosts must appear in `LLM_EGRESS_ALLOWLIST`. An empty list blocks
external calls. `LOCAL_ONLY_MODE=true` restricts calls to loopback endpoints
and overrides external allowlisting. Provider ids can be narrowed with
`LLM_ALLOWED_PROVIDERS`.

## PHI egress

When clinical content would leave the deployment boundary,
`X-PHI-Egress-Acknowledged: true` is required by default. The acknowledgement
is a policy gate, not consent, a data-processing agreement, or a compliance
determination. Operators must independently establish whether a provider may
receive the data.

Use `/v1/llm/probe` to test credentials and routing with a PHI-free request
before sending clinical content.

## Ephemeral keys and async jobs

The current conversion endpoints are synchronous and do not persist caller
credentials. Production still requires `FHIRBRIDGE_EPHEMERAL_KEY` so future
queued handoff cannot place plaintext BYOK material in Redis. Persistent
credential storage is disabled by default; enabling it requires a separate
master key and operational key-rotation process.

## Logging

Prompt and completion capture may contain PHI and is disabled by default.
`DEBUG_CAPTURE_LLM_IO=true` is forbidden in production. Central redaction is
defense in depth and does not make arbitrary provider payloads safe to log.
