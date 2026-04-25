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

# Directory for per-suite logs and override files
PARALLEL_DIR="${PROJECT_ROOT}/tmp/parallel-tests"
mkdir -p "$PARALLEL_DIR"

# Track background PIDs and results
declare -A SUITE_PIDS
declare -A SUITE_RESULTS
declare -A SUITE_START_TIMES

# =============================================================================
# Port allocation per suite
# Each suite gets a unique port range to avoid conflicts.
# Base port = OFFSET + standard port.
# =============================================================================
#
# Standard ports:
#   18443 - Bitcoin RPC (main)
#   18444 - Bitcoin P2P (main)
#   18445 - Bitcoin RPC (JAM)
#   18446 - Bitcoin P2P (JAM)
#   5222  - Directory server
#   5223  - Directory server 2
#   8080  - Orderbook watcher
#   8334  - Neutrino API
#   28183 - jmwalletd API
#   29183 - jam-playwright
#   19050 - Tor SOCKS
#   19051 - Tor control
#   28332 - ZMQ block
#   28333 - ZMQ tx
#
# Port offsets per suite (each suite adds this to standard ports).
# The gap (1001) must exceed the largest difference between any two base
# ports (29183 - 28183 = 1000) to avoid inter-suite port collisions.
declare -A PORT_OFFSETS=(
    [e2e]=0
    [playwright]=1001
    [jmwallet]=2002
    [maker]=3003
    [directory]=4004
    [reference-interop]=5005
    [reference-legacy]=6006
    [neutrino-functional]=7007
    [neutrino-coinjoin]=8008
    [neutrino-reference]=9009
    [reference-maker]=10010
)

# =============================================================================
# Generate Docker Compose override file for a suite
# =============================================================================
generate_override() {
    local suite=$1
    local offset=${PORT_OFFSETS[$suite]}
    local prefix="jm-${suite}"
    local override_file="${PARALLEL_DIR}/docker-compose.${suite}.override.yml"

    # Calculate ports
    local btc_rpc=$((18443 + offset))
    local btc_p2p=$((18444 + offset))
    local btc_jam_rpc=$((18445 + offset))
    local btc_jam_p2p=$((18446 + offset))
    local dir_port=$((5222 + offset))
    local dir2_port=$((5223 + offset))
    local obwatch_port=$((8080 + offset))
    local neutrino_port=$((8334 + offset))
    local walletd_port=$((28183 + offset))
    local jam_pw_port=$((29183 + offset))
    local tor_socks=$((19050 + offset))
    local tor_ctrl=$((19051 + offset))
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
    local project="jmpt-${suite}"
    local override_file="${PARALLEL_DIR}/docker-compose.${suite}.override.yml"

    docker compose \
        -p "$project" \
        -f "${PROJECT_ROOT}/docker-compose.yml" \
        -f "$override_file" \
        "$@"
}

