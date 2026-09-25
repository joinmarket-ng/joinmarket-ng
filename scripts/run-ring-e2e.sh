#!/usr/bin/env bash
# Run a live channel-ring E2E suite against a freshly reset stack.
#
# Both profiles keep LND, Tor and wallet state in host directories while bitcoind
# keeps its chain in a Docker volume. Resetting only one of the two leaves LND
# ahead of a fresh chain, which surfaces as "Block height out of range" and fails
# every test for reasons that have nothing to do with the code under test. This
# script always resets both, and wipes the host state from inside a container
# because the services write it as root.
#
# Both stacks run under their own Compose project name so a reset never touches
# another checkout's stack, a signet deployment, or the channel-buyout regtest
# project.
#
# Usage:
#   scripts/run-ring-e2e.sh [lnd-external|ring|all]   (default: all)
#
# Environment:
#   RING_E2E_KEEP=1     leave the stack running after the tests
#   RING_E2E_NO_RESET=1 reuse the current stack instead of resetting it
#   RING_E2E_NO_BUILD=1 reuse existing application images for the ring suite
#   JM_BUYOUT_LND_IMAGE override the patched LND image (build it with lnd/Dockerfile)
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$PROJECT_ROOT"

# Always import this worktree, even when another checkout is installed editable.
PROJECT_PYTHONPATH=$(printf '%s:' \
    "$PROJECT_ROOT/jmcore/src" \
    "$PROJECT_ROOT/jmwallet/src" \
    "$PROJECT_ROOT/directory_server/src" \
    "$PROJECT_ROOT/jmswap/src" \
    "$PROJECT_ROOT/orderbook_watcher/src" \
    "$PROJECT_ROOT/maker/src" \
    "$PROJECT_ROOT/taker/src")
export PYTHONPATH="${PROJECT_PYTHONPATH%:}${PYTHONPATH:+:$PYTHONPATH}"

SUITE=${1:-all}
KEEP=${RING_E2E_KEEP:-0}
NO_RESET=${RING_E2E_NO_RESET:-0}
NO_BUILD=${RING_E2E_NO_BUILD:-0}

LND_EXTERNAL_DATA_DIR=$(realpath -m "${LND_EXTERNAL_DATA_DIR:-${PROJECT_ROOT}/tmp/lnd-external}")
RING_E2E_DATA_DIR=$(realpath -m "${RING_E2E_DATA_DIR:-${PROJECT_ROOT}/tmp/ring-e2e}")
RING_E2E_PROJECT=${RING_E2E_PROJECT:-jm-ring-e2e}
LND_EXTERNAL_PROJECT=${LND_EXTERNAL_PROJECT:-jm-lnd-external}
export LND_EXTERNAL_DATA_DIR RING_E2E_DATA_DIR RING_E2E_PROJECT LND_EXTERNAL_PROJECT

if [ "$SUITE" != "lnd-external" ] && [ "$SUITE" != "ring" ] && [ "$SUITE" != "all" ]; then
    echo "usage: $0 [lnd-external|ring|all]" >&2
    exit 2
fi

