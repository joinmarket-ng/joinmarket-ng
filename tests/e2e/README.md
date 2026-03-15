# End-to-End Integration Tests

Complete system tests with all JoinMarket components.

## Quick Start

```bash
# Start services for your test scenario (pick one)
docker compose --profile e2e up -d --build      # Our implementation only
docker compose --profile reference up -d --build # Reference compatibility
docker compose --profile neutrino up -d --build  # Neutrino backend

# Wait for services
sleep 30

# Run relevant tests
pytest tests/e2e -v

# Cleanup
docker compose --profile <your-profile> down -v
```

## Docker Compose Profiles

The unified `docker-compose.yml` uses profiles to organize services:

| Profile | Services | Use Case |
|---------|----------|----------|
| (default) | bitcoin, miner, directory, orderbook-watcher | Core infrastructure |
| `maker` | + maker | Single maker bot |
| `taker` | + taker | Single taker client |
| `e2e` | + maker1, maker2, wallet-funder | E2E tests (our implementation) |
| `reference` | + tor, jam, bitcoin-jam, maker1, maker2 | Reference JAM compatibility |
| `neutrino` | + neutrino, maker-neutrino, wallet-funder | Light client testing |
| `reference-maker` | + jam-maker1, jam-maker2 | Reference makers (rarely needed) |

### Note: Neutrino + Reference Profile Interaction

When `neutrino` and `reference` profiles run together, the **neutrino maker** advertises
offers to the directory server. The **reference taker (JAM)** may pick up these offers,
but the neutrino maker cannot verify JAM's UTXOs (legacy PoDLE format lacks extended
metadata). The neutrino maker correctly detects this incompatibility and sends `!error`
back to the taker. The coinjoin still succeeds because enough regular makers (maker1,
maker2) are available.

This interaction is tested by `test_neutrino_maker_reference_taker.py`.

## Test Suites

### 1. E2E Tests (Our Implementation)

Tests our maker/taker implementation without any reference components.

```bash
# Clean start
docker compose --profile e2e down -v

# Start services
docker compose --profile e2e up -d --build

# Wait for services
echo "Waiting for Bitcoin..."
until docker compose exec -T bitcoin bitcoin-cli -chain=regtest \
    -rpcport=18443 -rpcuser=test -rpcpassword=test getblockchaininfo 2>/dev/null; do
  sleep 2
done

echo "Waiting for wallet funding..."
sleep 30

# Restart makers to sync latest blockchain state
docker compose restart maker1 maker2
sleep 10

# Run tests
pytest tests/e2e/test_complete_system.py -v -s

# Cleanup
docker compose --profile e2e down -v
```

### 2. Reference Compatibility Tests

Tests our makers with the reference JoinMarket taker (JAM).

**Note:** Some tests (e.g., `test_bond_reference_validation.py`) require the reference
implementation to be installed for validation purposes:

```bash
# Clone reference implementation (one-time setup)
git clone --depth 1 https://github.com/JoinMarket-Org/joinmarket-clientserver.git

# Install system dependencies (Ubuntu/Debian)
sudo apt-get install -y libsecp256k1-dev

# Install Python dependencies (base + jmbitcoin + jmclient from reference implementation)
# Note: Using latest klein/werkzeug for Python 3.14 compatibility
pip install \
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

# Note: Full installation with pip install -e may fail on Python 3.14 due to version constraints,
# but the deps above are sufficient for bond validation tests
```

To run the tests:

```bash
# Clean start
docker compose --profile reference down -v

# Start services (includes Tor for onion routing)
docker compose --profile reference up -d --build

# Wait for Bitcoin
echo "Waiting for Bitcoin..."
until docker compose exec -T bitcoin bitcoin-cli -chain=regtest \
    -rpcport=18443 -rpcuser=test -rpcpassword=test getblockchaininfo 2>/dev/null; do
  sleep 2
done

# Wait for Tor hidden service
echo "Waiting for Tor..."
until docker compose exec -T tor cat /var/lib/tor/directory/hostname 2>/dev/null | grep -q ".onion"; do
  sleep 2
done

echo "Waiting for wallet funding and JAM startup..."
sleep 60

# Restart makers to sync latest blockchain state
docker compose restart maker1 maker2
sleep 20

# Run reference tests
pytest tests/e2e/test_reference_coinjoin.py tests/e2e/test_our_maker_reference_taker.py -v -s

# Cleanup
docker compose --profile reference down -v
```

### 3. Neutrino Backend Tests

Tests the BIP157/158 light client backend.

