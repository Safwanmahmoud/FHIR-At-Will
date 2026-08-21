# The HL7 FHIR validator, running in its HTTP server mode.
#
# Two things here are load-bearing for the guarantees the API makes about it:
#
# 1. The jar is pinned by version *and* SHA-256. A validator whose bytes can
#    change under us cannot support a reproducible conformance claim: the same
#    bundle would score differently on two deployments that both call themselves
#    "current". The build fails rather than proceeds if the digest moves.
# 2. The IG package cache is warmed at build time. `POST /loadIG` was removed in
#    org.hl7.fhir.core 6.6.0, so the loaded IG set is fixed at startup by -ig.
#    Warming it here also means the container does not need to reach
#    packages.fhir.org to become healthy, so a registry outage cannot turn into
#    a validation outage.

FROM eclipse-temurin:21-jre-noble

# Pinned from the GitHub release asset, not from `latest`.
ARG VALIDATOR_VERSION=6.10.2
ARG VALIDATOR_SHA256=a3addadfa18dfa23146a0a243b6ede68eaad92157a5407738c468bb3d7e4ccd6
ARG IG_PACKAGE=hl7.fhir.us.core#9.0.0
ARG FHIR_VERSION=4.0

LABEL org.opencontainers.image.title="fhirbridge-validator" \
      org.opencontainers.image.description="HL7 FHIR validator in HTTP server mode" \
      org.fhirbridge.validator.version="${VALIDATOR_VERSION}" \
      org.fhirbridge.validator.sha256="${VALIDATOR_SHA256}" \
      org.fhirbridge.ig.package="${IG_PACKAGE}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/validator

RUN curl -fsSL -o validator_cli.jar \
        "https://github.com/hapifhir/org.hl7.fhir.core/releases/download/${VALIDATOR_VERSION}/validator_cli.jar" \
    && echo "${VALIDATOR_SHA256}  validator_cli.jar" | sha256sum -c - \
    && chmod 0444 validator_cli.jar

# Warm the package cache, so a first request does not pay for downloading the
# IG's whole dependency closure and a packages.fhir.org outage cannot become a
# validation outage. -tx n/a keeps the build off the terminology server: a build
# that silently depended on tx.fhir.org being up would not be reproducible.
#
# `install` exits non-zero on 6.10.2. It walks the full transitive closure and
# dies parsing one malformed resource in hl7.fhir.uv.sdc, which US Core reaches
# only indirectly and which the server never loads. The download happens before
# that, so the cache is still populated correctly. Rather than trust `|| true`
# blindly, the build then asserts that the packages we actually require are
# present, and fails if they are not.
RUN java -jar validator_cli.jar install \
        -version "${FHIR_VERSION}" \
        -ig "${IG_PACKAGE}" \
        -tx n/a \
    || echo "install exited non-zero; verifying the cache instead"; \
    ig_dir="$(echo "${IG_PACKAGE}" | tr '|' '#')"; \
    for required in "hl7.fhir.r4.core#4.0.1" "${ig_dir}"; do \
        if [ ! -d "/root/.fhir/packages/${required}" ]; then \
            echo "FATAL: ${required} is not in the package cache" >&2; \
            ls -1 /root/.fhir/packages >&2; \
            exit 1; \
        fi; \
        echo "cached: ${required}"; \
    done

COPY docker/validator/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

ENV VALIDATOR_PORT=8081 \
    FHIR_VERSION=${FHIR_VERSION} \
    IG_PACKAGES=${IG_PACKAGE} \
    TX_SERVER=n/a \
    JAVA_OPTS="-Xmx3g -XX:MaxRAMPercentage=75"

EXPOSE 8081

# No HEALTHCHECK: this build exposes no GET route (GET / is 404), and its only
# readiness signal is a real POST /validateResource. The API's own probe does
# exactly that, including checking that the required profiles resolve, which a
# shallow TCP or GET check could not tell us. See health.py::_validator_status.

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