for data_dir in "$LND_EXTERNAL_DATA_DIR" "$RING_E2E_DATA_DIR"; do
    case "$data_dir" in
    "$PROJECT_ROOT"/tmp/*) ;;
    *)
        echo "refusing to wipe E2E data outside $PROJECT_ROOT/tmp: $data_dir" >&2
        exit 2
        ;;
    esac
done

lnd_external_compose() {
    docker compose \
        -p "$LND_EXTERNAL_PROJECT" \
        -f "${PROJECT_ROOT}/jmswap/docker-compose.lnd-external.yml" \
        --profile lnd_external "$@"
}

ring_compose() {
    docker compose \
        -p "$RING_E2E_PROJECT" \
        -f "${PROJECT_ROOT}/jmswap/docker-compose.ring-e2e.yml" \
        -f "${PROJECT_ROOT}/jmswap/docker-compose.ring-e2e.host.yml" \
        --profile ring-e2e "$@"
}

# Service-written state is root-owned, so remove it with the same privileges.
wipe_host_state() {
    local target=$1
    [ -e "$target" ] || return 0
    docker run --rm \
        -v "$(dirname "$target"):/wipe" \
        alpine:3.20 sh -c 'rm -rf -- "/wipe/$1"' sh "$(basename "$target")"
}

build_ring_images() {
    local directory_image=${DIRECTORY_SERVER_IMAGE:-ring-e2e-directory:latest}
    local orderbook_image=${ORDERBOOK_WATCHER_IMAGE:-ring-e2e-orderbook:latest}
    local maker_image=${MAKER_IMAGE:-ring-e2e-maker:latest}
    if [ "$NO_BUILD" = "1" ]; then
        for image in "$directory_image" "$orderbook_image" "$maker_image" ring-e2e-tor:latest; do
            docker image inspect "$image" >/dev/null
        done
        return 0
    fi
    docker build -f "${PROJECT_ROOT}/directory_server/Dockerfile" \
        --target production -t "$directory_image" "$PROJECT_ROOT"
    docker build -f "${PROJECT_ROOT}/orderbook_watcher/Dockerfile" \
        -t "$orderbook_image" "$PROJECT_ROOT"
    docker build -f "${PROJECT_ROOT}/maker/Dockerfile" \
        -t "$maker_image" "$PROJECT_ROOT"
    docker build -f "${PROJECT_ROOT}/tests/e2e/Dockerfile.ring-tor" \
        -t ring-e2e-tor:latest "$PROJECT_ROOT"
}

# The patched LND build carries the channel-escrow RPC the buyout flow needs; the
# ring suite reuses it so both flows run one backend. It is built out of band by
# lnd/Dockerfile, so fail early with a pointer instead of a Docker pull error.
require_lnd_image() {
    local lnd_image=${JM_BUYOUT_LND_IMAGE:-jm-buyout-lnd:v0.21.3-beta}
    if ! docker image inspect "$lnd_image" >/dev/null 2>&1; then
        echo "missing patched LND image $lnd_image; build it with:" >&2
        echo "  docker build -t $lnd_image ${PROJECT_ROOT}/lnd" >&2
        return 1
    fi
}

wait_for_healthy() {
    local compose_function=$1
    local timeout_seconds=$2
    shift 2
    local services=("$@")
    local deadline=$((SECONDS + timeout_seconds))
    while [ "$SECONDS" -lt "$deadline" ]; do
        local running_services failed_services statuses service
        local all_running=1
        running_services=$("$compose_function" ps --services --status running "${services[@]}")
        for service in "${services[@]}"; do
            case $'\n'"$running_services"$'\n' in
            *$'\n'"$service"$'\n'*) ;;
            *) all_running=0 ;;
            esac
        done
        failed_services=$("$compose_function" ps --services --status exited "${services[@]}")
        if [ -n "$failed_services" ]; then
            echo "stack service exited before becoming healthy: $failed_services" >&2
            "$compose_function" ps --all >&2
            return 1
        fi
        statuses=$("$compose_function" ps --format '{{.Status}}' "${services[@]}")
        if [ "$all_running" = "1" ] && ! printf '%s\n' "$statuses" | grep -Eqi 'starting|unhealthy'; then
            return 0
        fi
        sleep 2
    done
    echo "stack did not become healthy in time" >&2
    "$compose_function" ps --all >&2
    return 1
}

run_lnd_external() {
    require_lnd_image
    if [ "$NO_RESET" != "1" ]; then
        lnd_external_compose down -v >/dev/null 2>&1 || true
        wipe_host_state "$LND_EXTERNAL_DATA_DIR"
        lnd_external_compose up -d
        wait_for_healthy lnd_external_compose 180 lnd-bitcoin lnd-opener lnd-fundee
        sleep 15
    fi
    LND_EXTERNAL_E2E=1 pytest -c "${PROJECT_ROOT}/pytest.ini" \
        "${PROJECT_ROOT}/tests/e2e/test_lnd_external_channel_e2e.py" \
        -m lnd_external --fail-on-skip --no-cov -v --timeout=300
}

run_ring() {
    require_lnd_image
    build_ring_images
    if [ "$NO_RESET" != "1" ]; then
        ring_compose down -v >/dev/null 2>&1 || true
        wipe_host_state "$RING_E2E_DATA_DIR"
        mkdir -p "$RING_E2E_DATA_DIR"
        ring_compose up -d
        wait_for_healthy ring_compose 300 \
            ring-bitcoin ring-directory ring-orderbook ring-tor \
            ring-onion-maker1 ring-onion-maker2 ring-onion-maker3 ring-onion-taker \
            lnd-maker1 lnd-maker2 lnd-maker3 lnd-taker \
            ring-maker1 ring-maker2 ring-maker3
        sleep 30
    fi
    RING_E2E=1 pytest -c "${PROJECT_ROOT}/pytest.ini" \
        "${PROJECT_ROOT}/tests/e2e/test_cofunded_ring_e2e.py" \
        -m ring_e2e --fail-on-skip --no-cov -v --timeout=900
}

cleanup() {
    if [ "$KEEP" = "1" ]; then
        return 0
    fi
    if [ "$SUITE" = "ring" ] || [ "$SUITE" = "all" ]; then
        ring_compose down -v >/dev/null 2>&1 || true
        wipe_host_state "$RING_E2E_DATA_DIR" || true
    fi
    if [ "$SUITE" = "lnd-external" ] || [ "$SUITE" = "all" ]; then
        lnd_external_compose down -v >/dev/null 2>&1 || true
        wipe_host_state "$LND_EXTERNAL_DATA_DIR" || true
    fi
    return 0
}
trap cleanup EXIT

case "$SUITE" in
lnd-external) run_lnd_external ;;
ring) run_ring ;;
all)
    run_lnd_external
    run_ring
    ;;
esac
