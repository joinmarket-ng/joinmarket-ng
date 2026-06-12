#!/bin/bash
set -euo pipefail

# JoinMarket Parallel Test Suite Runner
#
# Runs all test suites in parallel using Docker Compose project isolation.
# Each Docker-dependent test suite gets its own Compose project with unique
# container names, port mappings, networks, and volumes.
#
# This mirrors the GitHub Actions CI workflow where each test job runs on
# a separate VM, but achieves isolation locally via Docker Compose projects.
#
# Usage:
#   ./scripts/run_parallel_tests.sh              # Run all suites in parallel
#   ./scripts/run_parallel_tests.sh --cleanup    # Clean up all parallel projects
#   ./scripts/run_parallel_tests.sh --suite e2e  # Run a single suite
#   ./scripts/run_parallel_tests.sh --help       # Show help
#
# Prerequisites:
#   - Docker and Docker Compose
#   - Python 3.11+ with project dependencies installed
#   - Node.js (optional, for Playwright tests)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log_info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
log_success() { echo -e "${GREEN}[OK]${NC} $*"; }
log_warning() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $*"; }
log_suite()   { echo -e "${CYAN}[SUITE]${NC} $*"; }

# Test instance id. Lets multiple concurrent invocations (for example from two
# different worktrees/branches) coexist by giving each its own Compose project
# names, container-name prefixes, host port band, and log/override directory.
# Override with --instance N or JM_TEST_INSTANCE; defaults to 0.
INSTANCE="${JM_TEST_INSTANCE:-0}"
PROJECT_PREFIX="jmpt-i${INSTANCE}"
CONTAINER_PREFIX="jm-i${INSTANCE}"

# Skip the shared image build (set by --no-build / SKIP_BUILD=1). Useful for a
# second concurrent instance that reuses images already built by another run.
SKIP_BUILD="${SKIP_BUILD:-0}"

# Directory for per-suite logs and override files (namespaced per instance).
PARALLEL_DIR="${PROJECT_ROOT}/tmp/parallel-tests/i${INSTANCE}"
mkdir -p "$PARALLEL_DIR"

# Re-derive the instance-scoped globals after argument parsing may have changed
# INSTANCE. Idempotent; safe to call multiple times.
apply_instance() {
    PROJECT_PREFIX="jmpt-i${INSTANCE}"
    CONTAINER_PREFIX="jm-i${INSTANCE}"
    PARALLEL_DIR="${PROJECT_ROOT}/tmp/parallel-tests/i${INSTANCE}"
    mkdir -p "$PARALLEL_DIR"
}

# Track background PIDs and results
declare -A SUITE_PIDS
declare -A SUITE_RESULTS
declare -A SUITE_START_TIMES

# Maximum number of suites to run concurrently. 0 disables the limit.
# Defaults to half the visible CPU cores (rounded down, minimum 1) so that a
# 16-core host caps at 8 concurrent docker stacks. Override with
# --max-concurrent N or the MAX_CONCURRENT env var.
default_cpu_cap() {
    local cores
    if cores=$(nproc 2>/dev/null) && [ -n "$cores" ] && [ "$cores" -gt 0 ]; then
        local cap=$((cores / 2))
        [ "$cap" -lt 1 ] && cap=1
        echo "$cap"
    else
        echo 4
    fi
}
MAX_CONCURRENT="${MAX_CONCURRENT:-$(default_cpu_cap)}"

# =============================================================================
# Host port allocation
#
# Container-internal ports are unchanged (bitcoind still listens on 18443 etc.).
# Only the HOST side of each published port is remapped so that every
# (instance, suite, service) tuple gets a unique host port. Because the host
# port number is fully parameterized everywhere (override file + pytest env),
# its absolute value is arbitrary as long as it is unique.
#
# Layout: each instance owns a contiguous band of INSTANCE_STRIDE ports starting
# at PORT_BAND_BASE; within a band each suite owns SUITE_STRIDE ports; within a
# suite each service occupies one fixed slot. This keeps every concurrent run on
# disjoint host ports and scales to ~20 instances under 65535.
# =============================================================================
PORT_BAND_BASE="${JM_TEST_PORT_BASE:-20000}"
INSTANCE_STRIDE=2000   # host ports reserved per instance
SUITE_STRIDE=64        # host ports reserved per suite within an instance

# Suite slot index (also the canonical list of Docker-backed suites).
declare -A SUITE_SLOT=(
    [e2e]=0
    [playwright]=1
    [jmwallet]=2
    [reference-interop]=3
    [reference-legacy]=4
    [neutrino-functional]=5
    [neutrino-coinjoin]=6
    [neutrino-reference]=7
    [reference-maker]=8
    [tumbler]=9
)

# Service slot index within a suite's port window.
declare -A SVC_SLOT=(
    [btc_rpc]=0
    [btc_p2p]=1
    [btc_jam_rpc]=2
    [btc_jam_p2p]=3
    [dir]=4
    [dir2]=5
    [obwatch]=6
    [neutrino]=7
    [walletd]=8
    [jam_pw]=9
    [tor_socks]=10
    [tor_ctrl]=11
)

# Compute the host port for a given suite/service in the current instance.
host_port() {
    local suite=$1 svc=$2
    local s=${SUITE_SLOT[$suite]} v=${SVC_SLOT[$svc]}
    echo $((PORT_BAND_BASE + INSTANCE * INSTANCE_STRIDE + s * SUITE_STRIDE + v))
}

