# Open design questions

These questions are intentionally public so contributors can understand where
the current architecture is constrained rather than complete.

## Q1: R4 typed models

`fhir.resources` 8.x provides R4B (4.3.0) and R5 models, not native R4 (4.0.1)
models. The current L1 parser uses R4B models constrained to the supported R4
resource allowlist. The pinned HL7 validator running in R4 mode remains the
authority for profile and invariant conformance.

Open question: retain this split, adopt a maintained native-R4 model library, or
generate project-owned R4 models. See [ADR 0004](docs/adr/0004-r4-typed-models.md).

## Q2: Validator implementation-guide loading

Modern validator releases do not support mutating the loaded IG set through
the former HTTP `loadIG` endpoint. The container therefore downloads and
verifies IG packages during the image build and fixes the loaded set at
startup.

Open question: continue producing one immutable image per IG set or introduce a
separate, controlled image-building service. Runtime package mutation is not
acceptable because it makes validation reports irreproducible. See
[ADR 0003](docs/adr/0003-immutable-validator-igs.md).
