#!/bin/bash
set -euo pipefail

# JoinMarket Complete Test Suite Runner
# Runs all tests in optimal order with proper cleanup
#
# This script uses --fail-on-skip to ensure that if tests are selected by markers
# but skipped due to missing setup conditions, the suite will fail. This helps
# catch configuration issues in CI.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${BLUE}[INFO]${NC} $*"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $*"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $*"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $*"
}

log_info "Working directory: $PROJECT_ROOT"

# Test results tracking
FAILED_TESTS=()
FAILED_TEST_DETAILS=()
COVERAGE_FILES=()
TEMP_TEST_OUTPUT="/tmp/jm_test_suite_$$.log"

# Environment variables matching CI setup
export BITCOIN_RPC_URL="http://127.0.0.1:18443"
export BITCOIN_RPC_USER="test"
export BITCOIN_RPC_PASSWORD="test"

# Complete cleanup function
cleanup_all() {
    log_info "Performing complete cleanup..."

    # Clean up temporary test output files
    rm -f "${TEMP_TEST_OUTPUT}".* 2>/dev/null || true

    # Stop all profiles
    docker compose --profile e2e --profile reference --profile neutrino --profile reference-maker down -v 2>/dev/null || true

    # Remove any orphaned containers
    docker compose down --remove-orphans -v 2>/dev/null || true

    # Force remove any containers with jm- prefix that might be stuck
    STUCK_CONTAINERS=$(docker ps -aq -f "name=jm-" 2>/dev/null || true)
    if [ -n "$STUCK_CONTAINERS" ]; then
        log_warning "Found stuck containers, force removing..."
        echo "$STUCK_CONTAINERS" | xargs docker rm -f 2>/dev/null || true
    fi

    # Clean up any dangling volumes
    docker volume prune -f >/dev/null 2>&1 || true

    log_success "Cleanup complete"
}

# Wait for service with timeout
wait_for_service() {
    local service=$1
    local max_attempts=${2:-30}
    local attempt=0

    log_info "Waiting for $service..."
    while [ $attempt -lt $max_attempts ]; do
        if docker compose ps "$service" 2>/dev/null | grep -q "healthy\|running"; then
            log_success "$service is ready"
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 2
    done

    log_error "$service failed to start"
    return 1
}

# Wait for Bitcoin RPC on the specified container and port
wait_for_bitcoin() {
    local container=${1:-bitcoin}
    local rpc_port=${2:-18443}
    log_info "Waiting for Bitcoin RPC ($container:$rpc_port)..."

    for i in {1..60}; do
        if docker compose exec -T "$container" bitcoin-cli -chain=regtest \
            -rpcport="$rpc_port" -rpcuser=test -rpcpassword=test getblockchaininfo >/dev/null 2>&1; then
            log_success "Bitcoin RPC ready ($container:$rpc_port)"
            return 0
        fi
        echo "  Attempt $i/60: $container not ready..."
        sleep 2
    done

    log_error "Bitcoin RPC timeout ($container:$rpc_port)"
    return 1
}

# Wait for directory server on port 5222
wait_for_directory_server() {
    log_info "Waiting for directory server..."

    for i in {1..30}; do
        if nc -z localhost 5222 2>/dev/null; then
            log_success "Directory server ready"
            return 0
        fi
        sleep 2
    done

    log_error "Directory server timeout"
    return 1
}

# Wait for Tor hidden service to generate .onion address
wait_for_tor_hidden_service() {
    log_info "Waiting for Tor hidden service..."

    for i in {1..90}; do
        if docker compose exec -T tor cat /var/lib/tor/directory/hostname 2>/dev/null | grep -q ".onion"; then
            local onion
            onion=$(docker compose exec -T tor cat /var/lib/tor/directory/hostname 2>/dev/null | tr -d '[:space:]')
            log_success "Tor ready with onion address: $onion"
            return 0
        fi
        echo "  Attempt $i/90: Waiting for Tor..."
        sleep 2
    done

    log_error "Tor hidden service timeout"
    return 1
}

