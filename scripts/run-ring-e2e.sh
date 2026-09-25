#!/usr/bin/env bash
# Run a live channel-ring E2E suite against an isolated stack.
#
# Both profiles keep LND, Tor and wallet state in host directories while bitcoind
# keeps its chain in a Docker volume. Resetting only one of the two leaves LND
# ahead of a fresh chain, which surfaces as "Block height out of range" and fails
# every test for reasons that have nothing to do with the code under test. An
# explicit reset removes both the chain volume and root-owned host state.
#
# Both stacks run under their own Compose project name so a reset never touches
# another checkout's stack, a signet deployment, or the channel-buyout regtest
# project. Existing fixtures are preserved unless E2E_RESET=1 explicitly
# authorizes destroying the selected project and its host directory.
#
# Usage:
#   scripts/run-ring-e2e.sh [lnd-external|ring|ring-no-taker|all]   (default: all)
#
# Environment:
#   RING_E2E_KEEP=1     leave the stack running after successful tests
#   E2E_RESET=1         explicitly replace the selected project's retained state
#   RING_E2E_NO_RESET=1 is no longer safe for transactional tests; refuse replay
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
RESET=${E2E_RESET:-0}
RING_OWNED=0
LND_EXTERNAL_OWNED=0

LND_EXTERNAL_DATA_DIR=$(realpath -m "${LND_EXTERNAL_DATA_DIR:-${PROJECT_ROOT}/tmp/lnd-external}")
if [ "$SUITE" = ring-no-taker ]; then
    RING_E2E_DATA_DIR=$(realpath -m "${RING_E2E_DATA_DIR:-${PROJECT_ROOT}/tmp/ring-no-taker-e2e}")
    RING_E2E_PROJECT=${RING_E2E_PROJECT:-jm-ring-no-taker-e2e}
else
    RING_E2E_DATA_DIR=$(realpath -m "${RING_E2E_DATA_DIR:-${PROJECT_ROOT}/tmp/ring-e2e}")
    RING_E2E_PROJECT=${RING_E2E_PROJECT:-jm-ring-e2e}
fi
LND_EXTERNAL_PROJECT=${LND_EXTERNAL_PROJECT:-jm-lnd-external}
export LND_EXTERNAL_DATA_DIR RING_E2E_DATA_DIR RING_E2E_PROJECT LND_EXTERNAL_PROJECT

if [ "$SUITE" != "lnd-external" ] && [ "$SUITE" != "ring" ] &&
    [ "$SUITE" != "ring-no-taker" ] && [ "$SUITE" != "all" ]; then
    echo "usage: $0 [lnd-external|ring|ring-no-taker|all]" >&2
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

if [ "$LND_EXTERNAL_DATA_DIR" = "$RING_E2E_DATA_DIR" ] ||
    [ "$LND_EXTERNAL_PROJECT" = "$RING_E2E_PROJECT" ]; then
    echo "E2E profiles require distinct projects and data directories" >&2
    exit 2
fi
case "$LND_EXTERNAL_DATA_DIR/" in
"$RING_E2E_DATA_DIR/"*) echo "overlapping E2E data directories" >&2; exit 2 ;;
esac
case "$RING_E2E_DATA_DIR/" in
"$LND_EXTERNAL_DATA_DIR/"*) echo "overlapping E2E data directories" >&2; exit 2 ;;
esac
if [ "$RESET" != 0 ] && [ "$RESET" != 1 ]; then
    echo "E2E_RESET must be 0 or 1" >&2
    exit 2
fi
if [ "$NO_RESET" = 1 ]; then
    echo "RING_E2E_NO_RESET cannot establish that a retained signed round is safe to replay; use a new isolated project, or E2E_RESET=1 after reviewing its state" >&2
    exit 2
fi

lnd_external_compose() {
    local -a override=()
    if [ -n "${LND_EXTERNAL_IPAM_FILE:-}" ]; then
        override=(-f "$LND_EXTERNAL_IPAM_FILE")
    fi
    docker compose \
        -p "$LND_EXTERNAL_PROJECT" \
        -f "${PROJECT_ROOT}/jmswap/docker-compose.lnd-external.yml" \
        "${override[@]}" \
        --profile lnd_external "$@"
}

