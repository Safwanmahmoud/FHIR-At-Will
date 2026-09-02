# Narrative de-identification

FHIR at Will can minimize a narrative before the extraction model receives it.
Set `DEID_MODE=enforced` to replace detected identifiers with random,
request-local typed tokens. The extraction result is parsed first, then tokens in
entity values are restored locally before deterministic FHIR assembly.

`POST /v1/deidentify` exposes the same enforced minimizer without making a model
call. It returns the tokenized narrative and PHI-free processing evidence, and
requires the `conversions:write` scope. The endpoint refuses requests unless
`DEID_MODE=enforced`.

This is a security control, not a HIPAA compliance determination. The service
reports `residual_risk: not_assessed`; the covered entity remains responsible for
the Safe Harbor "actual knowledge" condition, provider agreements, risk analysis,
and all operational safeguards.

## Modes

- `off` is the default and preserves raw-narrative behavior.
- `advisory` detects and reports counts but does not alter model egress. Use it
  only for calibration; it does not protect the outgoing narrative.
- `enforced` substitutes identifiers and fails closed if the gateway sees a known
  original value or restoration leaves a surrogate behind.

`DEID_PROFILE=hipaa_safe_harbor` removes direct identifiers, sub-year dates,
geography, ZIP codes, and ages over 89. `hipaa_limited_data_set` retains dates
and coarse geography; its use requires the operator to establish the applicable
data-use agreement.

## Caller-declared identifiers

`POST /v1/NAR2FHIR` accepts a `known_identifiers` object beside `text`. Supplying
known names, medical-record numbers, dates, phone numbers, email addresses, and
other identifiers makes matching deterministic:

```json
{
  "text": "Jane Smith, MRN A12345, was seen on 01/02/2026.",
  "known_identifiers": {
    "names": ["Jane Smith"],
    "medical_record_numbers": ["A12345"],
    "dates": ["01/02/2026"]
  }
}
```

These values are PHI. They belong only in the request body and are never logged,
persisted, returned as diagnostics, or sent as separate provider metadata.

The layer also applies versioned deterministic patterns, place-name gazetteers,
and a conservative unknown-proper-noun backstop. Detection is intentionally
recall-first: over-redaction is preferable to disclosure. Clinical eponym phrases
such as "Parkinson disease" and "Bell palsy" are protected from surname matching.

## Reversible vault

Tokens such as `[[NAME_0123ABCDEF45]]` contain random bytes and are not derived
from patient information. Their mapping exists only in process memory for one
request and is cleared after assembly. The mapping is never sent to the model.

This design follows the re-identification-code condition in 45 CFR 164.514(c):
the code is not derived from information about the individual, and the mechanism
is not disclosed.

## Voice limitation

Audio is itself identifying and cannot be de-identified before transcription.
When de-identification is enforced, external speech-to-text egress is blocked
unless the operator explicitly sets `DEID_ALLOW_AUDIO_EGRESS=true`. Enabling that
exception does not de-identify the audio and requires an independently appropriate
provider relationship, including a BAA where applicable.

## Known limitations

Automated rules cannot prove that free text contains no "other unique identifying
characteristic." Rare occupations, events, and combinations of clinical facts may
still identify someone. The initial detector set is deterministic and pluggable;
it does not include a statistical NER model.

Replacing dates also prevents the remote model from resolving relative dates
against a hidden anchor. Absolute dates restore cleanly, but local relative-date
arithmetic is not implemented.