# =============================================================================
# Generate Docker Compose override file for a suite
# =============================================================================
generate_override() {
    local suite=$1
    local prefix="${CONTAINER_PREFIX}-${suite}"
    local override_file="${PARALLEL_DIR}/docker-compose.${suite}.override.yml"

    # Calculate host ports for this instance/suite.
    local btc_rpc=$(host_port "$suite" btc_rpc)
    local btc_p2p=$(host_port "$suite" btc_p2p)
    local btc_jam_rpc=$(host_port "$suite" btc_jam_rpc)
    local btc_jam_p2p=$(host_port "$suite" btc_jam_p2p)
    local dir_port=$(host_port "$suite" dir)
    local dir2_port=$(host_port "$suite" dir2)
    local obwatch_port=$(host_port "$suite" obwatch)
    local neutrino_port=$(host_port "$suite" neutrino)
    local walletd_port=$(host_port "$suite" walletd)
    local jam_pw_port=$(host_port "$suite" jam_pw)
    local tor_socks=$(host_port "$suite" tor_socks)
    local tor_ctrl=$(host_port "$suite" tor_ctrl)
    local shared_dir="${PARALLEL_DIR}/shared/${suite}"

    mkdir -p "$shared_dir"

    # NOTE: Docker Compose merges port lists by appending, so an override file
    # that just lists ports will ADD to the base ports instead of replacing them.
    # We use the !override YAML tag (Compose v2.24.6+ / v5+) to fully replace
    # each service's port list with the suite-specific one, preventing the base
    # ports from being bound and causing collisions between parallel suites.
    #
    # We also keep legacy jm-* network aliases for service-to-service DNS
    # compatibility (for example: jm-bitcoin, jm-directory, jm-tor). Many
    # container env vars and torrc still reference these names.
    cat > "$override_file" <<YAML
# Auto-generated override for parallel suite: ${suite}
# Remaps host ports and container names for isolation.
# Uses !override to REPLACE (not append to) port lists from the base file.
services:
  bitcoin:
    container_name: ${prefix}-bitcoin
    networks:
      jm-network:
        aliases:
          - jm-bitcoin
    volumes: !override
      - bitcoin-data:/bitcoin/.bitcoin
      - "${shared_dir}:/shared"
    ports: !override
      - "${btc_rpc}:18443"
      - "${btc_p2p}:18444"

  miner:
    container_name: ${prefix}-miner
    networks:
      jm-network:
        aliases:
          - jm-miner

  directory:
    container_name: ${prefix}-directory
    networks:
      jm-network:
        aliases:
          - jm-directory
    ports: !override
      - "${dir_port}:5222"

  directory2:
    container_name: ${prefix}-directory2
    networks:
      jm-network:
        aliases:
          - jm-directory2
    ports: !override
      - "${dir2_port}:5223"

  orderbook-watcher:
    container_name: ${prefix}-orderbook-watcher
    networks:
      jm-network:
        aliases:
          - jm-orderbook-watcher
    ports: !override
      - "${obwatch_port}:8000"

  jmwalletd:
    container_name: ${prefix}-walletd
    networks:
      jm-network:
        aliases:
          - jm-walletd
    volumes: !override
      - jmwalletd-data:/root/.joinmarket-ng
      - "${shared_dir}:/shared:ro"
    ports: !override
      - "${walletd_port}:28183"

  jam-playwright:
    container_name: ${prefix}-jam-playwright
    networks:
      jm-network:
        aliases:
          - jm-jam-playwright
    volumes: !override
      - jam-playwright-data:/root/.joinmarket-ng
      - "${shared_dir}:/shared:ro"
    ports: !override
      - "${jam_pw_port}:28183"

  bitcoin-jam:
    container_name: ${prefix}-bitcoin-jam
    networks:
      jm-network:
        aliases:
          - jm-bitcoin-jam
    ports: !override
      - "${btc_jam_rpc}:18445"
      - "${btc_jam_p2p}:18446"

  miner-jam:
    container_name: ${prefix}-miner-jam
    networks:
      jm-network:
        aliases:
          - jm-miner-jam

  tor-init:
    container_name: ${prefix}-tor-init
    networks:
      jm-network:
        aliases:
          - jm-tor-init

  tor:
    container_name: ${prefix}-tor
    networks:
      jm-network:
        aliases:
          - jm-tor
    ports: !override
      - "${tor_socks}:9050"
      - "${tor_ctrl}:9051"

  jam-config-init:
    container_name: ${prefix}-jam-config-init
    networks:
      jm-network:
        aliases:
          - jm-jam-config-init

  jam:
    container_name: ${prefix}-jam
    networks:
      jm-network:
        aliases:
          - jm-jam

  jam-maker1:
    container_name: ${prefix}-jam-maker1
    networks:
      jm-network:
        aliases:
          - jm-jam-maker1

  jam-maker2:
    container_name: ${prefix}-jam-maker2
    networks:
      jm-network:
        aliases:
          - jm-jam-maker2

  neutrino:
    container_name: ${prefix}-neutrino
    networks:
      jm-network:
        aliases:
          - jm-neutrino
    ports: !override
      - "${neutrino_port}:8334"

  wallet-funder:
    container_name: ${prefix}-wallet-funder
    networks:
      jm-network:
        aliases:
          - jm-wallet-funder

  maker1:
    container_name: ${prefix}-maker1
    networks:
      jm-network:
        aliases:
          - jm-maker1
    volumes: !override
      - maker1-data:/home/jm/.joinmarket-ng
      - "${shared_dir}:/shared:ro"

  maker2:
    container_name: ${prefix}-maker2
    networks:
      jm-network:
        aliases:
          - jm-maker2

  maker3:
    container_name: ${prefix}-maker3
    networks:
      jm-network:
        aliases:
          - jm-maker3

  maker4:
    container_name: ${prefix}-maker4
    networks:
      jm-network:
        aliases:
          - jm-maker4
    volumes: !override
      - maker4-data:/home/jm/.joinmarket-ng
      - "${shared_dir}:/shared:ro"

  maker5:
    container_name: ${prefix}-maker5
    networks:
      jm-network:
        aliases:
          - jm-maker5
    volumes: !override
      - maker5-data:/home/jm/.joinmarket-ng
      - "${shared_dir}:/shared:ro"

  maker-neutrino:
    container_name: ${prefix}-maker-neutrino
    networks:
      jm-network:
        aliases:
          - jm-maker-neutrino

  maker:
    container_name: ${prefix}-maker
    networks:
      jm-network:
        aliases:
          - jm-maker

  taker:
    container_name: ${prefix}-taker
    networks:
      jm-network:
        aliases:
          - jm-taker

  taker-reference:
    container_name: ${prefix}-taker-reference
    networks:
      jm-network:
        aliases:
          - jm-taker-reference

  taker-neutrino:
    container_name: ${prefix}-taker-neutrino
    networks:
      jm-network:
        aliases:
          - jm-taker-neutrino
YAML

    echo "$override_file"
}

# =============================================================================
# Compose helper: runs docker compose with project isolation
# =============================================================================
compose_cmd() {
    local suite=$1
    shift
    local project="${PROJECT_PREFIX}-${suite}"
    local override_file="${PARALLEL_DIR}/docker-compose.${suite}.override.yml"

    docker compose \
        -p "$project" \
        -f "${PROJECT_ROOT}/docker-compose.yml" \
        -f "$override_file" \
        "$@"
}

# =============================================================================
# Wait for Bitcoin RPC readiness on a specific port
#
# Verifies BOTH:
#   1. The bitcoind process inside the container is responsive (via bitcoin-cli)
#   2. The host-mapped RPC port accepts a real RPC over HTTP
#
# (1) alone is insufficient because tests connect via the host port, and there
# is a brief window between bitcoind accepting connections inside the container
# and Docker fully wiring up the host port forward — under parallel load this
# race can cause the first few RPC calls from pytest to fail.
# =============================================================================
wait_for_bitcoin_rpc() {
    local suite=$1
    local port=$2
    local max_attempts=${3:-60}
    local prefix="${CONTAINER_PREFIX}-${suite}"

    # Step 1: bitcoind responsive inside the container
    local internal_ready=0
    for i in $(seq 1 $max_attempts); do
        if compose_cmd "$suite" exec -T bitcoin \
            bitcoin-cli -chain=regtest -rpcport=18443 \
            -rpcuser=test -rpcpassword=test getblockchaininfo >/dev/null 2>&1; then
            internal_ready=1
            break
        fi
        sleep 2
    done
    if [[ $internal_ready -ne 1 ]]; then
        return 1
    fi

    # Step 2: host port reachable AND HTTP RPC responsive on host
    for i in $(seq 1 $max_attempts); do
        if curl -fsS --max-time 2 \
            --user test:test \
            -H 'content-type: application/json' \
            --data '{"jsonrpc":"1.0","id":"wait","method":"getblockchaininfo","params":[]}' \
            "http://127.0.0.1:${port}" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    return 1
}