# Wait for JAM web interface
wait_for_jam() {
    log_info "Waiting for JAM..."

    for i in {1..30}; do
        if docker compose exec -T jam sh -c "timeout 5 bash -c '</dev/tcp/127.0.0.1/80'" 2>/dev/null; then
            log_success "JAM ready"
            return 0
        fi
        sleep 5
    done

    log_error "JAM timeout"
    return 1
}

# Wait for JAM maker containers to be ready
wait_for_jam_makers() {
    log_info "Waiting for JAM makers..."

    for i in {1..30}; do
        if docker compose exec -T jam-maker1 sh -c "timeout 5 bash -c '</dev/tcp/127.0.0.1/80'" 2>/dev/null; then
            log_success "JAM maker1 ready"
            break
        fi
        sleep 5
    done

    log_info "Waiting for reference makers to connect..."
    sleep 30

    log_success "JAM makers ready"
}

# Wait for wallet-funder container to complete
# Note: -a is required because wallet-funder exits after completion,
# and `docker compose ps` without -a hides exited containers.
wait_for_wallet_funder() {
    log_info "Waiting for wallet-funder to complete..."

    for i in {1..60}; do
        local status
        status=$(docker compose ps -a wallet-funder --format '{{.Status}}' 2>/dev/null || echo "")
        if echo "$status" | grep -qi "exited\|completed"; then
            log_success "Wallet funder completed"
            return 0
        fi
        echo "  Attempt $i/60: wallet-funder status: $status"
        sleep 5
    done

    # Fall back to a fixed wait if the container status check fails
    log_warning "Could not confirm wallet-funder completion, waiting 30s as fallback..."
    sleep 30
}

# Wait for Neutrino sync
wait_for_neutrino() {
    log_info "Waiting for Neutrino to sync..."

    for i in {1..60}; do
        if curl -s http://localhost:8334/v1/status 2>/dev/null | grep -q '"synced":true'; then
            log_success "Neutrino synced!"
            return 0
        fi
        echo "  Attempt $i/60: Neutrino syncing..."
        sleep 5
    done

    log_error "Neutrino sync timeout"
    return 1
}

# Run tests and track results (always returns success to continue with other tests)
run_test_suite() {
    local test_name=$1
    shift

    log_info "Running $test_name..."
    log_info "Command: pytest -c \"$PROJECT_ROOT/pytest.ini\" --fail-on-skip $*"

    # Run pytest and capture output
    local test_output_file="${TEMP_TEST_OUTPUT}.${test_name// /_}"
    if pytest -c "$PROJECT_ROOT/pytest.ini" --fail-on-skip "$@" 2>&1 | tee "$test_output_file"; then
        log_success "$test_name passed"
        rm -f "$test_output_file"
    else
        log_error "$test_name failed"
        FAILED_TESTS+=("$test_name")

        # Extract failed test names from output
        local failed_details
        failed_details=$(grep -E "^FAILED|^ERROR" "$test_output_file" | head -10 || echo "See full output for details")
        FAILED_TEST_DETAILS+=("$test_name:|$failed_details")
    fi

    # Always return success to continue running other test suites
    return 0
}

# Wait for jam-playwright container (serves JAM frontend + jmwalletd on port 29183)
wait_for_jam_playwright() {
    log_info "Waiting for jam-playwright (port 29183)..."

    for i in {1..60}; do
        if curl -sf http://localhost:29183/api/v1/session >/dev/null 2>&1; then
            log_success "jam-playwright ready"
            return 0
        fi
        echo "  Attempt $i/60: jam-playwright not ready..."
        sleep 2
    done

    log_error "jam-playwright timeout"
    return 1
}