ring_compose() {
    local -a override=()
    if [ -n "${RING_E2E_IPAM_FILE:-}" ]; then
        override=(-f "$RING_E2E_IPAM_FILE")
    fi
    docker compose \
        -p "$RING_E2E_PROJECT" \
        -f "${PROJECT_ROOT}/jmswap/docker-compose.ring-e2e.yml" \
        -f "${PROJECT_ROOT}/jmswap/docker-compose.ring-e2e.host.yml" \
        "${override[@]}" \
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

# An absent host directory does not prove that a Docker volume, network or
# stopped container from an interrupted run is absent. Inspection errors are
# unknown state, never permission to start over.
target_has_state() {
    local data_dir=$1 project=$2 containers volumes networks
    containers=$(docker ps -aq --filter "label=com.docker.compose.project=$project") || return 2
    volumes=$(docker volume ls -q --filter "label=com.docker.compose.project=$project") || return 2
    networks=$(docker network ls -q --filter "label=com.docker.compose.project=$project") || return 2
    if [ -e "$data_dir" ] || [ -n "$containers$volumes$networks" ]; then
        return 0
    fi
    return 1
}

preflight_target() {
    local data_dir=$1 project=$2 state=0
    target_has_state "$data_dir" "$project" || state=$?
    if [ "$state" -eq 2 ]; then
        echo "cannot inspect existing E2E state for $project; refusing to proceed" >&2
        return 1
    fi
    if [ "$state" -eq 0 ] && [ "$RESET" != 1 ]; then
        echo "retained E2E state for $project at $data_dir; preserve it or explicitly use E2E_RESET=1" >&2
        return 1
    fi
}

prepare_target() {
    local data_dir=$1 project=$2 compose_function=$3
    if [ "$RESET" = 1 ]; then
        "$compose_function" down -v
        wipe_host_state "$data_dir"
    fi
    mkdir -p "$data_dir"
    python - "$data_dir" "$project" <<'PY'
import json
import pathlib
import sys

path, project = sys.argv[1:]
with (pathlib.Path(path) / ".e2e-run.json").open("x", encoding="utf-8") as marker:
    json.dump({"schema_version": 1, "project": project, "data_dir": path,
               "disposable_only_after_success": True}, marker)
PY
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

# Compose's LND healthcheck proves only that getinfo responds. A freshly mined
# regtest block may still leave the chain wallet unsynced, so enrollment and
# maker startup must wait for LND's actual funding prerequisite.
wait_for_ring_lnd_sync() {
    local deadline=$((SECONDS + 180))
    local node unsynced
    while [ "$SECONDS" -lt "$deadline" ]; do
        unsynced=""
        for node in "$@"; do
            if ! ring_compose exec -T "lnd-$node" lncli --network=regtest \
                --tlscertpath=/root/.lnd/tls.cert --macaroonpath=/root/.lnd/admin.macaroon \
                getinfo 2>/dev/null | python -c 'import json,sys
try:
    info = json.load(sys.stdin)
    sys.exit(0 if info.get("synced_to_chain") is True and info.get("wallet_synced") is True else 1)
except (ValueError, KeyError):
    sys.exit(1)' >/dev/null; then
                unsynced="$unsynced lnd-$node"
            fi
        done
        if [ -z "$unsynced" ]; then
            return 0
        fi
        sleep 2
    done
    echo "ring LND chain wallets did not sync in time:$unsynced" >&2
    return 1
}

run_lnd_external() {
    require_lnd_image
    prepare_target "$LND_EXTERNAL_DATA_DIR" "$LND_EXTERNAL_PROJECT" lnd_external_compose
    LND_EXTERNAL_OWNED=1
    lnd_external_compose up -d
    wait_for_healthy lnd_external_compose 180 lnd-bitcoin lnd-opener lnd-fundee
    sleep 15
    LND_EXTERNAL_E2E=1 pytest -c "${PROJECT_ROOT}/pytest.ini" \
        "${PROJECT_ROOT}/tests/e2e/test_lnd_external_channel_e2e.py" \
        -m lnd_external --fail-on-skip --no-cov -v --timeout=300
}

run_ring() {
    require_lnd_image
    build_ring_images
    prepare_target "$RING_E2E_DATA_DIR" "$RING_E2E_PROJECT" ring_compose
    RING_OWNED=1
    local -a nodes=(maker1 maker2 maker3)
    if [ "$SUITE" = ring ]; then
        nodes+=(maker4 taker)
    fi
    local -a services=()
    local node maker node_id
    for node in "${nodes[@]}"; do
        services+=("lnd-$node" "ring-onion-$node")
    done
    ring_compose up -d ring-bitcoin ring-directory ring-orderbook ring-wallet-funder \
        "${services[@]}"
    wait_for_healthy ring_compose 300 \
        ring-bitcoin ring-directory ring-orderbook ring-tor \
        "${services[@]}"
    wait_for_ring_lnd_sync "${nodes[@]}"
    # Tor resolves HiddenServicePort forwarder hostnames when it loads its
    # configuration. Compose starts Tor before the LND forwarders, so it can
    # retain a stale target even though the onion descriptor is reachable.
    # Recreate only Tor after all forwarders exist, preserving its mounted keys.
    ring_compose up -d --no-deps --force-recreate ring-tor
    wait_for_healthy ring_compose 90 ring-tor
    # Explicit fresh-fixture enrollment, never an implicit startup migration.
    for maker in 1 2 3; do
        node_id=$(ring_compose exec -T "lnd-maker$maker" lncli --network=regtest \
            --tlscertpath=/root/.lnd/tls.cert --macaroonpath=/root/.lnd/admin.macaroon \
            getinfo | python -c 'import json,sys; print(json.load(sys.stdin)["identity_pubkey"])')
        ring_compose run --rm --no-deps -e "RING_NODE_ENROLLMENT_PUBKEY=$node_id" "ring-maker$maker"
    done
    local -a makers=(ring-maker1 ring-maker2 ring-maker3)
    if [ "$SUITE" = ring ]; then
        makers+=(ring-maker4)
    fi
    ring_compose up -d --no-deps "${makers[@]}"
    wait_for_healthy ring_compose 300 "${makers[@]}"
    # New Tor state publishes new onion descriptors. A healthy container is not
    # proof that the descriptors have propagated yet.
    sleep 120
    wait_for_ring_lnd_sync "${nodes[@]}"
    if [ "$SUITE" = ring-no-taker ]; then
        RING_E2E=1 pytest -c "${PROJECT_ROOT}/pytest.ini" \
            "${PROJECT_ROOT}/tests/e2e/test_ring_without_taker_lnd_e2e.py" \
            -m ring_e2e --fail-on-skip --no-cov -v --timeout=1800
    else
        RING_E2E=1 pytest -c "${PROJECT_ROOT}/pytest.ini" \
            "${PROJECT_ROOT}/tests/e2e/test_direct_heartbeat_e2e.py" \
            "${PROJECT_ROOT}/tests/e2e/test_cofunded_ring_e2e.py" \
            -m ring_e2e --fail-on-skip --no-cov -v --timeout=3600
    fi
}

cleanup_success() {
    if [ "$KEEP" = "1" ]; then
        return 0
    fi
    if [ "$RING_OWNED" = 1 ]; then
        ring_compose down -v || return 1
        wipe_host_state "$RING_E2E_DATA_DIR" || return 1
    fi
    if [ "$LND_EXTERNAL_OWNED" = 1 ]; then
        lnd_external_compose down -v || return 1
        wipe_host_state "$LND_EXTERNAL_DATA_DIR" || return 1
    fi
}

finish() {
    local result=$1
    trap - EXIT
    if [ "$result" -ne 0 ]; then
        echo "E2E failed; preserving project, chain volumes and host journals for review" >&2
    elif ! cleanup_success; then
        result=1
        echo "E2E cleanup incomplete; retaining remaining state for review" >&2
    fi
    exit "$result"
}
trap 'finish $?' EXIT

# Inspect every selected target before touching either one, so `all` cannot
# create or reset one fixture and only then discover retained state in the other.
case "$SUITE" in
ring|ring-no-taker|all) preflight_target "$RING_E2E_DATA_DIR" "$RING_E2E_PROJECT" ;;
esac
case "$SUITE" in
lnd-external|all) preflight_target "$LND_EXTERNAL_DATA_DIR" "$LND_EXTERNAL_PROJECT" ;;
esac

case "$SUITE" in
lnd-external) run_lnd_external ;;
    ring|ring-no-taker) run_ring ;;
all)
    run_lnd_external
    run_ring
    ;;
esac