# =============================================================================
# Wait for Bitcoin RPC readiness on a specific port
# =============================================================================
wait_for_bitcoin_rpc() {
    local suite=$1
    local port=$2
    local max_attempts=${3:-60}
    local prefix="jm-${suite}"

    for i in $(seq 1 $max_attempts); do
        if compose_cmd "$suite" exec -T bitcoin \
            bitcoin-cli -chain=regtest -rpcport=18443 \
            -rpcuser=test -rpcpassword=test getblockchaininfo >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
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
        local status
        status=$(compose_cmd "$suite" ps -a wallet-funder --format '{{.Status}}' 2>/dev/null || echo "")
        if echo "$status" | grep -qi "exited\|completed"; then
            return 0
        fi
        sleep 5
    done
    log_warning "[$suite] Could not confirm wallet-funder completion, continuing..."
    return 0
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
    local prefix="jm-${suite}"

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
    local prefix="jm-${suite}"
    for maker in "${prefix}-maker1" "${prefix}-maker2" "${prefix}-maker3" "${prefix}-maker-neutrino"; do
        docker exec "$maker" sh -c \
            "rm -rf /home/jm/.joinmarket-ng/cmtdata/commitmentlist" 2>/dev/null || true
    done
    compose_cmd "$suite" restart maker1 maker2 maker3 maker-neutrino 2>/dev/null || true
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
    log_info "Cleaning up all parallel test suites..."
    for suite in "${!PORT_OFFSETS[@]}"; do
        cleanup_suite "$suite" 2>/dev/null &
    done
    wait

    # Also clean up the default project
    docker compose --profile e2e --profile reference --profile neutrino --profile reference-maker down -v 2>/dev/null || true
    docker compose down --remove-orphans -v 2>/dev/null || true
    docker volume prune -f >/dev/null 2>&1 || true

    log_success "All parallel suites cleaned up"
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
    log_info "Building Docker images (shared across suites)..."
    docker compose build --parallel 2>&1 | tee "${PARALLEL_DIR}/build.log"
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
    local offset=${PORT_OFFSETS[$suite]}
    local btc_rpc=$((18443 + offset))
    local dir_port=$((5222 + offset))
    local prefix="jm-${suite}"

    log_suite "Starting: E2E Tests ($suite)"
    local rc=0
    {
        generate_override "$suite"
        cleanup_suite "$suite"
        compose_cmd "$suite" --profile e2e up -d

        wait_for_bitcoin_rpc "$suite" "$btc_rpc"
        wait_for_port "$dir_port" "Directory ($suite)"
        wait_for_wallet_funder "$suite"

        sleep 20  # Wait for makers to connect

        # E2E tests
        BITCOIN_RPC_URL="http://127.0.0.1:${btc_rpc}" \
        BITCOIN_RPC_USER=test \
        BITCOIN_RPC_PASSWORD=test \
        DIRECTORY_PORT="${dir_port}" \
        JM_CONTAINER_PREFIX="${prefix}" \
        COMPOSE_PROJECT_NAME="jmpt-${suite}" \
        COVERAGE_FILE=".coverage.${suite}" \
        pytest -c pytest.ini -m e2e --fail-on-skip \
            -lv --timeout=300 --reruns=1 --reruns-delay=10 \
            --cov --cov-report=term-missing \
            tests/

        # Docker integration tests (maker, jmwallet, directory_server)
        BITCOIN_RPC_URL="http://127.0.0.1:${btc_rpc}" \
        BITCOIN_RPC_USER=test \
        BITCOIN_RPC_PASSWORD=test \
        DIRECTORY_PORT="${dir_port}" \
        JM_CONTAINER_PREFIX="${prefix}" \
        COMPOSE_PROJECT_NAME="jmpt-${suite}" \
        COVERAGE_FILE=".coverage.${suite}-docker" \
        pytest -c pytest.ini -m "docker and not e2e and not reference and not neutrino and not reference_maker" --fail-on-skip \
            -lv --timeout=300 \
            --cov --cov-report=term-missing \
            maker/tests/integration/ jmwallet/tests/ directory_server/tests/
    } > "$log" 2>&1 || rc=$?
    cleanup_suite "$suite"
    return $rc
}

run_suite_playwright() {
    local suite="playwright"
    local log="${PARALLEL_DIR}/${suite}.log"
    local offset=${PORT_OFFSETS[$suite]}
    local btc_rpc=$((18443 + offset))
    local dir_port=$((5222 + offset))
    local jam_pw_port=$((29183 + offset))
    local prefix="jm-${suite}"

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

        wait_for_bitcoin_rpc "$suite" "$btc_rpc"
        wait_for_port "$dir_port" "Directory ($suite)"

        # Wait for jam-playwright
        for i in $(seq 1 60); do
            if curl -sf "http://127.0.0.1:${jam_pw_port}/api/v1/session" >/dev/null 2>&1; then
                break
            fi
            sleep 2
        done

        local PW_DIR="${PROJECT_ROOT}/tests/playwright"
        (cd "$PW_DIR" && npm install && npx playwright install chromium)

        JAM_URL="http://localhost:${jam_pw_port}" \
        JMWALLETD_URL="http://localhost:${jam_pw_port}" \
        BITCOIN_RPC_URL="http://localhost:${btc_rpc}" \
        BITCOIN_RPC_USER=test \
        BITCOIN_RPC_PASS=test \
        DIRECTORY_PORT="${dir_port}" \
        JM_CONTAINER_PREFIX="${prefix}" \
        COMPOSE_PROJECT_NAME="jmpt-${suite}" \
        bash -c "cd '${PW_DIR}' && npx playwright test"
    } > "$log" 2>&1 || rc=$?
    cleanup_suite "$suite"
    return $rc
}