# Run Playwright browser tests against the jam-playwright container.
# Requires Node.js and npm. Skipped (with warning) if Node.js is not available.
run_playwright_tests() {
    log_info "=== Playwright Browser Tests ==="

    if ! command -v node >/dev/null 2>&1; then
        log_warning "Node.js not found — skipping Playwright tests"
        return 0
    fi

    local PW_DIR="${PROJECT_ROOT}/tests/playwright"
    if [ ! -f "${PW_DIR}/package.json" ]; then
        log_warning "Playwright test directory not found — skipping"
        return 0
    fi

    wait_for_jam_playwright || {
        log_error "jam-playwright container not ready — skipping Playwright tests"
        FAILED_TESTS+=("Playwright Tests")
        FAILED_TEST_DETAILS+=("Playwright Tests:|jam-playwright container failed to start on port 29183")
        return 0
    }

    log_info "Installing Playwright dependencies..."
    (cd "${PW_DIR}" && npm install && npx playwright install chromium) || {
        log_error "Playwright install failed"
        FAILED_TESTS+=("Playwright Tests")
        FAILED_TEST_DETAILS+=("Playwright Tests:|npm install or playwright install chromium failed")
        return 0
    }

    log_info "Running Playwright tests..."
    local test_output_file="${TEMP_TEST_OUTPUT}.playwright"
    export JAM_URL="${JAM_URL:-http://localhost:29183}"
    export JMWALLETD_URL="${JMWALLETD_URL:-http://localhost:29183}"

    if (cd "${PW_DIR}" && npx playwright test) 2>&1 | tee "$test_output_file"; then
        log_success "Playwright Tests passed"
        rm -f "$test_output_file"
    else
        log_error "Playwright Tests failed"
        FAILED_TESTS+=("Playwright Tests")
        local failed_details
        failed_details=$(grep -E "failed|Error|Timeout" "$test_output_file" | head -10 || echo "See full output for details")
        FAILED_TEST_DETAILS+=("Playwright Tests:|$failed_details")
    fi
}

# Restart makers to sync blockchain state
restart_makers() {
    log_info "Clearing maker commitment blacklists..."
    for maker in jm-maker1 jm-maker2 jm-maker3 jm-maker-neutrino; do
        docker exec "$maker" sh -c \
            "rm -rf /home/jm/.joinmarket-ng/cmtdata/commitmentlist" 2>/dev/null || true
    done
    log_info "Restarting makers to sync blockchain state..."
    docker compose restart maker1 maker2 maker3 maker-neutrino 2>/dev/null || true
    sleep 20
    log_success "Makers restarted with clean commitment state"
}

# Ensure the reference implementation (joinmarket-clientserver) is cloned and
# its Python dependencies are available.  The bond-validation and other
# reference-marked tests import jmclient from that checkout.
setup_reference_implementation() {
    if [ -d "$PROJECT_ROOT/joinmarket-clientserver/src/jmclient" ]; then
        log_info "Reference implementation already present"
    else
        log_info "Cloning reference implementation (joinmarket-clientserver)..."
        git clone --depth 1 https://github.com/JoinMarket-Org/joinmarket-clientserver.git \
            "$PROJECT_ROOT/joinmarket-clientserver"
    fi

    log_info "Installing reference implementation Python dependencies..."
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

    log_success "Reference implementation ready"
}