```bash
# Clean start
docker compose --profile neutrino down -v

# Start services
docker compose --profile neutrino up -d --build

# Wait for Bitcoin
echo "Waiting for Bitcoin..."
until docker compose exec -T bitcoin bitcoin-cli -chain=regtest \
    -rpcport=18443 -rpcuser=test -rpcpassword=test getblockchaininfo 2>/dev/null; do
  sleep 2
done

# Wait for Neutrino to sync
echo "Waiting for Neutrino..."
until curl -s http://localhost:8335/v1/status 2>/dev/null | grep -q '"synced":true'; do
  echo "  Neutrino syncing..."
  sleep 5
done
echo "Neutrino synced!"

# Run neutrino tests
# Note: Some tests require BOTH neutrino AND e2e profiles
pytest tests/e2e/test_neutrino_backend.py -m "neutrino and not slow" -v  # Basic tests
pytest tests/e2e/test_neutrino_backend.py::TestNeutrinoCoinJoin::test_coinjoin_with_neutrino_maker -m slow -v  # CoinJoin with neutrino maker

# For neutrino taker test, also start e2e profile for Bitcoin Core makers:
docker compose --profile e2e up -d
sleep 10
pytest tests/e2e/test_neutrino_backend.py::TestNeutrinoCoinJoin::test_coinjoin_with_neutrino_taker -m slow -v

# Cleanup
docker compose --profile neutrino --profile e2e down -v
```

### 4. Full Test Suite (All Unit + Integration)

Run all tests including unit tests for each component:

```bash
# Start e2e profile for integration tests
docker compose --profile e2e down -v
docker compose --profile e2e up -d --build
sleep 30
docker compose restart maker1 maker2
sleep 10

# Run complete test suite
pytest -lv \
  --cov=jmcore --cov=jmwallet --cov=directory_server \
  --cov=orderbook_watcher --cov=maker --cov=taker \
  jmcore orderbook_watcher directory_server jmwallet maker taker tests/e2e/test_complete_system.py

# Cleanup
docker compose --profile e2e down -v
```

### 5. Reference Maker Tests (Our Taker + JAM Makers)

Tests our taker implementation against reference JoinMarket (JAM) makers.

```bash
# Clean start
docker compose --profile reference-maker down -v

# Start services (includes Tor for onion routing)
docker compose --profile reference-maker up -d --build

# Wait for Bitcoin
echo "Waiting for Bitcoin..."
until docker compose exec -T bitcoin bitcoin-cli -chain=regtest \
    -rpcport=18443 -rpcuser=test -rpcpassword=test getblockchaininfo 2>/dev/null; do
  sleep 2
done

echo "Waiting for JAM makers to start..."
sleep 60

# Run reference maker tests
pytest tests/e2e/test_reference_maker_our_taker.py -v -s -m reference_maker

# Cleanup
docker compose --profile reference-maker down -v
```

**Important Note - PoDLE Blacklisting:**

The test fixture automatically clears PoDLE commitment blacklists before running.
This is necessary because:
- PoDLE commitments are blacklisted after first use (anti-sybil protection)
- Without clearing, subsequent test runs with the same UTXO fail
- The blacklist file is at `/root/.joinmarket-ng/cmtdata/commitmentlist` in JAM containers

## Running Specific Tests

### Using Pytest Markers (Recommended)

The e2e tests use pytest markers to organize tests by Docker profile. By default,
running `pytest` excludes all Docker-dependent tests:

```bash
# Run only unit tests (no Docker needed) - this is the default
pytest jmcore directory_server orderbook_watcher maker taker

# Run e2e tests (requires: docker compose --profile e2e up -d)
pytest -m e2e -v

# Run reference tests (requires: docker compose --profile reference up -d)
pytest -m reference -v

# Run neutrino tests (requires: docker compose --profile neutrino up -d)
pytest -m neutrino -v

# Run reference-maker tests (requires: docker compose --profile reference-maker up -d)
pytest -m reference_maker -v

# Run ALL Docker tests (combines all profiles - NOT recommended)
pytest -m docker -v

# Run all tests including Docker (override default exclusion)
pytest -m "" -v
```

### Available Markers

The following pytest markers are configured in `pytest.ini`:

| Marker | Docker Profile | Description |
|--------|----------------|-------------|
| `slow` | - | Marks tests as slow (deselect with `-m "not slow"`) |
| `docker` | Any | Marks tests requiring any Docker services (auto-excluded by default) |
| `e2e` | `e2e` | Marks tests requiring Docker e2e profile (our implementation only) |
| `reference` | `reference` | Marks tests requiring Docker reference profile (JAM compatibility) |
| `neutrino` | `neutrino` | Marks tests requiring Docker neutrino profile (light client) |
| `reference_maker` | `reference-maker` | Marks tests requiring Docker reference-maker profile |

