# Public release checklist

Complete these hosted settings immediately before changing repository
visibility. They cannot be verified from a local clone.

## GitHub repository

- Confirm the canonical repository remains
  `Safwanmahmoud/FHIR-It-Will`.
- Set a concise description, `https://fhiratwill.com` as the website, and
  relevant topics such as `fhir`, `healthcare`, `interoperability`, `fastapi`,
  and `python`.
- Confirm GitHub detects the Apache-2.0 license.
- Enable Issues and private vulnerability reporting.
- Enable the dependency graph, Dependabot alerts, Dependabot security updates,
  secret scanning, push protection, and validity checks where available.
- Restrict Actions permissions to read repository contents by default and
  require approval for workflows from first-time contributors.
- Protect `main`: require pull requests, passing CI jobs, resolved
  conversations, and no force pushes or deletions.
- Add a social preview and verify that it is safe to redistribute.
- Review collaborators, deploy keys, webhooks, environments, Actions secrets,
  Pages settings, and branch/tag rules before visibility changes.

## Rewritten history

The local `main` history was rewritten to remove a personal email address,
production notebook metadata, and persisted notebook outputs. Replacing the
private remote history requires a coordinated force push and invalidates every
old commit id and existing clone.

Before replacement:

1. ensure no collaborator has unpublished work based on the old history;
2. retain the private backup bundle outside the repository;
3. make the force push while the repository is still private;
4. ask collaborators to clone again rather than merge old history; and
5. rerun GitHub secret scanning after the new history is uploaded.

Do not upload the backup bundle or old refs to the public repository.

## Website and hosted service

- Update `fhiratwill.com` so roadmap, BYOK/NAR2FHIR capability claims, and clone
  commands match this repository.
- Confirm the playground and documentation never encourage real patient data.
- Treat the Railway hostname as public infrastructure: keep authentication,
  rate limits, monitoring, TLS, and incident response in place.
- Rotate any hosted FHIR API key used while producing previously committed
  notebook traces.
- Verify the linked landing-page repository is intentionally public before
  keeping that README link.

## Release

- Review and commit the public-readiness changes.
- Run the complete test, lint, type, dependency, notebook, documentation, and
  container gates from `CONTRIBUTING.md`.
- Push rewritten history only after explicit approval.
- Wait for CI to pass on GitHub.
- Create the first version tag and release notes from `CHANGELOG.md`.
- Change visibility only after license detection, security settings, links, and
  the hosted service have been checked from a signed-out browser.
