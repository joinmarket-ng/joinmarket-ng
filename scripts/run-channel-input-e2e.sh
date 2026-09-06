#!/usr/bin/env bash
# Run the isolated live three-maker channel-input v1 E2E suite.
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$PROJECT_ROOT"

PROJECT_PYTHONPATH=$(printf '%s:' \
    "$PROJECT_ROOT/jmcore/src" \
    "$PROJECT_ROOT/jmwallet/src" \
    "$PROJECT_ROOT/directory_server/src" \
    "$PROJECT_ROOT/jmswap/src" \
    "$PROJECT_ROOT/orderbook_watcher/src" \
    "$PROJECT_ROOT/maker/src" \
    "$PROJECT_ROOT/taker/src")
export PYTHONPATH="${PROJECT_PYTHONPATH%:}${PYTHONPATH:+:$PYTHONPATH}"

CHANNEL_INPUT_E2E_DATA_DIR=$(realpath -m "${CHANNEL_INPUT_E2E_DATA_DIR:-$PROJECT_ROOT/tmp/channel-input-e2e}")
CHANNEL_INPUT_E2E_PROJECT=${CHANNEL_INPUT_E2E_PROJECT:-jm-channel-input-e2e}
CHANNEL_INPUT_E2E_KEEP=${CHANNEL_INPUT_E2E_KEEP:-0}
CHANNEL_INPUT_E2E_NO_BUILD=${CHANNEL_INPUT_E2E_NO_BUILD:-0}
CHANNEL_INPUT_E2E_LND_IMAGE=${CHANNEL_INPUT_E2E_LND_IMAGE:-lnd-channelescrow:v0.21.1-beta-local}
HOST_BITCOINTX_DIR=$(realpath -m "${CHANNEL_INPUT_E2E_BITCOINTX_DIR:-$PROJECT_ROOT/../../../python-bitcointx}")
export CHANNEL_INPUT_E2E_DATA_DIR CHANNEL_INPUT_E2E_PROJECT CHANNEL_INPUT_E2E_LND_IMAGE
export CHANNEL_INPUT_E2E_BITCOINTX_SOURCE="$HOST_BITCOINTX_DIR"
export RING_E2E_DATA_DIR="$CHANNEL_INPUT_E2E_DATA_DIR"

for path in "$PROJECT_ROOT/tmp" "$(dirname "$CHANNEL_INPUT_E2E_DATA_DIR")"; do
    if [ ! -d "$path" ]; then
        echo "required E2E parent directory does not exist: $path" >&2
        exit 2
    fi