Additional markers from pytest plugins:
- `anyio`: Mark coroutine function test to be run asynchronously via anyio
- `asyncio`: Mark test as a coroutine, runs using an asyncio event loop
- `timeout(timeout, ...)`: Set timeout for a single test (see pytest-timeout docs)
- `no_cover`: Disable coverage for this test
- `filterwarnings(warning)`: Add a warning filter to the test
- `skip(reason=None)`: Skip test with optional reason
- `skipif(condition, *, reason=...)`: Skip test if condition is True
- `xfail(condition, *, reason=..., run=True, raises=None, strict=False)`: Mark test as expected failure
- `parametrize(argnames, argvalues)`: Call test multiple times with different arguments
- `usefixtures(fixturename1, ...)`: Mark tests as needing specified fixtures

### Running Specific Test Files

```bash
# Single test file
pytest tests/e2e/test_complete_system.py -v -m e2e

# Single test function
pytest tests/e2e/test_reference_coinjoin.py::test_execute_reference_coinjoin -v -s -m reference

# With timeout override
pytest -m reference -v --timeout=600

# Skip slow tests
pytest -m "e2e and not slow" -v
```

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                     JoinMarket Test System                            │
├──────────────────────────────────────────────────────────────────────┤
│                                                                       │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐            │
│  │   Bitcoin    │◄───│  Directory   │◄───│   Orderbook  │            │
│  │   Regtest    │    │   Server     │    │   Watcher    │            │
│  └──────────────┘    └──────────────┘    └──────────────┘            │
│         ▲                    ▲                                        │
│         │            ┌───────┴────────┐                               │
│         │            │                 │                               │
│  ┌──────┴──────┐  ┌─▼──────────┐  ┌──▼────────┐                       │
│  │   Miner     │  │  Maker 1   │  │  Maker 2  │                       │
│  │  (auto)     │  └────────────┘  └───────────┘                       │
│  └─────────────┘                                                      │
│                                                                       │
│  Neutrino Profile (run separately!):                                  │
│  ┌──────────────┐    ┌──────────────────┐                             │
│  │   Neutrino   │◄───│  Maker-Neutrino  │                             │
│  │   Server     │    │  (light client)  │                             │
│  └──────────────┘    └──────────────────┘                             │
│                                                                       │
│  Reference Profile (run separately!):                                 │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐            │
│  │ Bitcoin-JAM  │◄───│     Tor      │───►│     JAM      │            │
│  │  (legacy)    │    │   (.onion)   │    │  (Reference) │            │
│  └──────────────┘    └──────────────┘    └──────────────┘            │
│                                                                       │
└──────────────────────────────────────────────────────────────────────┘
```

## Tor Hidden Service Keys

The reference tests use a Tor hidden service to expose the directory server. The
onion address is **generated dynamically** at container startup to avoid conflicts
when multiple instances run simultaneously (e.g., CI pipelines + developer machines).

### How It Works

1. **Default (Local Development)**: Tor generates a random onion address each time
2. **CI (GitHub Actions)**: Uses a unique key from `TOR_HS_ED25519_SECRET_KEY_BASE64` secret
3. **Dynamic Discovery**: Tests automatically discover the onion address from the Tor container

### Setting Up CI Secrets

For GitHub Actions, generate a unique key and add it as a repository secret:

```bash
# Generate Tor hidden service keys
pip install cryptography  # or: pip install pynacl
python scripts/generate_tor_keys.py ./ci-tor-keys

# The script outputs the base64-encoded key to set as a secret:
# export TOR_HS_ED25519_SECRET_KEY_BASE64='...'

# Add to GitHub:
# Settings > Secrets and variables > Actions > New repository secret
# Name: TOR_HS_ED25519_SECRET_KEY_BASE64
# Value: (the base64 string from the script output)
```

### Using a Persistent Key Locally

If you want the same onion address across restarts (e.g., for debugging):

```bash
# Generate keys once
python scripts/generate_tor_keys.py ~/.joinmarket-ng-tor-keys

# Export the secret key before starting Docker
export TOR_HS_ED25519_SECRET_KEY_BASE64=$(base64 -w0 ~/.joinmarket-ng-tor-keys/hs_ed25519_secret_key)