# =============================================================================
# Wait for a TCP port to be open on localhost
# =============================================================================
wait_for_port() {
    local port=$1
    local label=${2:-"service"}
    local max_attempts=${3:-30}

    for i in $(seq 1 $max_attempts); do
        if nc -z 127.0.0.1 "$port" 2>/dev/null; then
            return 0
        fi
        sleep 2
    done
    log_error "$label not ready on port $port after $max_attempts attempts"
    return 1
}

# =============================================================================
# Wait for wallet-funder to complete
# =============================================================================
wait_for_wallet_funder() {
    local suite=$1
    for i in $(seq 1 60); do
        local container_id
        container_id=$(compose_cmd "$suite" ps -q wallet-funder 2>/dev/null | tail -n1 || true)
        if [ -n "${container_id}" ]; then
            local state
            state=$(docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' "${container_id}" 2>/dev/null | head -n1 || true)
            case "${state}" in
                "exited 0")
                    return 0
                    ;;
                exited\ *|dead\ *)
                    log_error "[$suite] wallet-funder failed (${state})"
                    compose_cmd "$suite" logs --tail=120 wallet-funder >&2 || true
                    return 1
                    ;;
            esac
        fi
        sleep 5
    done
    log_error "[$suite] wallet-funder did not complete successfully in time"
    compose_cmd "$suite" logs --tail=120 wallet-funder >&2 || true
    return 1
}

# =============================================================================
# Wait for Tor hidden service
# =============================================================================
wait_for_tor() {
    local suite=$1
    for i in $(seq 1 90); do
        if compose_cmd "$suite" exec -T tor \
            cat /var/lib/tor/directory/hostname 2>/dev/null | grep -q ".onion"; then
            return 0
        fi
        sleep 2
    done
    return 1
}

# =============================================================================
# Wait for JAM web interface
# =============================================================================
wait_for_jam() {
    local suite=$1
    for i in $(seq 1 30); do
        if compose_cmd "$suite" exec -T jam \
            sh -c "timeout 5 bash -c '</dev/tcp/127.0.0.1/80'" 2>/dev/null; then
            return 0
        fi
        sleep 5
    done
    return 1
}

# =============================================================================
# Wait for JAM makers
# =============================================================================
wait_for_jam_makers() {
    local suite=$1
    for i in $(seq 1 30); do
        if compose_cmd "$suite" exec -T jam-maker1 \
            sh -c "timeout 5 bash -c '</dev/tcp/127.0.0.1/80'" 2>/dev/null; then
            break
        fi
        sleep 5
    done
    sleep 30
}

# =============================================================================
# Wait for Neutrino sync
# =============================================================================
wait_for_neutrino() {
    local suite=$1
    local port=$2
    local prefix="${CONTAINER_PREFIX}-${suite}"

    local token
    token=$(docker exec "${prefix}-neutrino" cat /data/neutrino/auth_token 2>/dev/null || true)

    for i in $(seq 1 120); do
        local height=0
        if [ -n "$token" ]; then
            height=$(curl -sk -H "Authorization: Bearer $token" \
                "https://127.0.0.1:${port}/v1/status" 2>/dev/null | \
                python3 -c 'import json,sys; print(int(json.load(sys.stdin).get("block_height", 0)))' 2>/dev/null || echo 0)
        else
            height=$(curl -sf "http://127.0.0.1:${port}/v1/status" 2>/dev/null | \
                python3 -c 'import json,sys; print(int(json.load(sys.stdin).get("block_height", 0)))' 2>/dev/null || echo 0)
        fi
        if [ "$height" -gt 0 ]; then
            return 0
        fi
        sleep 2
    done
    return 1
}

# =============================================================================
# Restart makers (clear commitment blacklists)
# =============================================================================
restart_makers() {
    local suite=$1
    local prefix="${CONTAINER_PREFIX}-${suite}"
    for maker in "${prefix}-maker1" "${prefix}-maker2" "${prefix}-maker3" "${prefix}-maker4" "${prefix}-maker5" "${prefix}-maker-neutrino"; do
        docker exec "$maker" sh -c \
            "rm -rf /home/jm/.joinmarket-ng/cmtdata/commitmentlist" 2>/dev/null || true
    done
    compose_cmd "$suite" restart maker1 maker2 maker3 maker4 maker5 maker-neutrino 2>/dev/null || true
    sleep 20
}

# =============================================================================
# Cleanup a single suite
# =============================================================================
cleanup_suite() {
    local suite=$1
    log_info "Cleaning up suite: $suite"
    compose_cmd "$suite" --profile e2e --profile reference --profile neutrino --profile reference-maker down -v 2>/dev/null || true
    compose_cmd "$suite" down --remove-orphans -v 2>/dev/null || true
    rm -rf "${PARALLEL_DIR}/shared/${suite}" 2>/dev/null || true
}

# =============================================================================
# Cleanup all suites
# =============================================================================
cleanup_all() {
    log_info "Cleaning up parallel test suites for instance ${INSTANCE}..."
    for suite in "${!SUITE_SLOT[@]}"; do
        cleanup_suite "$suite" 2>/dev/null &
    done
    wait

    # Also clean up this instance's default project (legacy non-isolated runs).
    # Note: we intentionally do NOT run a global `docker volume prune` here, as
    # that would delete volumes belonging to other concurrently-running
    # instances. Each project's volumes are already removed via `down -v`.
    docker compose -p "${PROJECT_PREFIX}-default" --profile e2e --profile reference \
        --profile neutrino --profile reference-maker down -v 2>/dev/null || true
    docker compose -p "${PROJECT_PREFIX}-default" down --remove-orphans -v 2>/dev/null || true

    log_success "Parallel suites cleaned up (instance ${INSTANCE})"
}

# =============================================================================
# Ensure reference implementation is available
# =============================================================================
setup_reference_implementation() {
    if [ -d "$PROJECT_ROOT/joinmarket-clientserver/src/jmclient" ]; then
        log_info "Reference implementation already present"
    else
        log_info "Cloning reference implementation..."
        git clone --depth 1 https://github.com/JoinMarket-Org/joinmarket-clientserver.git \
            "$PROJECT_ROOT/joinmarket-clientserver"
    fi

    pip install -q \
        chromalog==1.0.5 \
        service-identity==21.1.0 \
        twisted==24.7.0 \
        txtorcon==23.11.0 \
        python-bitcointx==1.1.5 \
        argon2_cffi==21.3.0 \
        autobahn==20.12.3 \
        fastbencode==0.3.6 \
        mnemonic==0.20 \
        pyjwt==2.4.0 \
        klein \
        werkzeug
}