run_suite_jmwallet() {
    local suite="jmwallet"
    local log="${PARALLEL_DIR}/${suite}.log"
    local offset=${PORT_OFFSETS[$suite]}
    local btc_rpc=$((18443 + offset))
    local dir_port=$((5222 + offset))
    local prefix="jm-${suite}"

    log_suite "Starting: jmwallet Docker Tests ($suite)"
    local rc=0
    {
        generate_override "$suite"
        cleanup_suite "$suite"
        compose_cmd "$suite" up -d bitcoin

        wait_for_bitcoin_rpc "$suite" "$btc_rpc"

        BITCOIN_RPC_URL="http://127.0.0.1:${btc_rpc}" \
        BITCOIN_RPC_USER=test \
        BITCOIN_RPC_PASSWORD=test \
        DIRECTORY_PORT="${dir_port}" \
        JM_CONTAINER_PREFIX="${prefix}" \
        COMPOSE_PROJECT_NAME="jmpt-${suite}" \
        COVERAGE_FILE=".coverage.${suite}" \
        pytest -c pytest.ini -m "docker and not neutrino" --fail-on-skip \
            -lv --timeout=300 \
            --cov=jmwallet --cov-report=term-missing \
            jmwallet/tests/
    } > "$log" 2>&1 || rc=$?
    cleanup_suite "$suite"
    return $rc
}

run_suite_maker() {
    local suite="maker"
    local log="${PARALLEL_DIR}/${suite}.log"
    local offset=${PORT_OFFSETS[$suite]}
    local btc_rpc=$((18443 + offset))
    local dir_port=$((5222 + offset))
    local prefix="jm-${suite}"

    log_suite "Starting: Maker Docker Tests ($suite)"
    local rc=0
    {
        generate_override "$suite"
        cleanup_suite "$suite"
        compose_cmd "$suite" up -d bitcoin

        wait_for_bitcoin_rpc "$suite" "$btc_rpc"

        BITCOIN_RPC_URL="http://127.0.0.1:${btc_rpc}" \
        BITCOIN_RPC_USER=test \
        BITCOIN_RPC_PASSWORD=test \
        DIRECTORY_PORT="${dir_port}" \
        JM_CONTAINER_PREFIX="${prefix}" \
        COMPOSE_PROJECT_NAME="jmpt-${suite}" \
        COVERAGE_FILE=".coverage.${suite}" \
        pytest -c pytest.ini -m docker --fail-on-skip \
            -lv --timeout=300 \
            --cov=maker --cov-report=term-missing \
            maker/tests/
    } > "$log" 2>&1 || rc=$?
    cleanup_suite "$suite"
    return $rc
}

run_suite_directory() {
    local suite="directory"
    local log="${PARALLEL_DIR}/${suite}.log"

    log_suite "Starting: Directory Server Docker Tests ($suite)"
    {
        # directory_server docker tests don't need compose services
        COVERAGE_FILE=".coverage.${suite}" \
        pytest -c pytest.ini -m docker --fail-on-skip \
            -lv --timeout=300 \
            --cov=directory_server --cov-report=term-missing \
            directory_server/tests/
    } > "$log" 2>&1
}

