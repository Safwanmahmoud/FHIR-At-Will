#!/usr/bin/env bash
# Start validator_cli.jar in HTTP server mode.
#
# -allowNetworkAccess is mandatory, not optional: without it the server binds
# loopback only and every request from another container is refused. It is safe
# here *because this port is never published*. The validator has no
# authentication and no authorisation of any kind, so exposing it to a network
# any wider than the private one shared with the API would hand an anonymous
# caller a resource fetcher that runs inside your perimeter. Keep it internal.
set -euo pipefail

port="${VALIDATOR_PORT:-8081}"
fhir_version="${FHIR_VERSION:-4.0}"
tx_server="${TX_SERVER:-n/a}"

# IG_PACKAGES is a comma-separated list so one env var can carry several guides.
ig_args=()
IFS=',' read -ra packages <<<"${IG_PACKAGES:-}"
for package in "${packages[@]}"; do
    trimmed="$(echo "${package}" | xargs)"
    if [[ -n "${trimmed}" ]]; then
        ig_args+=("-ig" "${trimmed}")
    fi
done

echo "validator: port=${port} fhir=${fhir_version} tx=${tx_server} igs=${IG_PACKAGES:-none}"

# shellcheck disable=SC2086
exec java ${JAVA_OPTS:-} -jar /opt/validator/validator_cli.jar server "${port}" \
    -version "${fhir_version}" \
    -tx "${tx_server}" \
    -allowNetworkAccess \
    "${ig_args[@]}"
