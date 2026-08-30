# Model compatibility

Narrative conversion depends on provider and model capabilities that can change
outside this project.

`/v1/NAR2FHIR` requires reliable structured JSON output.

The model's only task is extraction: identifying the facts a narrative states and
grouping the ones that describe the same real-world thing, via the `instance` key.
Bundle assembly is deterministic Python, so a weaker model degrades extraction
recall and grouping accuracy rather than producing malformed FHIR. A model that
mis-groups two measurements will still yield structurally valid resources with the
wrong values attached, which validation cannot detect — read the response's
`assembly` list and check grouping against the source.

Models are assigned a qualification tier. Unknown models are `unqualified` and
are refused when below `MIN_QUALIFICATION_TIER`. Lowering that threshold is an
operator decision and does not imply that output is safe. Every generated
Bundle is returned unvalidated and must be submitted separately to
`POST /v1/validate` before it is trusted.

Provider availability, context limits, pricing, content filters, and supported
parameters are not stable API contracts of FHIR at Will. Set
`MAX_COST_USD_PER_CONVERSION` to bound individual requests, and treat provider
metadata in responses as operational evidence rather than a performance
guarantee.
