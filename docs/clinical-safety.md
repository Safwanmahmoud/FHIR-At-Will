# Clinical safety boundaries

FHIR at Will is alpha software, not a medical device, and not clinical
decision support. It must not be used to diagnose, prescribe, or make
unsupervised care decisions.

Generated resources are machine proposals. A conformant resource can still be
clinically wrong, incomplete, or unsupported by the source. L1-L5 test
structure, profiles, terminology, invariants, and selected plausibility rules;
they do not prove source fidelity or mention coverage. L6 and L7 remain
unimplemented.

The provenance tag `ai-derived` means a language model proposed content.
`unqualified-model` and `nondeterminism-risk` disclose model and reproduction
limitations. `human-reviewed` is reserved for an explicit future review
workflow and is never inferred from automated validation.

`machine-inferred` means at least one element required by FHIR was filled from
a reviewed constant table because the source did not state it — for example
`Observation.status` or `Encounter.class`. Such a value is auditable and
reproducible but is not evidence about the patient. `POST /v1/NAR2FHIR` reports
every inferred element, and every element it could not ground, in the response's
`assembly` list; that list is the record to review, not the tag alone.

Operators and downstream clients must:

- inspect the complete validation report, including skipped checks;
- keep generated output separate from trusted clinical records until reviewed;
- use qualified personnel and deployment-specific acceptance criteria;
- retain source/version provenance needed to investigate a result;
- monitor model, terminology, profile, and rule changes; and
- provide a safe failure path when dependencies or evidence are unavailable.

Public demos, tests, issues, and documentation must use synthetic data only.
