# Bring-your-own-key LLM access

FHIR at Will accepts caller-owned provider credentials for narrative conversion.
The service does not include a provider key.

## Supplying credentials

Send `X-LLM-Provider`, `X-LLM-Model`, and `X-LLM-API-Key` on each conversion
request. `X-LLM-Base-Url` and `X-LLM-Extra-Headers` are optional and remain subject
to egress policy. Do not place credentials in URLs, notebooks, source files, logs,
or issue reports.

`POST /v1/VOICE2FHIR` makes a second, independent BYOK call to transcribe audio and
takes its own `X-STT-Provider`, `X-STT-Model`, `X-STT-API-Key`, and optional
`X-STT-Base-Url`, `X-STT-Extra-Headers`, and `X-STT-Language`. It is separate because
litellm cannot transcribe through OpenRouter, so the dictation provider (Gemini by
default) is usually not the extraction provider. Both calls obey every gate below;
the single `X-PHI-Egress-Acknowledged` header covers both hops.

## Transport security

Provider keys and FHIR API keys must travel over TLS. Plain HTTP is refused
unless `ALLOW_INSECURE_TRANSPORT=true`, which is a local-development escape
hatch and is forbidden when `FHIRBRIDGE_ENV=production`.

## Egress policy

External hosts must appear in `LLM_EGRESS_ALLOWLIST`. An empty list blocks
external calls. `LOCAL_ONLY_MODE=true` restricts calls to loopback endpoints
and overrides external allowlisting. Provider ids can be narrowed with
`LLM_ALLOWED_PROVIDERS`. Dictation is governed by the same lists, so a voice
provider's host must also be allowlisted (Gemini is
`generativelanguage.googleapis.com`) and its provider id permitted.

## PHI egress

When clinical content would leave the deployment boundary,
`X-PHI-Egress-Acknowledged: true` is required by default. The acknowledgement
is a policy gate, not consent, a data-processing agreement, or a compliance
determination. Operators must independently establish whether a provider may
receive the data.

## Optional narrative de-identification

Set `DEID_MODE=enforced` to replace detected identifiers before the extraction
call and restore them locally before FHIR assembly. Callers should provide known
patient identifiers in the request body's `known_identifiers` object to make
matching deterministic. `advisory` mode reports detections but sends the original
narrative and is not protective.

See [Narrative de-identification](deidentification.md) for profiles, limitations,
and the external-audio restriction. De-identification is not a substitute for a
BAA or an operator HIPAA determination.

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