# =============================================================================
# Build Docker images (shared across all suites)
# =============================================================================
build_images() {
    if [ "${SKIP_BUILD:-0}" = "1" ]; then
        log_info "Skipping image build (SKIP_BUILD/--no-build); reusing existing images"
        return 0
    fi
    log_info "Building Docker images (shared across suites)..."
    # Use --profile all so profile-gated services (e.g. jam-playwright,
    # neutrino*, reference, maker) are built too. Without this, profile
    # services keep stale images from previous runs.
    docker compose --profile all --profile e2e --profile maker \
        --profile neutrino --profile reference --profile reference-maker \
        --profile taker \
        build --parallel 2>&1 | tee "${PARALLEL_DIR}/build.log"
    log_success "Docker images built"
}

# =============================================================================
# Suite runners: each runs in a subshell, outputs to a log file
# =============================================================================

run_suite_unit() {
    local log="${PARALLEL_DIR}/unit.log"
    log_suite "Starting: Unit Tests"
    {
        COVERAGE_FILE=.coverage.unit pytest -c pytest.ini --fail-on-skip \
            -lv \
            --cov=jmcore --cov=jmwallet --cov=directory_server --cov=jmwalletd \
            --cov=tumbler \
            --cov=orderbook_watcher --cov=maker --cov=taker \
            --cov-report=term-missing \
            jmcore/ jmwallet/ directory_server/ jmwalletd/ tumbler/ orderbook_watcher/ maker/ taker/
    } > "$log" 2>&1
}

run_suite_e2e() {
    local suite="e2e"
    local log="${PARALLEL_DIR}/${suite}.log"
    local btc_rpc=$(host_port "$suite" btc_rpc)
    local dir_port=$(host_port "$suite" dir)
    local walletd_port=$(host_port "$suite" walletd)
    local prefix="${CONTAINER_PREFIX}-${suite}"

    log_suite "Starting: E2E Tests ($suite)"
    local rc=0
    {
        generate_override "$suite"
        cleanup_suite "$suite"
        compose_cmd "$suite" --profile e2e up -d

        if ! wait_for_bitcoin_rpc "$suite" "$btc_rpc"; then
            log_error "Bitcoin RPC not ready on host port $btc_rpc for suite $suite"
            return 1
        fi
        wait_for_port "$dir_port" "Directory ($suite)"
        wait_for_wallet_funder "$suite"

        sleep 20  # Wait for makers to connect

        # E2E tests
        BITCOIN_RPC_URL="http://127.0.0.1:${btc_rpc}" \
        BITCOIN_RPC_USER=test \
        BITCOIN_RPC_PASSWORD=test \
        JMWALLETD_URL="https://127.0.0.1:${walletd_port}" \
        DIRECTORY_PORT="${dir_port}" \
        JM_CONTAINER_PREFIX="${prefix}" \
        COMPOSE_PROJECT_NAME="${PROJECT_PREFIX}-${suite}" \
        COVERAGE_FILE=".coverage.${suite}" \
        pytest -c pytest.ini -m "e2e and not tumbler_e2e" --fail-on-skip \
            -lv --timeout=300 --reruns=1 --reruns-delay=10 \
            --cov --cov-report=term-missing \
            tests/

        # Docker integration tests (maker, jmwallet, directory_server)
        BITCOIN_RPC_URL="http://127.0.0.1:${btc_rpc}" \
        BITCOIN_RPC_USER=test \
        BITCOIN_RPC_PASSWORD=test \
        DIRECTORY_PORT="${dir_port}" \
        JM_CONTAINER_PREFIX="${prefix}" \
        COMPOSE_PROJECT_NAME="${PROJECT_PREFIX}-${suite}" \
        COVERAGE_FILE=".coverage.${suite}-docker" \
        pytest -c pytest.ini -m "docker and not e2e and not reference and not neutrino and not reference_maker" --fail-on-skip \
            -lv --timeout=300 \
            --cov --cov-report=term-missing \
            maker/tests/integration/ jmwallet/tests/ directory_server/tests/
    } > "$log" 2>&1 || rc=$?
    cleanup_suite "$suite"
    return $rc
}

run_suite_tumbler() {
    local suite="tumbler"
    local log="${PARALLEL_DIR}/${suite}.log"
    local btc_rpc=$(host_port "$suite" btc_rpc)
    local dir_port=$(host_port "$suite" dir)
    local walletd_port=$(host_port "$suite" walletd)
    local prefix="${CONTAINER_PREFIX}-${suite}"

    log_suite "Starting: Tumbler E2E Tests ($suite)"
    local rc=0
    {
        generate_override "$suite"
        cleanup_suite "$suite"
        JMWALLETD_IMAGE="joinmarket-ng-jmwalletd:latest" \
        MAKER_IMAGE="joinmarket-ng-maker1:latest" \
        DIRECTORY_IMAGE="joinmarket-ng-directory:latest" \
        ORDERBOOK_WATCHER_IMAGE="joinmarket-ng-orderbook-watcher:latest" \
        compose_cmd "$suite" --profile e2e up -d \
            bitcoin miner directory directory2 orderbook-watcher tor tor-init wallet-funder \
            jmwalletd maker1 maker2 maker3 maker4 maker5

        if ! wait_for_bitcoin_rpc "$suite" "$btc_rpc"; then
            log_error "Bitcoin RPC not ready on host port $btc_rpc for suite $suite"
            return 1
        fi
        wait_for_port "$dir_port" "Directory ($suite)"
        wait_for_wallet_funder "$suite"

        sleep 20

        BITCOIN_RPC_URL="http://127.0.0.1:${btc_rpc}" \
        BITCOIN_RPC_USER=test \
        BITCOIN_RPC_PASSWORD=test \
        JMWALLETD_URL="https://127.0.0.1:${walletd_port}" \
        DIRECTORY_PORT="${dir_port}" \
        JM_CONTAINER_PREFIX="${prefix}" \
        COMPOSE_PROJECT_NAME="${PROJECT_PREFIX}-${suite}" \
        COVERAGE_FILE=".coverage.${suite}" \
        pytest -c pytest.ini -m tumbler_e2e --fail-on-skip \
            -lv --timeout=1800 \
            --tb=long -rA \
            --cov --cov-report=term-missing \
            tests/e2e/test_tumbler_*.py
    } > "$log" 2>&1 || rc=$?
    if [ "$rc" -ne 0 ]; then
        # On failure, dump container logs for the most relevant services
        # (walletd + makers + bitcoin) so we can diagnose tumbler stalls
        # without re-running the whole suite.
        {
            echo
            echo "=========================================================="
            echo "Container logs for diagnostics (suite=${suite}, rc=${rc})"
            echo "=========================================================="
            for svc in jmwalletd maker1 maker2 maker3 maker4 maker5 bitcoin miner directory directory2; do
                echo
                echo "----- ${svc} -----"
                COMPOSE_PROJECT_NAME="${PROJECT_PREFIX}-${suite}" \
                    compose_cmd "$suite" logs --tail=200 "$svc" 2>&1 || true
            done
        } >> "$log" 2>&1 || true
    fi
    cleanup_suite "$suite"
    return $rc
}