# Start services (will use your key)
docker compose --profile reference up -d
```

Or add to a `.env` file (not committed to git):

```bash
# .env (gitignored)
TOR_HS_ED25519_SECRET_KEY_BASE64=<your-base64-key>
```

## Test Wallets

Pre-configured test wallet mnemonics (regtest only!):

| Wallet | Mnemonic |
|--------|----------|
| Maker 1 | `avoid whisper mesh corn already blur sudden fine planet chicken hover sniff` |
| Maker 2 | `minute faint grape plate stock mercy tent world space opera apple rocket` |
| Maker-Neutrino | `ice index boss season jealous supreme nephew kit cool lock caught enter` |
| Taker | `burden notable love elephant orbit couch message galaxy elevator exile drop toilet` |
| Generic | `abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about` |

## Service URLs

| Service | URL |
|---------|-----|
| Bitcoin RPC | http://localhost:18443 |
| Bitcoin-JAM RPC | http://localhost:18445 (reference profile) |
| Directory Server | localhost:5222 |
| Orderbook Watcher | http://localhost:8080 |
| Neutrino API | http://localhost:8335 |

## Neutrino Backend

The neutrino backend is a BIP157/BIP158 light client that syncs with Bitcoin Core using compact block filters. This provides privacy-preserving SPV operation without downloading the full blockchain.

### Neutrino Status API

Check neutrino sync status:
```bash
curl -s http://localhost:8335/v1/status
# {"synced":true,"block_height":5490,"filter_height":5490,"peers":1}
```

### How Neutrino Works

1. **Compact Block Filters**: Neutrino downloads compact block filters (BIP158) instead of full blocks
2. **Privacy**: Doesn't reveal which addresses you're interested in to peers
3. **Sync**: Syncs headers and filters before reporting as synced
4. **UTXO Discovery**: Uses filter matching to find relevant transactions

### Neutrino Test Requirements

- Bitcoin Core must have `blockfilterindex=1` and `peerblockfilters=1` enabled
- Neutrino needs P2P access to Bitcoin Core on port 18444
- Tests will skip if neutrino is unavailable or not synced

## Troubleshooting

### IMPORTANT: Always Clean Volumes Before Testing

**Docker volumes persist blockchain state between runs.** If you restart services without cleaning volumes, makers will have outdated wallet state and tests will fail with:

```
ERROR: outputs unconfirmed or already spent. utxo_data=[None]
```

**Solution:** Always use `down -v` and restart makers after funding:

```bash
# Clean volumes
docker compose --profile <profile> down -v

# Start fresh
docker compose --profile <profile> up -d --build
sleep 30

# Restart makers to sync latest blockchain
docker compose restart maker1 maker2
sleep 10
```

### Neutrino Maker and Reference Tests

The neutrino maker has its own unique mnemonic and funded wallet. When running alongside
reference tests, the neutrino maker correctly detects incompatible takers and sends
`!error`. Coinjoins succeed because enough regular makers are available.

If reference tests fail with "Makers who didnt respond" or "UTXO not found", the issue
is likely stale wallet state, not the neutrino maker. Try cleaning volumes:

```bash
docker compose --profile reference down -v
docker compose --profile reference up -d --build
```

### Check Service Status

```bash
docker compose --profile <profile> ps
docker compose logs <service-name>
docker compose logs --tail=50 maker1
```

### Neutrino Not Syncing?

1. Check if Bitcoin Core has block filters enabled:
```bash
docker compose exec bitcoin bitcoin-cli -regtest -rpcuser=test -rpcpassword=test getblockchaininfo | grep -A5 filter
```

2. Check neutrino logs:
```bash
docker compose logs neutrino
```

3. Verify neutrino can reach Bitcoin:
```bash
docker compose exec neutrino ping -c 3 jm-bitcoin
```

4. Clear neutrino data and restart:
```bash
docker compose stop neutrino
docker volume rm joinmarket-ng_neutrino-data
docker compose up -d neutrino
```

### Makers Not Seeing UTXOs?

Check if makers synced after wallet funding:

```bash
# Check maker1 balance
docker compose logs maker1 | grep "Total balance"

