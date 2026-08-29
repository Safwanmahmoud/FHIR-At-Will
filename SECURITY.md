# Security policy

FHIR at Will handles credentials and may process clinical data, so security and
privacy reports are treated as release-blocking work.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository:

https://github.com/Safwanmahmoud/FHIR-It-Will/security/advisories/new

Do not open a public issue for suspected credential exposure, PHI disclosure,
authentication or authorization bypass, tenant-isolation failure, unsafe LLM
egress, or a vulnerability in the hosted service. Include affected versions,
reproduction steps, impact, and any suggested mitigation. Use synthetic data
and placeholder credentials in every reproduction.

Maintainers will acknowledge a complete report within five business days and
will coordinate validation, remediation, disclosure, and credit with the
reporter. Timelines depend on severity and upstream dependencies.

## Supported versions

Until the project publishes a stable release, only the current `main` branch is
supported. Public releases will document their support window here.

## Scope and operational responsibility

The policy covers this repository's code and first-party hosted service. It
does not cover vulnerabilities solely in an operator's infrastructure or
third-party LLM, terminology, database, or observability provider. Please still
report issues where this project uses those systems unsafely.

Self-hosting does not itself provide HIPAA, GDPR, or other regulatory
compliance. Operators remain responsible for deployment security, agreements,
access control, encryption, retention, backups, and incident response.
