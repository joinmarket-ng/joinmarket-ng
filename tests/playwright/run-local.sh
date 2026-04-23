#!/usr/bin/env bash
# -------------------------------------------------------------------
# Run Playwright E2E tests against a local Docker Compose environment.
#
# Usage:
#   ./tests/playwright/run-local.sh                        # headless (default)
#   ./tests/playwright/run-local.sh --headed               # with browser visible
#   ./tests/playwright/run-local.sh --headed --slowmo 500  # headed + 500ms slowdown
#   ./tests/playwright/run-local.sh --ui                   # Playwright UI mode
#   ./tests/playwright/run-local.sh --debug                # debug mode
#
# Prerequisites:
#   - Docker + Docker Compose
#   - Node.js >= 18
#
# This script will:
#   1. Start Docker Compose services (e2e profile)
#   2. Wait for Bitcoin Core and the jam-playwright container
#   3. Install Playwright + browsers
#   4. Run the tests
# -------------------------------------------------------------------

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PW_DIR="${REPO_ROOT}/tests/playwright"

cd "${REPO_ROOT}"

# -- Colours for output --
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[-]${NC} $*" >&2; }

# -- Parse arguments --
# Separate --slowmo <value> from other playwright args.
PW_ARGS=()
SLOWMO=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --slowmo)
      SLOWMO="$2"
      shift 2
      ;;
    --slowmo=*)
      SLOWMO="${1#--slowmo=}"
      shift
      ;;
    *)
      PW_ARGS+=("$1")
      shift
      ;;
  esac
done

# -- 1. Start Docker services --
info "Starting Docker Compose services (e2e profile)..."
# Use || true so that one-shot services (like wallet-funder) exiting with
# non-zero codes do not abort the script. The individual service health
# checks below will catch any real failures.
docker compose --profile e2e up -d || true

info "Waiting for Bitcoin Core..."
timeout 60 bash -c 'until curl -s -o /dev/null http://localhost:18443; do sleep 2; done' || {
  error "Bitcoin Core did not start in time"
  exit 1
}

# -- Chain-height guard --
# On regtest the block reward halves every 150 blocks.  Above ~1200 blocks the
# coinbase reward is < 0.2 BTC and wallets quickly accumulate only dust UTXOs.
# When the chain gets too tall, nuke the Docker volumes and start fresh so that
# the wallet-funder service re-creates a usable chain from genesis.
MAX_HEIGHT=1200
BLOCK_HEIGHT=$(curl -sf --user test:test \
  --data-binary '{"jsonrpc":"1.0","method":"getblockcount","params":[]}' \
  -H 'content-type:text/plain;' http://localhost:18443 \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['result'])" 2>/dev/null || echo 0)
if [[ "${BLOCK_HEIGHT}" -gt "${MAX_HEIGHT}" ]]; then
  warn "Chain height ${BLOCK_HEIGHT} exceeds ${MAX_HEIGHT} — block rewards are dust."
  warn "Resetting Docker volumes for a fresh regtest chain..."
  docker compose --profile e2e down -v
  docker compose --profile e2e up -d || true
  # Re-wait for services after the reset.
  info "Waiting for Bitcoin Core (post-reset)..."
  timeout 60 bash -c 'until curl -s -o /dev/null http://localhost:18443; do sleep 2; done' || {
    error "Bitcoin Core did not start in time after reset"
    exit 1
  }
fi

info "Waiting for jam-playwright (port 29183)..."
timeout 120 bash -c 'until curl -skf https://localhost:29183/api/v1/session >/dev/null 2>&1; do sleep 2; done' || {
  error "jam-playwright container did not start in time"
  exit 1
}

# -- 2. Install Playwright --
info "Installing Playwright dependencies..."
cd "${PW_DIR}"
npm install
npx playwright install chromium

# -- 3. Run tests --
info "Running Playwright tests..."
export JAM_URL="${JAM_URL:-https://localhost:29183}"
export JMWALLETD_URL="${JMWALLETD_URL:-https://localhost:29183}"
# jmwalletd serves a self-signed cert; allow Node's global fetch to accept it.
export NODE_TLS_REJECT_UNAUTHORIZED=0

# Build the playwright command.
# --slowmo is passed as an env var PLAYWRIGHT_SLOWMO (read by playwright.config.ts)
# or via the --slowmo flag if the playwright CLI supports it.
CMD=(npx playwright test)
[[ ${#PW_ARGS[@]} -gt 0 ]] && CMD+=("${PW_ARGS[@]}")
[[ -n "${SLOWMO}" ]] && export PLAYWRIGHT_SLOWMO="${SLOWMO}"

"${CMD[@]}"

info "Done! Results in ${PW_DIR}/playwright-report/"