# Should show ~5900 BTC. If showing old balance, restart:
docker compose restart maker1 maker2
```

### Reference Tests Failing?

1. Make sure JAM is running:
```bash
docker compose --profile reference ps | grep jam
```

2. Check JAM logs:
```bash
docker compose logs jam --tail=100
```

3. Check Tor is bootstrapped:
```bash
docker compose logs tor | grep -i bootstrap
```

4. Verify our makers see the directory:
```bash
docker compose logs maker1 | grep -i "connected\|handshake"
```

### Wallet Has Zero Balance

The auto-miner and test fixtures should fund wallets automatically. If needed:

```bash
ADDR="bcrt1q..."
docker compose exec bitcoin bitcoin-cli -regtest -rpcuser=test -rpcpassword=test generatetoaddress 110 $ADDR
```

### Directory Server Log Noise (Healthcheck Messages)

If you see repeated "Handshake error: Connection closed by peer" messages in directory
logs every 10 seconds, **this is normal!** These come from Docker healthchecks, not
from actual connection issues:

```
INFO     | Handshake error from 172.25.0.1:42356: Connection closed by peer
```

The healthcheck sends a TCP connection to verify the directory server is running,
but doesn't complete the JoinMarket handshake protocol.

### PoDLE Commitment Blacklisted

If makers reject `!fill` messages with "Commitment is blacklisted":

```
PRIVMSG routing: J58xxx -> J5Bxxx (rest: error Commitment is blacklisted: 1071cb...)
```

This means the PoDLE commitment was already used in a previous coinjoin attempt.
Solutions:
1. **Clear the blacklist** (for testing):
   ```bash
   docker compose exec -T jam-maker1 rm -f /root/.joinmarket-ng/cmtdata/commitmentlist
   docker compose exec -T jam-maker2 rm -f /root/.joinmarket-ng/cmtdata/commitmentlist
   ```
2. **Fund a fresh address** to get a new UTXO with an unused commitment
3. **Use a different NUMS index** (the taker can rotate through indices 0-15)

## CI/CD

The GitHub Actions workflow runs tests in separate jobs using pytest markers:

| Job | Profile | Marker | Description |
|-----|---------|--------|-------------|
| `test-jmcore`, `test-taker`, etc. | None | `-m "not docker"` (default) | Unit tests (no Docker) |
| `test-e2e` | `e2e` | `-m e2e` | Our implementation |
| `test-reference` | `reference` | `-m reference` | JAM compatibility |
| `test-neutrino` | `neutrino` | `-m neutrino` | Light client tests |
| `test-reference-maker` | `reference-maker` | `-m reference_maker` | JAM makers + our taker |

**Marker Details:**
- `slow`: Long-running tests, can be excluded with `-m "not slow"`
- `docker`: Auto-excluded by default via `pytest.ini` (`-m "not docker"`)
- `e2e`: Requires e2e Docker profile (our maker/taker implementation)
- `reference`: Requires reference Docker profile (JAM compatibility tests)
- `neutrino`: Requires neutrino Docker profile (BIP157/158 light client)
- `reference_maker`: Requires reference-maker profile (reference makers + our taker)

**Key Points:**
- Unit tests run by default without Docker (markers auto-exclude Docker tests)
- Docker tests use profile-specific markers to match the running services
- All markers are defined in `pytest.ini`

### Fail-on-Skip Mode

**IMPORTANT:** All CI jobs and the `run_all_tests.sh` script use `--fail-on-skip` mode.

This means that if a test is selected by its marker but then skips due to missing
setup (e.g., Docker service not running), the test **fails instead of skipping**.
This catches configuration issues where tests appear to pass but are actually
not running at all.

```bash
# Example: If e2e profile is not running, this will FAIL (not skip)
pytest -m e2e --fail-on-skip

# Without --fail-on-skip, tests silently skip (bad for CI)
pytest -m e2e  # Tests skip if Docker not running - looks like success!
```

The `--fail-on-skip` option is implemented in `tests/e2e/conftest.py` and converts
any pytest.skip() calls into pytest.fail() when enabled.

### Running the Complete Test Suite

Use `scripts/run_all_tests.sh` to run all test phases with proper Docker orchestration:

```bash
# Run complete test suite (unit + e2e + reference + neutrino)
./scripts/run_all_tests.sh

# Just cleanup Docker resources
./scripts/run_all_tests.sh --cleanup-only

# Show help
./scripts/run_all_tests.sh --help
```

The script:
1. Runs unit tests (no Docker)
2. Starts e2e profile and runs e2e tests + Docker integration tests
3. Starts reference profile and runs compatibility tests
4. Starts reference-maker profile and runs maker tests
5. Starts neutrino profile and runs light client tests
6. Combines coverage reports from all phases

All phases use `--fail-on-skip` to ensure tests don't silently skip.

See `.github/workflows/test.yaml` for CI implementation details.

## Security Notes

**These are development/test environments only!**

- Never use on mainnet
- Never use real mnemonics
- Never store real funds
- Only for testing on regtest

## Useful Debugging Commands

```bash
# View raw transaction details
docker exec -it jm-bitcoin bitcoin-cli -chain=regtest -rpcport=18443 -rpcuser=test -rpcpassword=test \
  getrawtransaction <txid> true
```

---

**Status:** E2E tests fully automated