run_suite_playwright() {
    local suite="playwright"
    local log="${PARALLEL_DIR}/${suite}.log"
    local btc_rpc=$(host_port "$suite" btc_rpc)
    local dir_port=$(host_port "$suite" dir)
    local jam_pw_port=$(host_port "$suite" jam_pw)
    local prefix="${CONTAINER_PREFIX}-${suite}"

    log_suite "Starting: Playwright Tests ($suite)"
    local rc=0
    {
        if ! command -v node >/dev/null 2>&1; then
            log_warning "Node.js not found -- skipping Playwright tests"
            exit 0
        fi

        generate_override "$suite"
        cleanup_suite "$suite"
        compose_cmd "$suite" --profile e2e up -d

        if ! wait_for_bitcoin_rpc "$suite" "$btc_rpc"; then
            log_error "Bitcoin RPC not ready on host port $btc_rpc for suite $suite"
            return 1
        fi
        wait_for_port "$dir_port" "Directory ($suite)"
        wait_for_wallet_funder "$suite"

        # Wait for jam-playwright (HTTPS, self-signed cert)
        for i in $(seq 1 60); do
            if curl -skf "https://127.0.0.1:${jam_pw_port}/api/v1/session" >/dev/null 2>&1; then
                break
            fi
            sleep 2
        done

        # Wait for at least 4 maker offers to appear in the orderbook so that
        # the collaborative-send playwright tests have enough counterparties.
        # The maker minimum the JAM UI accepts is 4. We poll the orderbook
        # watcher and time out after ~3 minutes.
        local obwatch_port=$(host_port "$suite" obwatch)
        for i in $(seq 1 90); do
            local n_offers
            n_offers=$(curl -sf "http://127.0.0.1:${obwatch_port}/orderbook.json" 2>/dev/null \
                | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('offers', [])))" 2>/dev/null \
                || echo 0)
            if [ "${n_offers:-0}" -ge 4 ]; then
                log_info "Orderbook has ${n_offers} offers (>=4), proceeding"
                break
            fi
            sleep 2
        done

        local PW_DIR="${PROJECT_ROOT}/tests/playwright"
        (cd "$PW_DIR" && npm install && npx playwright install chromium)

        JAM_URL="https://localhost:${jam_pw_port}" \
        JMWALLETD_URL="https://localhost:${jam_pw_port}" \
        BITCOIN_RPC_URL="http://localhost:${btc_rpc}" \
        BITCOIN_RPC_USER=test \
        BITCOIN_RPC_PASS=test \
        DIRECTORY_PORT="${dir_port}" \
        JM_CONTAINER_PREFIX="${prefix}" \
        COMPOSE_PROJECT_NAME="${PROJECT_PREFIX}-${suite}" \
        NODE_TLS_REJECT_UNAUTHORIZED=0 \
        bash -c "cd '${PW_DIR}' && npx playwright test"
    } > "$log" 2>&1 || rc=$?
    cleanup_suite "$suite"
    return $rc
}

run_suite_jmwallet() {
    local suite="jmwallet"
    local log="${PARALLEL_DIR}/${suite}.log"
    local btc_rpc=$(host_port "$suite" btc_rpc)
    local dir_port=$(host_port "$suite" dir)
    local prefix="${CONTAINER_PREFIX}-${suite}"

    log_suite "Starting: jmwallet Docker Tests ($suite)"
    local rc=0
    {
        generate_override "$suite"
        cleanup_suite "$suite"
        compose_cmd "$suite" up -d bitcoin

        if ! wait_for_bitcoin_rpc "$suite" "$btc_rpc"; then
            log_error "Bitcoin RPC not ready on host port $btc_rpc for suite $suite"
            return 1
        fi

        BITCOIN_RPC_URL="http://127.0.0.1:${btc_rpc}" \
        BITCOIN_RPC_USER=test \
        BITCOIN_RPC_PASSWORD=test \
        DIRECTORY_PORT="${dir_port}" \
        JM_CONTAINER_PREFIX="${prefix}" \
        COMPOSE_PROJECT_NAME="${PROJECT_PREFIX}-${suite}" \
        COVERAGE_FILE=".coverage.${suite}" \
        pytest -c pytest.ini -m "docker and not neutrino" --fail-on-skip \
            -lv --timeout=300 \
            --cov=jmwallet --cov-report=term-missing \
            jmwallet/tests/
    } > "$log" 2>&1 || rc=$?
    cleanup_suite "$suite"
    return $rc
}

run_suite_reference_interop() {
    local suite="reference-interop"
    local log="${PARALLEL_DIR}/${suite}.log"
    local btc_rpc=$(host_port "$suite" btc_rpc)
    local dir_port=$(host_port "$suite" dir)
    local prefix="${CONTAINER_PREFIX}-${suite}"

    log_suite "Starting: Reference Interop Tests ($suite)"
    local rc=0
    {
        generate_override "$suite"
        cleanup_suite "$suite"
        compose_cmd "$suite" --profile reference up -d

        if ! wait_for_bitcoin_rpc "$suite" "$btc_rpc"; then
            log_error "Bitcoin RPC not ready on host port $btc_rpc for suite $suite"
            return 1
        fi
        wait_for_port "$dir_port" "Directory ($suite)"
        wait_for_wallet_funder "$suite"
        wait_for_tor "$suite"
        wait_for_jam "$suite"

        sleep 30  # Wait for makers

        BITCOIN_RPC_URL="http://127.0.0.1:${btc_rpc}" \
        BITCOIN_RPC_USER=test \
        BITCOIN_RPC_PASSWORD=test \
        DIRECTORY_PORT="${dir_port}" \
        JM_CONTAINER_PREFIX="${prefix}" \
        COMPOSE_PROJECT_NAME="${PROJECT_PREFIX}-${suite}" \
        COVERAGE_FILE=".coverage.${suite}" \
        pytest -c pytest.ini -m reference --fail-on-skip \
            -lv --timeout=300 --reruns=1 --reruns-delay=10 \
            --cov --cov-report=term-missing \
            tests/e2e/test_our_maker_reference_taker.py
    } > "$log" 2>&1 || rc=$?
    cleanup_suite "$suite"
    return $rc
}