# Main test execution
main() {
    log_info "=== JoinMarket Complete Test Suite ==="
    log_info "Starting at $(date)"
    echo

    # Clean up on success only; on failure, leave containers for debugging.
    # The EXIT trap checks the exit code ($?) to decide.
    trap '
        exit_code=$?
        if [ $exit_code -eq 0 ]; then
            log_info "Cleaning up on success..."
            cleanup_all
        else
            log_warning "Skipping cleanup so you can inspect containers."
            log_info "Run: $0 --cleanup-only"
        fi
    ' EXIT

    # Initial cleanup
    cleanup_all

    # ========================================================================
    # Phase 1: Unit Tests (No Docker Required)
    # ========================================================================
    log_info "=== Phase 1: Unit Tests ==="

    # Note: pytest.ini already has '-m "not docker"' as default, so Docker tests
    # in maker/tests/integration/ are automatically excluded
    COVERAGE_FILE=.coverage.unit run_test_suite "Unit Tests" \
        -lv \
        --cov=jmcore --cov=jmwallet --cov=directory_server \
        --cov=orderbook_watcher --cov=maker --cov=taker \
        --cov-report=term-missing \
        --cov-report=html:htmlcov/unit \
        jmcore/ jmwallet/ directory_server/ orderbook_watcher/ maker/ taker/

    # Save unit test coverage
    if [ -f .coverage.unit ]; then
        COVERAGE_FILES+=(".coverage.unit")
        log_info "Saved unit test coverage"
    else
        log_warning "No .coverage file found for unit tests"
    fi

    echo

    # ========================================================================
    # Phase 2: E2E Tests (Our Implementation)
    # ========================================================================
    log_info "=== Phase 2: E2E Tests (Our Implementation) ==="

    log_info "Starting e2e profile..."
    docker compose --profile e2e up -d --build

    wait_for_bitcoin bitcoin 18443
    wait_for_directory_server
    wait_for_wallet_funder

    log_info "Waiting for makers to connect..."
    sleep 20

    # Run e2e tests from tests/ directory
    COVERAGE_FILE=.coverage.e2e run_test_suite "E2E Tests" \
        -lv -m e2e \
        --timeout=300 \
        --cov=jmcore --cov=jmwallet --cov=directory_server \
        --cov=orderbook_watcher --cov=maker --cov=taker \
        --cov-report=term-missing \
        --cov-report=html:htmlcov/e2e \
        tests/

    # Run Docker integration tests from component directories (e.g., maker/tests/integration/)
    # These are marked with @pytest.mark.docker but not with e2e/reference/neutrino
    # Run from project root to ensure conftest.py is loaded
    COVERAGE_FILE=.coverage.docker run_test_suite "Docker Integration Tests" \
        -lv -m "docker and not e2e and not reference and not neutrino and not reference_maker" \
        --timeout=300 \
        --cov=jmcore --cov=jmwallet --cov=directory_server \
        --cov=orderbook_watcher --cov=maker --cov=taker \
        --cov-report=term-missing \
        "$PROJECT_ROOT/maker/tests/integration/" \
        "$PROJECT_ROOT/jmwallet/tests/" \
        "$PROJECT_ROOT/directory_server/tests/"

    # Save e2e coverage
    if [ -f .coverage.e2e ]; then
        COVERAGE_FILES+=(".coverage.e2e")
        log_info "Saved e2e coverage"
    else
        log_warning "No .coverage file found for e2e tests"
    fi

    # Save docker integration coverage
    if [ -f .coverage.docker ]; then
        COVERAGE_FILES+=(".coverage.docker")
        log_info "Saved docker integration coverage"
    else
        log_warning "No .coverage file found for docker integration tests"
    fi

    # Run Playwright E2E tests against the jam-playwright container.
    # The container is already running from the e2e profile above.
    run_playwright_tests

    echo

    # ========================================================================
    # Phase 3: Reference Tests (Add JAM to existing e2e setup)
    # ========================================================================
    log_info "=== Phase 3: Reference Compatibility Tests ==="

    # Ensure reference implementation is available for import by tests
    setup_reference_implementation

    log_info "Starting reference profile components (keeping e2e running)..."
    docker compose --profile reference up -d --build

    # If directory containers were recreated above, tor's HiddenServicePort
    # backend mapping can be stale. Restart tor so onion routing to directory
    # is refreshed before JAM taker tests.
    log_info "Restarting tor to refresh hidden service backend mapping..."
    docker compose restart tor

    # Wait for reference-specific services
    wait_for_tor_hidden_service
    wait_for_jam

    log_info "Waiting for makers to connect..."
    sleep 30
    restart_makers

    COVERAGE_FILE=.coverage.reference run_test_suite "Reference Tests" \
        -lv -m reference \
        --timeout=300 \
        --cov=jmcore --cov=jmwallet --cov=directory_server \
        --cov=orderbook_watcher --cov=maker --cov=taker \
        --cov-report=term-missing \
        --cov-report=html:htmlcov/reference \
        tests/

    # Coverage file already saved with correct name
    if [ -f .coverage.reference ]; then
        COVERAGE_FILES+=(".coverage.reference")
        log_info "Saved reference test coverage"
    else
        log_warning "No .coverage file found for reference tests"
    fi

    echo

    # ========================================================================
    # Phase 4: Reference Maker Tests (Our Taker + JAM Makers)
    # ========================================================================
    log_info "=== Phase 4: Reference Maker Tests ==="

    # Full teardown of previous profiles - reference-maker uses its own bitcoin (bitcoin-jam).
    # Include --profile reference-maker in the teardown so volumes shared between
    # reference and reference-maker (jam-maker1-data, jam-maker2-data) can be removed
    # cleanly without "Resource is still in use" warnings.
    log_info "Tearing down previous profiles for clean reference-maker start..."
    docker compose --profile e2e --profile reference --profile reference-maker down -v

    log_info "Starting reference-maker profile..."
    docker compose --profile reference-maker up -d --build

    # Wait for reference-maker specific services (uses bitcoin-jam on port 18445)
    wait_for_bitcoin bitcoin-jam 18445
    wait_for_directory_server
    wait_for_tor_hidden_service
    wait_for_jam_makers

    COVERAGE_FILE=.coverage.reference_maker \
    BITCOIN_RPC_URL="http://127.0.0.1:18445" \
    run_test_suite "Reference Maker Tests" \
        -lv -m reference_maker \
        --timeout=300 \
        --cov=taker \
        --cov-report=term-missing \
        --cov-report=html:htmlcov/reference_maker \
        tests/

    # Coverage file already saved with correct name
    if [ -f .coverage.reference_maker ]; then
        COVERAGE_FILES+=(".coverage.reference_maker")
        log_info "Saved reference maker coverage"
    else
        log_warning "No .coverage file found for reference maker tests"
    fi

    echo

    # ========================================================================
    # Phase 5: Neutrino Tests (Fresh Setup)
    # ========================================================================
    log_info "=== Phase 5: Neutrino Backend Tests ==="

    log_info "Stopping previous profiles..."
    docker compose --profile e2e --profile reference --profile reference-maker down -v

    log_info "Starting neutrino profile..."
    docker compose --profile neutrino up -d --build

    wait_for_bitcoin bitcoin 18443
    wait_for_directory_server
    wait_for_neutrino
    wait_for_wallet_funder

    # Run basic neutrino tests (exclude coinjoin tests which need e2e makers)
    COVERAGE_FILE=.coverage.neutrino run_test_suite "Neutrino Basic Tests" \
        -lv -m "neutrino and not slow" \
        --timeout=300 \
        --cov=jmcore --cov=jmwallet --cov=maker \
        --cov-report=term-missing \
        --cov-report=html:htmlcov/neutrino \
        tests/

    # Save neutrino coverage
    if [ -f .coverage.neutrino ]; then
        COVERAGE_FILES+=(".coverage.neutrino")
        log_info "Saved neutrino basic test coverage"
    else
        log_warning "No .coverage file found for neutrino tests"
    fi

    # For neutrino coinjoin tests, also start e2e makers
    log_info "Starting e2e makers for neutrino coinjoin tests..."
    docker compose --profile e2e up -d
    wait_for_wallet_funder
    wait_for_neutrino

    log_info "Waiting for makers to connect..."
    sleep 20
    restart_makers

    COVERAGE_FILE=.coverage.neutrino_slow run_test_suite "Neutrino CoinJoin Tests" \
        -lv -m 'neutrino and slow' \
        --timeout=300 --reruns=1 --reruns-delay=15 \
        --cov=jmcore --cov=jmwallet --cov=maker --cov=taker \
        --cov-report=term-missing \
        --cov-report=html:htmlcov/neutrino_slow \
        tests/

    # Coverage file already saved with correct name
    if [ -f .coverage.neutrino_slow ]; then
        COVERAGE_FILES+=(".coverage.neutrino_slow")
        log_info "Saved neutrino coinjoin test coverage"
    else
        log_warning "No .coverage file found for neutrino slow tests"
    fi

    echo

    # ========================================================================
    # Combine Coverage Reports
    # ========================================================================
    if [ ${#COVERAGE_FILES[@]} -gt 0 ]; then
        log_info "=== Combining Coverage Reports ==="

        # Filter out missing coverage files
        EXISTING_COVERAGE_FILES=()
        for cov_file in "${COVERAGE_FILES[@]}"; do
            if [ -f "$cov_file" ]; then
                EXISTING_COVERAGE_FILES+=("$cov_file")
            else
                log_warning "Coverage file not found: $cov_file"
            fi
        done

        if [ ${#EXISTING_COVERAGE_FILES[@]} -gt 0 ]; then
            # Combine all existing coverage files
            coverage combine "${EXISTING_COVERAGE_FILES[@]}" 2>&1 || log_warning "Coverage combine had warnings"

            # Generate combined reports
            coverage report --skip-covered
            coverage html -d htmlcov/combined
            coverage xml -o coverage.xml

            log_success "Combined coverage report: htmlcov/combined/index.html"
        else
            log_warning "No coverage files found to combine"
        fi
    fi

    echo

    # ========================================================================
    # Summary
    # ========================================================================
    echo
    echo "========================================================================"
    log_info "=== Test Suite Summary ==="
    log_info "Completed at $(date)"
    echo "========================================================================"
    echo

    if [ ${#FAILED_TESTS[@]} -eq 0 ]; then
        log_success "✓ All test suites passed!"
        echo
        log_info "Test phases completed:"
        echo "  ✓ Unit Tests"
        echo "  ✓ E2E Tests"
        echo "  ✓ Docker Integration Tests"
        echo "  ✓ Playwright Browser Tests"
        echo "  ✓ Reference Compatibility Tests"
        echo "  ✓ Reference Maker Tests"
        echo "  ✓ Neutrino Basic Tests"
        echo "  ✓ Neutrino CoinJoin Tests"
        echo
        if [ ${#COVERAGE_FILES[@]} -gt 0 ]; then
            log_info "Coverage reports:"
            echo "  - Combined: htmlcov/combined/index.html"
            echo "  - XML: coverage.xml"
        fi
        exit 0
    else
        log_error "✗ Some test suites failed"
        echo
        log_error "Failed test suites (${#FAILED_TESTS[@]}):"

        # Show each failed test suite with its specific failures
        for i in "${!FAILED_TESTS[@]}"; do
            echo "  ✗ ${FAILED_TESTS[$i]}"

            # Show failed test details if available
            for detail in "${FAILED_TEST_DETAILS[@]}"; do
                if [[ "$detail" == "${FAILED_TESTS[$i]}:|"* ]]; then
                    # Extract the failure details after the separator
                    local failures="${detail#*:|}"
                    if [ -n "$failures" ] && [ "$failures" != "See full output for details" ]; then
                        echo "      Failed tests:"
                        echo "$failures" | while IFS= read -r line; do
                            if [ -n "$line" ]; then
                                echo "        - $line"
                            fi
                        done
                    fi
                    break
                fi
            done
        done

        echo
        log_info "=== Failure Details ==="
        log_info "To see detailed failure information, check the output above."
        log_info "Common issues:"
        echo "  - Docker containers not running: docker compose --profile <profile> up -d"
        echo "  - Services not ready: Wait longer or check service health"
        echo "  - Insufficient funds: Run wallet-funder or fund wallets manually"
        echo "  - Port conflicts: Check for processes using required ports"
        echo
        log_info "For complete test output, redirect to a file:"
        echo "  $0 2>&1 | tee /tmp/test_output.log"
        echo

        # Clean up temporary test output files
        rm -f "${TEMP_TEST_OUTPUT}".* 2>/dev/null || true

        exit 1
    fi
}

# Handle script arguments
case "${1:-}" in
    --cleanup-only)
        cleanup_all
        log_success "Cleanup complete"
        exit 0
        ;;
    --help|-h)
        cat <<EOF
JoinMarket Complete Test Suite Runner

Usage: $0 [OPTIONS]

Options:
    (no args)       Run complete test suite
    --cleanup-only  Only perform cleanup and exit
    --help, -h      Show this help message

The script runs all tests in this order:
1. Unit tests (no Docker)
2. E2E tests (our implementation) + Docker integration tests + Playwright browser tests
3. Reference tests (JAM compatibility)
4. Reference maker tests (JAM makers + our taker)
5. Neutrino tests (light client backend)

Coverage reports are generated for each phase and combined at the end.
All Docker resources are cleaned up before and after the run.

IMPORTANT: Uses --fail-on-skip to ensure tests don't silently skip.
If tests are selected by markers but skip due to missing setup,
the suite will fail. This catches configuration issues in CI.

All pytest commands use '-c pytest.ini' to ensure the root conftest.py
is loaded, which registers the --fail-on-skip option.

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
