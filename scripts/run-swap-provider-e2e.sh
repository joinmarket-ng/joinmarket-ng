#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DATA_DIR=${RING_E2E_DATA_DIR:-${PROJECT_ROOT}/tmp/swap-provider-e2e}
PROJECT=${SWAP_PROVIDER_E2E_PROJECT:-jm-swap-provider-e2e}
REQUEST_FILE=${DATA_DIR}/restart-provider.request
COMPLETE_FILE=${DATA_DIR}/restart-provider.complete
RUNNER_TIMEOUT_SECONDS=${SWAP_PROVIDER_RUNNER_TIMEOUT_SECONDS:-900}
runner_pid=

docker_endpoint=${DOCKER_HOST:-}
if [ -z "$docker_endpoint" ]; then
    docker_endpoint=$(docker context inspect --format '{{.Endpoints.docker.Host}}')
fi
if [[ "$docker_endpoint" != unix://* ]]; then
    echo "Swap-provider E2E requires a local Docker Unix socket context" >&2
    exit 1
fi

compose() {
    docker compose \
        -p "$PROJECT" \
        -f "${PROJECT_ROOT}/jmswap/docker-compose.ring-e2e.yml" \
        --profile ring-e2e \
        --profile swap-provider-e2e \
        "$@"
}

cleanup() {
    rm -f "$REQUEST_FILE" "$COMPLETE_FILE"
    if [ -n "$runner_pid" ] && kill -0 "$runner_pid" 2>/dev/null; then
        kill "$runner_pid" 2>/dev/null || true
        wait "$runner_pid" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

mkdir -p "$DATA_DIR"
rm -f "$REQUEST_FILE" "$COMPLETE_FILE"

compose run --rm swap-provider-test-runner &
runner_pid=$!
deadline=$((SECONDS + RUNNER_TIMEOUT_SECONDS))

while [ ! -f "$REQUEST_FILE" ]; do
    if ! kill -0 "$runner_pid" 2>/dev/null; then
        set +e
        wait "$runner_pid"
        runner_rc=$?
        set -e
        runner_pid=
        if [ "$runner_rc" -eq 0 ]; then
            echo "Swap-provider test exited before requesting a restart" >&2
            exit 1
        fi
        exit "$runner_rc"
    fi
    if [ "$SECONDS" -ge "$deadline" ]; then
        echo "Timed out waiting for the swap-provider restart request" >&2
        exit 1
    fi
    sleep 1
done

compose restart -t 60 ring-maker1
touch "$COMPLETE_FILE"

wait "$runner_pid"
runner_pid=