run_suite_reference_legacy() {
    local suite="reference-legacy"
    local log="${PARALLEL_DIR}/${suite}.log"
    local btc_rpc=$(host_port "$suite" btc_rpc)
    local dir_port=$(host_port "$suite" dir)
    local prefix="${CONTAINER_PREFIX}-${suite}"

    log_suite "Starting: Reference Legacy Tests ($suite)"
    local rc=0
    {
        generate_override "$suite"
        cleanup_suite "$suite"
        compose_cmd "$suite" --profile reference up -d

        if ! wait_for_bitcoin_rpc "$suite" "$btc_rpc"; then
            log_error "Bitcoin RPC not ready on host port $btc_rpc for suite $suite"
            return 1
        fi
        wait_for_port "$dir_port" "Directory ($suite)"
        wait_for_wallet_funder "$suite"
        wait_for_tor "$suite"
        wait_for_jam "$suite"

        sleep 30  # Wait for makers

        BITCOIN_RPC_URL="http://127.0.0.1:${btc_rpc}" \
        BITCOIN_RPC_USER=test \
        BITCOIN_RPC_PASSWORD=test \
        DIRECTORY_PORT="${dir_port}" \
        JM_CONTAINER_PREFIX="${prefix}" \
        COMPOSE_PROJECT_NAME="${PROJECT_PREFIX}-${suite}" \
        COVERAGE_FILE=".coverage.${suite}" \
        pytest -c pytest.ini -m reference --fail-on-skip \
            -lv --timeout=300 --reruns=1 --reruns-delay=10 \
            --cov --cov-report=term-missing \
            tests/e2e/test_reference_coinjoin.py tests/e2e/test_reference_bond_import.py
    } > "$log" 2>&1 || rc=$?
    cleanup_suite "$suite"
    return $rc
}

run_suite_neutrino_functional() {
    local suite="neutrino-functional"
    local log="${PARALLEL_DIR}/${suite}.log"
    local btc_rpc=$(host_port "$suite" btc_rpc)
    local dir_port=$(host_port "$suite" dir)
    local neutrino_port=$(host_port "$suite" neutrino)
    local prefix="${CONTAINER_PREFIX}-${suite}"

    log_suite "Starting: Neutrino Functional Tests ($suite)"
    local rc=0
    {
        generate_override "$suite"
        cleanup_suite "$suite"
        compose_cmd "$suite" --profile neutrino up -d

        if ! wait_for_bitcoin_rpc "$suite" "$btc_rpc"; then
            log_error "Bitcoin RPC not ready on host port $btc_rpc for suite $suite"
            return 1
        fi
        wait_for_port "$dir_port" "Directory ($suite)"
        wait_for_neutrino "$suite" "$neutrino_port"

        BITCOIN_RPC_URL="http://127.0.0.1:${btc_rpc}" \
        BITCOIN_RPC_USER=test \
        BITCOIN_RPC_PASSWORD=test \
        NEUTRINO_URL="https://127.0.0.1:${neutrino_port}" \
        DIRECTORY_PORT="${dir_port}" \
        JM_CONTAINER_PREFIX="${prefix}" \
        COMPOSE_PROJECT_NAME="${PROJECT_PREFIX}-${suite}" \
        COVERAGE_FILE=".coverage.${suite}" \
        pytest -c pytest.ini -m neutrino -k "not test_coinjoin" --fail-on-skip \
            -lv --timeout=300 --reruns=1 --reruns-delay=10 \
            --cov --cov-report=term-missing \
            tests/
    } > "$log" 2>&1 || rc=$?
    cleanup_suite "$suite"
    return $rc
}

run_suite_neutrino_coinjoin() {
    local suite="neutrino-coinjoin"
    local log="${PARALLEL_DIR}/${suite}.log"
    local btc_rpc=$(host_port "$suite" btc_rpc)
    local dir_port=$(host_port "$suite" dir)
    local neutrino_port=$(host_port "$suite" neutrino)
    local prefix="${CONTAINER_PREFIX}-${suite}"

    log_suite "Starting: Neutrino CoinJoin Tests ($suite)"
    local rc=0
    {
        generate_override "$suite"
        cleanup_suite "$suite"
        compose_cmd "$suite" --profile neutrino up -d

        if ! wait_for_bitcoin_rpc "$suite" "$btc_rpc"; then
            log_error "Bitcoin RPC not ready on host port $btc_rpc for suite $suite"
            return 1
        fi
        wait_for_port "$dir_port" "Directory ($suite)"
        wait_for_neutrino "$suite" "$neutrino_port"
        wait_for_wallet_funder "$suite"

        sleep 20

        BITCOIN_RPC_URL="http://127.0.0.1:${btc_rpc}" \
        BITCOIN_RPC_USER=test \
        BITCOIN_RPC_PASSWORD=test \
        NEUTRINO_URL="https://127.0.0.1:${neutrino_port}" \
        DIRECTORY_PORT="${dir_port}" \
        JM_CONTAINER_PREFIX="${prefix}" \
        COMPOSE_PROJECT_NAME="${PROJECT_PREFIX}-${suite}" \
        COVERAGE_FILE=".coverage.${suite}" \
        pytest -c pytest.ini -m neutrino -k "test_coinjoin" --fail-on-skip \
            -lv --timeout=300 --reruns=2 --reruns-delay=15 \
            --cov --cov-report=term-missing \
            tests/
    } > "$log" 2>&1 || rc=$?
    cleanup_suite "$suite"
    return $rc
}

run_suite_neutrino_reference() {
    local suite="neutrino-reference"
    local log="${PARALLEL_DIR}/${suite}.log"
    local btc_rpc=$(host_port "$suite" btc_rpc)
    local dir_port=$(host_port "$suite" dir)
    local neutrino_port=$(host_port "$suite" neutrino)
    local prefix="${CONTAINER_PREFIX}-${suite}"

    log_suite "Starting: Neutrino Reference Tests ($suite)"
    local rc=0
    {
        generate_override "$suite"
        cleanup_suite "$suite"
        compose_cmd "$suite" --profile reference --profile neutrino up -d

        if ! wait_for_bitcoin_rpc "$suite" "$btc_rpc"; then
            log_error "Bitcoin RPC not ready on host port $btc_rpc for suite $suite"
            return 1
        fi
        wait_for_port "$dir_port" "Directory ($suite)"
        wait_for_wallet_funder "$suite"
        wait_for_tor "$suite"
        wait_for_jam "$suite"
        wait_for_neutrino "$suite" "$neutrino_port"

        sleep 60  # Wait for makers to connect and announce offers

        BITCOIN_RPC_URL="http://127.0.0.1:${btc_rpc}" \
        BITCOIN_RPC_USER=test \
        BITCOIN_RPC_PASSWORD=test \
        NEUTRINO_URL="https://127.0.0.1:${neutrino_port}" \
        DIRECTORY_PORT="${dir_port}" \
        JM_CONTAINER_PREFIX="${prefix}" \
        COMPOSE_PROJECT_NAME="${PROJECT_PREFIX}-${suite}" \
        COVERAGE_FILE=".coverage.${suite}" \
        pytest -c pytest.ini -m neutrino_reference --fail-on-skip \
            -lv --timeout=900 --reruns=1 --reruns-delay=10 \
            --cov --cov-report=term-missing \
            tests/
    } > "$log" 2>&1 || rc=$?
    cleanup_suite "$suite"
    return $rc
}