run_suite_reference_interop() {
    local suite="reference-interop"
    local log="${PARALLEL_DIR}/${suite}.log"
    local offset=${PORT_OFFSETS[$suite]}
    local btc_rpc=$((18443 + offset))
    local dir_port=$((5222 + offset))
    local prefix="jm-${suite}"

    log_suite "Starting: Reference Interop Tests ($suite)"
    local rc=0
    {
        generate_override "$suite"
        cleanup_suite "$suite"
        compose_cmd "$suite" --profile reference up -d

        wait_for_bitcoin_rpc "$suite" "$btc_rpc"
        wait_for_port "$dir_port" "Directory ($suite)"
        wait_for_tor "$suite"
        wait_for_jam "$suite"

        sleep 30  # Wait for makers

        BITCOIN_RPC_URL="http://127.0.0.1:${btc_rpc}" \
        BITCOIN_RPC_USER=test \
        BITCOIN_RPC_PASSWORD=test \
        DIRECTORY_PORT="${dir_port}" \
        JM_CONTAINER_PREFIX="${prefix}" \
        COMPOSE_PROJECT_NAME="jmpt-${suite}" \
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
    local offset=${PORT_OFFSETS[$suite]}
    local btc_rpc=$((18443 + offset))
    local dir_port=$((5222 + offset))
    local prefix="jm-${suite}"

    log_suite "Starting: Reference Legacy Tests ($suite)"
    local rc=0
    {
        generate_override "$suite"
        cleanup_suite "$suite"
        compose_cmd "$suite" --profile reference up -d

        wait_for_bitcoin_rpc "$suite" "$btc_rpc"
        wait_for_port "$dir_port" "Directory ($suite)"
        wait_for_tor "$suite"
        wait_for_jam "$suite"

        sleep 30  # Wait for makers

        BITCOIN_RPC_URL="http://127.0.0.1:${btc_rpc}" \
        BITCOIN_RPC_USER=test \
        BITCOIN_RPC_PASSWORD=test \
        DIRECTORY_PORT="${dir_port}" \
        JM_CONTAINER_PREFIX="${prefix}" \
        COMPOSE_PROJECT_NAME="jmpt-${suite}" \
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
    local offset=${PORT_OFFSETS[$suite]}
    local btc_rpc=$((18443 + offset))
    local dir_port=$((5222 + offset))
    local neutrino_port=$((8334 + offset))
    local prefix="jm-${suite}"

    log_suite "Starting: Neutrino Functional Tests ($suite)"
    local rc=0
    {
        generate_override "$suite"
        cleanup_suite "$suite"
        compose_cmd "$suite" --profile neutrino up -d

        wait_for_bitcoin_rpc "$suite" "$btc_rpc"
        wait_for_port "$dir_port" "Directory ($suite)"
        wait_for_neutrino "$suite" "$neutrino_port"

        BITCOIN_RPC_URL="http://127.0.0.1:${btc_rpc}" \
        BITCOIN_RPC_USER=test \
        BITCOIN_RPC_PASSWORD=test \
        NEUTRINO_URL="https://127.0.0.1:${neutrino_port}" \
        DIRECTORY_PORT="${dir_port}" \
        JM_CONTAINER_PREFIX="${prefix}" \
        COMPOSE_PROJECT_NAME="jmpt-${suite}" \
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
    local offset=${PORT_OFFSETS[$suite]}
    local btc_rpc=$((18443 + offset))
    local dir_port=$((5222 + offset))
    local neutrino_port=$((8334 + offset))
    local prefix="jm-${suite}"

    log_suite "Starting: Neutrino CoinJoin Tests ($suite)"
    local rc=0
    {
        generate_override "$suite"
        cleanup_suite "$suite"
        compose_cmd "$suite" --profile neutrino up -d

        wait_for_bitcoin_rpc "$suite" "$btc_rpc"
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
        COMPOSE_PROJECT_NAME="jmpt-${suite}" \
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
    local offset=${PORT_OFFSETS[$suite]}
    local btc_rpc=$((18443 + offset))
    local dir_port=$((5222 + offset))
    local neutrino_port=$((8334 + offset))
    local prefix="jm-${suite}"

    log_suite "Starting: Neutrino Reference Tests ($suite)"
    local rc=0
    {
        generate_override "$suite"
        cleanup_suite "$suite"
        compose_cmd "$suite" --profile reference --profile neutrino up -d

        wait_for_bitcoin_rpc "$suite" "$btc_rpc"
        wait_for_port "$dir_port" "Directory ($suite)"
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
        COMPOSE_PROJECT_NAME="jmpt-${suite}" \
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
    local offset=${PORT_OFFSETS[$suite]}
    local btc_jam_rpc=$((18445 + offset))
    local dir_port=$((5222 + offset))
    local prefix="jm-${suite}"

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
        COMPOSE_PROJECT_NAME="jmpt-${suite}" \
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

    SUITE_START_TIMES[$suite_name]=$(date +%s)
    $runner_func &
    SUITE_PIDS[$suite_name]=$!
    log_info "Launched $suite_name (PID ${SUITE_PIDS[$suite_name]})"
}

# =============================================================================
# Wait for all suites and collect results
# =============================================================================
wait_for_all() {
    local any_failed=false

    for suite_name in "${!SUITE_PIDS[@]}"; do
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

    # Directory server docker tests (no compose services needed)
    launch_suite "directory" run_suite_directory

    # Docker test suites (each with isolated compose project)
    launch_suite "e2e" run_suite_e2e
    launch_suite "jmwallet" run_suite_jmwallet
    launch_suite "maker" run_suite_maker
    launch_suite "reference-interop" run_suite_reference_interop
    launch_suite "reference-legacy" run_suite_reference_legacy
    launch_suite "neutrino-functional" run_suite_neutrino_functional
    launch_suite "neutrino-coinjoin" run_suite_neutrino_coinjoin
    launch_suite "neutrino-reference" run_suite_neutrino_reference
    launch_suite "reference-maker" run_suite_reference_maker
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
        log_info "Available suites: ${!PORT_OFFSETS[*]}"
        exit 1
    fi

    log_success "All test suites passed!"
    exit 0
}

# =============================================================================
# Argument handling
# =============================================================================
case "${1:-}" in
    --cleanup|--cleanup-only)
        cleanup_all
        exit 0
        ;;
    --suite)
        suite="${2:-}"
        if [ -z "$suite" ]; then
            log_error "Usage: $0 --suite <suite-name>"
            log_info "Available suites: ${!PORT_OFFSETS[*]}"
            exit 1
        fi
        GLOBAL_START_TIME=$(date +%s)
        export BITCOIN_RPC_URL="http://127.0.0.1:18443"
        export BITCOIN_RPC_USER="test"
        export BITCOIN_RPC_PASSWORD="test"

        # Map suite name to runner function
        case "$suite" in
            unit)                  run_suite_unit ;;
            e2e)                   run_suite_e2e ;;
            playwright)            run_suite_playwright ;;
            jmwallet)              run_suite_jmwallet ;;
            maker)                 run_suite_maker ;;
            directory)             run_suite_directory ;;
            reference-interop)     run_suite_reference_interop ;;
            reference-legacy)      run_suite_reference_legacy ;;
            neutrino-functional)   run_suite_neutrino_functional ;;
            neutrino-coinjoin)     run_suite_neutrino_coinjoin ;;
            neutrino-reference)    run_suite_neutrino_reference ;;
            reference-maker)       run_suite_reference_maker ;;
            *)
                log_error "Unknown suite: $suite"
                log_info "Available suites: ${!PORT_OFFSETS[*]}"
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
  $0                    Run all suites in parallel
  $0 --suite <name>     Run a single suite
  $0 --cleanup          Clean up all parallel test resources
  $0 --cleanup-only     Alias for --cleanup
  $0 --help             Show this help

Available suites:
  unit                  Unit tests (no Docker)
  directory             Directory server Docker tests
  e2e                   E2E + Docker integration tests
  playwright            Playwright browser tests
  jmwallet              jmwallet Docker tests
  maker                 Maker Docker tests
  reference-interop     Reference interop tests (our maker + JAM taker)
  reference-legacy      Reference legacy tests (JAM coinjoin + bond import)
  neutrino-functional   Neutrino functional tests
  neutrino-coinjoin     Neutrino CoinJoin tests
  neutrino-reference    Neutrino + reference combined tests
  reference-maker       Reference maker tests (JAM makers + our taker)

Logs are written to: tmp/parallel-tests/<suite>.log

How it works:
  Each Docker-dependent suite runs in an isolated Docker Compose project
  with unique container names, port mappings, networks, and volumes.
  This mirrors CI where each job runs on a separate VM.

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
