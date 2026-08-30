# Model compatibility

Narrative conversion depends on provider and model capabilities that can change
outside this project.

`/v1/NAR2FHIR` requires reliable structured JSON output.

Models are assigned a qualification tier. Unknown models are `unqualified` and
are refused when below `MIN_QUALIFICATION_TIER`. Lowering that threshold is an
operator decision and does not imply that output is safe. Every generated
Bundle must be evaluated using its returned validation report.

Provider availability, context limits, pricing, content filters, and supported
parameters are not stable API contracts of FHIR at Will. Set
`MAX_COST_USD_PER_CONVERSION` to bound individual requests, and treat provider
metadata in responses as operational evidence rather than a performance
guarantee.