run_suite_reference_maker() {
    local suite="reference-maker"
    local log="${PARALLEL_DIR}/${suite}.log"
    local btc_jam_rpc=$(host_port "$suite" btc_jam_rpc)
    local dir_port=$(host_port "$suite" dir)
    local prefix="${CONTAINER_PREFIX}-${suite}"

    log_suite "Starting: Reference Maker Tests ($suite)"
    local rc=0
    {
        generate_override "$suite"
        cleanup_suite "$suite"
        compose_cmd "$suite" --profile reference-maker up -d

        # reference-maker uses bitcoin-jam on port 18445
        for i in $(seq 1 60); do
            if compose_cmd "$suite" exec -T bitcoin-jam \
                bitcoin-cli -chain=regtest -rpcport=18445 \
                -rpcuser=test -rpcpassword=test getblockchaininfo >/dev/null 2>&1; then
                break
            fi
            sleep 2
        done

        wait_for_port "$dir_port" "Directory ($suite)"
        wait_for_tor "$suite"
        wait_for_jam_makers "$suite"

        BITCOIN_RPC_URL="http://127.0.0.1:${btc_jam_rpc}" \
        BITCOIN_RPC_USER=test \
        BITCOIN_RPC_PASSWORD=test \
        DIRECTORY_PORT="${dir_port}" \
        JM_CONTAINER_PREFIX="${prefix}" \
        COMPOSE_PROJECT_NAME="${PROJECT_PREFIX}-${suite}" \
        COVERAGE_FILE=".coverage.${suite}" \
        pytest -c pytest.ini -m reference_maker --fail-on-skip \
            -lv --timeout=300 --reruns=1 --reruns-delay=10 \
            --cov --cov-report=term-missing \
            tests/
    } > "$log" 2>&1 || rc=$?
    cleanup_suite "$suite"
    return $rc
}

# =============================================================================
# Launch a suite in the background and track its PID
# =============================================================================
launch_suite() {
    local suite_name=$1
    local runner_func=$2

    if [ "${MAX_CONCURRENT:-0}" -gt 0 ]; then
        wait_for_capacity
    fi

    SUITE_START_TIMES[$suite_name]=$(date +%s)
    $runner_func &
    SUITE_PIDS[$suite_name]=$!
    log_info "Launched $suite_name (PID ${SUITE_PIDS[$suite_name]})"
}

# Block until the number of still-running suite PIDs falls below MAX_CONCURRENT.
# Reaped suites have their results recorded so the final summary stays accurate
# regardless of completion order.
wait_for_capacity() {
    while :; do
        local running=0
        local sn
        for sn in "${!SUITE_PIDS[@]}"; do
            local pid=${SUITE_PIDS[$sn]}
            # Skip already-finalized entries.
            [ -n "${SUITE_RESULTS[$sn]:-}" ] && continue
            if kill -0 "$pid" 2>/dev/null; then
                running=$((running + 1))
            else
                # Reap and record result.
                local rc=0
                wait "$pid" 2>/dev/null || rc=$?
                local end=$(date +%s)
                local start=${SUITE_START_TIMES[$sn]:-$end}
                local duration=$((end - start))
                if [ "$rc" -eq 0 ]; then
                    SUITE_RESULTS[$sn]="PASS"
                    log_success "$sn passed (${duration}s)"
                else
                    SUITE_RESULTS[$sn]="FAIL"
                    log_error "$sn failed (${duration}s) -- see ${PARALLEL_DIR}/${sn}.log"
                fi
            fi
        done

        if [ "$running" -lt "$MAX_CONCURRENT" ]; then
            return 0
        fi
        sleep 2
    done
}

# =============================================================================
# Wait for all suites and collect results
# =============================================================================
wait_for_all() {
    local any_failed=false

    for suite_name in "${!SUITE_PIDS[@]}"; do
        # Skip suites that were already reaped by wait_for_capacity().
        if [ -n "${SUITE_RESULTS[$suite_name]:-}" ]; then
            [ "${SUITE_RESULTS[$suite_name]}" = "FAIL" ] && any_failed=true
            continue
        fi

        local pid=${SUITE_PIDS[$suite_name]}
        local start=${SUITE_START_TIMES[$suite_name]}

        if wait "$pid" 2>/dev/null; then
            local end=$(date +%s)
            local duration=$((end - start))
            SUITE_RESULTS[$suite_name]="PASS"
            log_success "$suite_name passed (${duration}s)"
        else
            local end=$(date +%s)
            local duration=$((end - start))
            SUITE_RESULTS[$suite_name]="FAIL"
            log_error "$suite_name failed (${duration}s) -- see ${PARALLEL_DIR}/${suite_name}.log"
            any_failed=true
        fi
    done

    $any_failed && return 1 || return 0
}

# =============================================================================
# Print final summary
# =============================================================================
print_summary() {
    echo
    echo "========================================================================"
    echo -e "${BOLD}Parallel Test Suite Summary${NC}"
    echo "========================================================================"

    local pass_count=0
    local fail_count=0
    local total_start=${GLOBAL_START_TIME:-$(date +%s)}
    local total_end=$(date +%s)
    local total_duration=$((total_end - total_start))

    for suite_name in $(echo "${!SUITE_RESULTS[@]}" | tr ' ' '\n' | sort); do
        local result=${SUITE_RESULTS[$suite_name]}
        local start=${SUITE_START_TIMES[$suite_name]:-$total_start}
        local log="${PARALLEL_DIR}/${suite_name}.log"

        if [ "$result" = "PASS" ]; then
            echo -e "  ${GREEN}PASS${NC}  $suite_name"
            ((pass_count++))
        else
            echo -e "  ${RED}FAIL${NC}  $suite_name  (log: $log)"
            ((fail_count++))
        fi
    done

    echo "------------------------------------------------------------------------"
    echo -e "  Total: $((pass_count + fail_count))  Pass: ${GREEN}${pass_count}${NC}  Fail: ${RED}${fail_count}${NC}"
    echo -e "  Wall time: ${BOLD}${total_duration}s${NC} ($((total_duration / 60))m $((total_duration % 60))s)"
    echo -e "  Logs: ${PARALLEL_DIR}/"
    echo "========================================================================"

    return $fail_count
}