done
case "$CHANNEL_INPUT_E2E_DATA_DIR" in
"$PROJECT_ROOT"/tmp/*) ;;
*)
    echo "refusing to wipe E2E data outside $PROJECT_ROOT/tmp: $CHANNEL_INPUT_E2E_DATA_DIR" >&2
    exit 2
    ;;
esac

compose() {
    docker compose -p "$CHANNEL_INPUT_E2E_PROJECT" \
        -f "$PROJECT_ROOT/jmswap/docker-compose.ring-e2e.yml" \
        -f "$PROJECT_ROOT/jmswap/docker-compose.channel-input-e2e.yml" \
        -f "$PROJECT_ROOT/jmswap/docker-compose.channel-input-e2e.host.yml" \
        --profile ring-e2e "$@"
}

wipe_host_state() {
    local target=$1
    [ -e "$target" ] || return 0
    docker run --rm -v "$(dirname "$target"):/wipe" alpine:3.20 \
        sh -c 'rm -rf -- "/wipe/$1"' sh "$(basename "$target")"
}

require_free_port() {
    local port=$1
    if ss -ltnH "sport = :$port" | rg -q .; then
        echo "required channel-input E2E port is already in use: $port" >&2
        exit 2
    fi
}

wait_for_healthy() {
    local deadline=$((SECONDS + 300))
    local service
    local services=(
        ring-bitcoin ring-directory ring-orderbook ring-tor
        ring-onion-maker1 ring-onion-maker2 ring-onion-maker3 ring-onion-taker
        lnd-maker1 lnd-maker2 lnd-maker3 lnd-taker
        ring-maker1 ring-maker2 ring-maker3
    )
    while [ "$SECONDS" -lt "$deadline" ]; do
        local running failed statuses all_running=1
        running=$(compose ps --services --status running)
        for service in "${services[@]}"; do
            case $'\n'"$running"$'\n' in
            *$'\n'"$service"$'\n'*) ;;
            *) all_running=0 ;;
            esac
        done
        failed=$(compose ps --services --status exited "${services[@]}")
        if [ -n "$failed" ]; then
            echo "stack service exited before becoming healthy: $failed" >&2
            compose ps --all >&2
            return 1
        fi
        statuses=$(compose ps --format '{{.Status}}')
        if [ "$all_running" = "1" ] && ! printf '%s\n' "$statuses" | rg -qi 'starting|unhealthy'; then
            return 0
        fi
        sleep 2
    done
    echo "stack did not become healthy in time" >&2
    compose ps --all >&2
    return 1
}

ensure_host_bitcointx() {
    if [ ! -f "$HOST_BITCOINTX_DIR/bitcointx/core/musig.py" ]; then
        echo "MuSig-enabled local python-bitcointx checkout is unavailable: $HOST_BITCOINTX_DIR" >&2
        exit 2
    fi
    PYTHONPATH="$HOST_BITCOINTX_DIR${PYTHONPATH:+:$PYTHONPATH}" \
        python -c 'import bitcointx.core.musig' >/dev/null
}

build_images() {
    local directory_image=${DIRECTORY_SERVER_IMAGE:-channel-input-e2e-directory:latest}
    local orderbook_image=${ORDERBOOK_WATCHER_IMAGE:-channel-input-e2e-orderbook:latest}
    local maker_image=${MAKER_IMAGE:-channel-input-e2e-maker:latest}
    export DIRECTORY_SERVER_IMAGE="$directory_image" ORDERBOOK_WATCHER_IMAGE="$orderbook_image"
    export MAKER_IMAGE="$maker_image"
    docker image inspect "$CHANNEL_INPUT_E2E_LND_IMAGE" >/dev/null
    if [ "$CHANNEL_INPUT_E2E_NO_BUILD" = "1" ]; then
        for image in "$directory_image" "$orderbook_image" "$maker_image"; do
            docker image inspect "$image" >/dev/null
        done
        return 0
    fi
    docker build -f "$PROJECT_ROOT/directory_server/Dockerfile" --target production \
        -t "$directory_image" "$PROJECT_ROOT"
    docker build -f "$PROJECT_ROOT/orderbook_watcher/Dockerfile" \
        -t "$orderbook_image" "$PROJECT_ROOT"
    docker build -f "$PROJECT_ROOT/maker/Dockerfile" -t "$maker_image" "$PROJECT_ROOT"
}

cleanup() {
    local status=$?
    if [ "$status" != "0" ]; then
        compose logs --no-color --tail 160 ring-maker1 ring-maker2 ring-maker3 ring-directory >&2 || true
    fi
    if [ "$CHANNEL_INPUT_E2E_KEEP" = "1" ]; then
        return 0
    fi
    compose down -v >/dev/null 2>&1 || true
    wipe_host_state "$CHANNEL_INPUT_E2E_DATA_DIR" || true
}
trap cleanup EXIT

for port in "${CHANNEL_INPUT_E2E_BITCOIN_PORT:-30443}" \
    "${CHANNEL_INPUT_E2E_DIRECTORY_PORT:-30322}" \
    "${CHANNEL_INPUT_E2E_ORDERBOOK_PORT:-30880}" \
    "${CHANNEL_INPUT_E2E_TOR_SOCKS_PORT:-30950}" \
    "${CHANNEL_INPUT_E2E_MAKER1_GRPC_PORT:-31009}" \
    "${CHANNEL_INPUT_E2E_MAKER2_GRPC_PORT:-32009}" \
    "${CHANNEL_INPUT_E2E_MAKER3_GRPC_PORT:-31309}" \
    "${CHANNEL_INPUT_E2E_TAKER_GRPC_PORT:-30009}"; do
    require_free_port "$port"
done
ensure_host_bitcointx
export PYTHONPATH="$HOST_BITCOINTX_DIR:$PYTHONPATH"
build_images
compose down -v >/dev/null 2>&1 || true
wipe_host_state "$CHANNEL_INPUT_E2E_DATA_DIR"
mkdir -p "$CHANNEL_INPUT_E2E_DATA_DIR"
compose up -d
wait_for_healthy
for node in lnd-maker1 lnd-maker2 lnd-maker3 lnd-taker; do
    # Keep LND's database directories private; share only the dedicated credential.
    compose exec -T "$node" sh -c \
        'cp /root/.lnd/data/chain/bitcoin/regtest/channelescrow.macaroon /root/.lnd/channelescrow.macaroon && chmod 640 /root/.lnd/channelescrow.macaroon'
done
sleep 30
CHANNEL_INPUT_E2E=1 pytest -c "$PROJECT_ROOT/pytest.ini" \
    "$PROJECT_ROOT/tests/e2e/test_channel_input_e2e.py" \
    -m 'docker and e2e' --fail-on-skip --no-cov -v --timeout=1200