# =============================================================================
# Main
# =============================================================================
main() {
    GLOBAL_START_TIME=$(date +%s)

    log_info "=== JoinMarket Parallel Test Suite ==="
    log_info "Starting at $(date)"
    log_info "Logs directory: ${PARALLEL_DIR}/"
    if [ "${MAX_CONCURRENT:-0}" -gt 0 ]; then
        log_info "Concurrency cap: ${MAX_CONCURRENT} suite(s) at a time"
    else
        log_info "Concurrency cap: unlimited"
    fi
    echo

    # Environment setup
    export BITCOIN_RPC_URL="http://127.0.0.1:18443"
    export BITCOIN_RPC_USER="test"
    export BITCOIN_RPC_PASSWORD="test"

    # Cleanup any previous runs
    log_info "Cleaning up previous runs..."
    cleanup_all 2>/dev/null || true

    # Phase 0: Build images (shared across all suites)
    build_images

    # Reference implementation (needed by some suites)
    setup_reference_implementation

    # Phase 1+2: Launch all suites in parallel
    log_info "Launching test suites in parallel..."
    echo

    # Unit tests (no Docker)
    launch_suite "unit" run_suite_unit

    # Docker test suites (each with isolated compose project)
    launch_suite "e2e" run_suite_e2e
    launch_suite "jmwallet" run_suite_jmwallet
    launch_suite "reference-interop" run_suite_reference_interop
    launch_suite "reference-legacy" run_suite_reference_legacy
    launch_suite "neutrino-functional" run_suite_neutrino_functional
    launch_suite "neutrino-coinjoin" run_suite_neutrino_coinjoin
    launch_suite "neutrino-reference" run_suite_neutrino_reference
    launch_suite "reference-maker" run_suite_reference_maker
    launch_suite "tumbler" run_suite_tumbler
    launch_suite "playwright" run_suite_playwright

    echo
    log_info "All suites launched. Waiting for completion..."
    log_info "Monitor progress with: tail -f ${PARALLEL_DIR}/*.log"
    echo

    # Wait for all suites
    local all_result=0
    if ! wait_for_all; then
        all_result=1
    fi

    # Summary
    print_summary || true

    if [ $all_result -ne 0 ]; then
        echo
        log_error "Some suites failed. Check logs in ${PARALLEL_DIR}/"
        log_info "To re-run a single suite:"
        log_info "  $0 --suite <suite-name>"
        log_info "Available suites: ${!SUITE_SLOT[*]}"
        exit 1
    fi

    log_success "All test suites passed!"
    exit 0
}

# =============================================================================
# Argument handling
# =============================================================================

# Pre-parse global flags that may precede the subcommand: --max-concurrent
# (alias --jobs / -j), --instance (alias -i), and --no-build, plus their =N forms.
while [ $# -gt 0 ]; do
    case "$1" in
        --max-concurrent|--jobs|-j)
            if [ -z "${2:-}" ]; then
                log_error "Option $1 requires an integer argument (use 0 for unlimited)"
                exit 1
            fi
            MAX_CONCURRENT="$2"
            shift 2
            ;;
        --max-concurrent=*|--jobs=*)
            MAX_CONCURRENT="${1#*=}"
            shift
            ;;
        --instance|-i)
            if [ -z "${2:-}" ]; then
                log_error "Option $1 requires an integer instance id"
                exit 1
            fi
            INSTANCE="$2"
            shift 2
            ;;
        --instance=*)
            INSTANCE="${1#*=}"
            shift
            ;;
        --no-build)
            SKIP_BUILD=1
            shift
            ;;
        *)
            break
            ;;
    esac
done

if ! [[ "$MAX_CONCURRENT" =~ ^[0-9]+$ ]]; then
    log_error "MAX_CONCURRENT must be a non-negative integer (got '$MAX_CONCURRENT')"
    exit 1
fi

if ! [[ "$INSTANCE" =~ ^[0-9]+$ ]]; then
    log_error "Instance id must be a non-negative integer (got '$INSTANCE')"
    exit 1
fi

# Re-derive instance-scoped globals now that INSTANCE is final.
apply_instance

case "${1:-}" in
    --cleanup|--cleanup-only)
        cleanup_all
        exit 0
        ;;
    --suite)
        suite="${2:-}"
        if [ -z "$suite" ]; then
            log_error "Usage: $0 --suite <suite-name>"
            log_info "Available suites: ${!SUITE_SLOT[*]}"
            exit 1
        fi
        GLOBAL_START_TIME=$(date +%s)
        export BITCOIN_RPC_URL="http://127.0.0.1:18443"
        export BITCOIN_RPC_USER="test"
        export BITCOIN_RPC_PASSWORD="test"

        # Keep single-suite reruns aligned with the full runner so they do not
        # accidentally exercise stale Docker images.
        build_images
        setup_reference_implementation

        # Map suite name to runner function
        case "$suite" in
            unit)                  run_suite_unit ;;
            e2e)                   run_suite_e2e ;;
            playwright)            run_suite_playwright ;;
            jmwallet)              run_suite_jmwallet ;;
            reference-interop)     run_suite_reference_interop ;;
            reference-legacy)      run_suite_reference_legacy ;;
            neutrino-functional)   run_suite_neutrino_functional ;;
            neutrino-coinjoin)     run_suite_neutrino_coinjoin ;;
            neutrino-reference)    run_suite_neutrino_reference ;;
            reference-maker)       run_suite_reference_maker ;;
            tumbler)               run_suite_tumbler ;;
            *)
                log_error "Unknown suite: $suite"
                log_info "Available suites: ${!SUITE_SLOT[*]}"
                exit 1
                ;;
        esac
        exit $?
        ;;
    --help|-h)
        cat <<EOF
JoinMarket Parallel Test Suite Runner

Runs all test suites in parallel using Docker Compose project isolation.
Each suite gets its own containers, ports, network, and volumes.

Usage:
  $0                              Run all suites in parallel
  $0 --suite <name>               Run a single suite
  $0 --instance <N>               Isolation id for concurrent runs (default 0)
  $0 --no-build                   Reuse existing images (skip the shared build)
  $0 --max-concurrent <N>         Limit concurrent suites (0 = unlimited;
                                   default = max(1, CPU cores / 2))
  $0 --jobs <N>                   Alias for --max-concurrent
  $0 --cleanup                    Clean up this instance's test resources
  $0 --cleanup-only               Alias for --cleanup
  $0 --help                       Show this help

Environment:
  MAX_CONCURRENT=<N>              Same as --max-concurrent
  JM_TEST_INSTANCE=<N>            Same as --instance
  JM_TEST_PORT_BASE=<port>        First host port of instance 0 (default 20000)
  SKIP_BUILD=1                    Same as --no-build

Running two suites at once:
  Give each invocation a distinct instance id so their Compose projects,
  container names, host ports, and logs do not collide. For example:
    ./scripts/run_parallel_tests.sh --instance 0
    ./scripts/run_parallel_tests.sh --instance 1 --no-build

Available suites:
  unit                  Unit tests (no Docker)
  e2e                   E2E + Docker integration tests
  playwright            Playwright browser tests
  jmwallet              jmwallet Docker tests
  reference-interop     Reference interop tests (our maker + JAM taker)
  reference-legacy      Reference legacy tests (JAM coinjoin + bond import)
  neutrino-functional   Neutrino functional tests
  neutrino-coinjoin     Neutrino CoinJoin tests
  neutrino-reference    Neutrino + reference combined tests
  reference-maker       Reference maker tests (JAM makers + our taker)
  tumbler               Tumbler end-to-end tests

Logs are written to: tmp/parallel-tests/i<instance>/<suite>.log

How it works:
  Each Docker-dependent suite runs in an isolated Docker Compose project
  with unique container names, host port mappings, networks, and volumes.
  Distinct --instance ids further isolate concurrent invocations onto
  separate Compose projects and disjoint host port bands. This mirrors CI
  where each job runs on a separate VM.

EOF
        exit 0
        ;;
    "")
        main
        ;;
    *)
        log_error "Unknown option: $1"
        echo "Use --help for usage information"
        exit 1
        ;;
esac
