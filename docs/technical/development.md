### Dependency Management

Using [pip-tools](https://github.com/jazzband/pip-tools) for pinned dependencies:

```bash
pip install pip-tools

# Update pinned dependencies
cd jmcore
python -m piptools compile -Uv pyproject.toml -o requirements.txt
```

Install order: `jmcore` -> `jmwallet` -> other packages

### Running Tests

```bash
# Unit tests with coverage
pytest -lv \
  --cov=jmcore --cov=jmwallet --cov=directory_server \
  --cov=orderbook_watcher --cov=maker --cov=taker \
  jmcore orderbook_watcher directory_server jmwallet maker taker tests

# E2E tests (requires Docker)
./scripts/run_all_tests.sh
```

Test markers:

- Default: `-m "not docker"` excludes Docker tests
- `e2e`: Our maker/taker implementation
- `reference`: JAM compatibility tests
- `neutrino`: Light client tests

### Reproducible Builds

Docker images are built reproducibly using `SOURCE_DATE_EPOCH` to ensure identical builds from the same source code. This allows independent verification that released binaries match the source.

**How it works:**

- `SOURCE_DATE_EPOCH` is set to the git commit timestamp
- All platforms (amd64, arm64, armv7) are built with the same timestamp
- Per-platform layer digests are stored in the release manifest
- Verification compares layer digests (not manifest digests) for reliability
- Apt packages are pinned to exact versions to prevent drift between build and verification
- Python build tools (setuptools, wheel) are pinned via `PIP_CONSTRAINT` in Dockerfiles to prevent version stamps in WHEEL metadata from changing between build and verification
- Python dependencies are locked with hash verification via `pip-compile --generate-hashes`
- Base images are pinned by digest (updated via `./scripts/update-base-images.sh`)

**Why layer digests?**

Docker manifest digests vary based on manifest format (Docker distribution vs OCI) even for identical image content. CI pushes to a registry using Docker format, while local builds typically use OCI format. Layer digests are content-addressable hashes of the actual tar.gz layer content and are identical regardless of manifest format, making them reliable for reproducibility verification.

**Verify a release:**{ #verify-a-release }

```bash
# Check GPG signatures and published image digests
./scripts/verify-release.sh 1.0.0

# Full verification: signatures + published digests + reproduce build locally
./scripts/verify-release.sh 1.0.0 --reproduce

# Require multiple signatures
./scripts/verify-release.sh 1.0.0 --min-sigs 2
```

The `--reproduce` flag builds the Docker image for your current architecture and compares layer digests against the release manifest. This verifies the released image content matches the source code. Cross-platform builds via QEMU are not supported for verification because QEMU emulation produces different layer digests than native builds.

**BuildKit requirements:**

The `--reproduce` flag requires a Docker buildx builder with the `docker-container` driver to support OCI export format. The scripts will automatically create one if needed, but you can also set it up manually:

```bash
# Create a buildx builder with docker-container driver
docker buildx create --name jmng-verify --driver docker-container --use --bootstrap

# Verify the driver
docker buildx inspect  # Should show: Driver: docker-container
```

Alternatively, if using Docker Desktop, enable the "containerd image store" in Settings > Features in development.

**Sign a release:**{ #sign-a-release }

```bash
# Verify + reproduce build + sign (--reproduce is enabled by default)
./scripts/sign-release.sh 1.0.0 --key YOUR_GPG_KEY

# Skip reproduce check (not recommended)
./scripts/sign-release.sh 1.0.0 --key YOUR_GPG_KEY --no-reproduce
```

All signers should use `--reproduce` to verify builds are reproducible before signing. Multiple signatures only add value if each signer independently verifies reproducibility.

**Build locally (manual):**

```bash
VERSION=1.0.0
git checkout $VERSION
SOURCE_DATE_EPOCH=$(git log -1 --pretty=%ct)

# Build for your architecture as OCI tar
docker buildx build \
  --file ./maker/Dockerfile \
  --build-arg SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH \
  --platform linux/amd64 \
  --output type=oci,dest=maker.tar \
  .

# Extract layer digests from OCI tar
mkdir -p oci && tar -xf maker.tar -C oci
manifest_digest=$(jq -r '.manifests[0].digest' oci/index.json)
jq -r '.layers[].digest' "oci/blobs/sha256/${manifest_digest#sha256:}" | sort
```

**Release manifest format:**

The release manifest (`release-manifest-<version>.txt`) contains:

```
commit: <git-sha>
source_date_epoch: <timestamp>

## Docker Images
maker-manifest: sha256:...    # Registry manifest list digest
taker-manifest: sha256:...

## Per-Platform Layer Digests (for reproducibility verification)

### maker-amd64-layers
sha256:abc123...
sha256:def456...

### maker-arm64-layers
sha256:abc123...
sha256:ghi789...
```
Signatures are stored in `signatures/<version>/<fingerprint>.sig`.

### Manual Testing: Swap Input

The following workflow lets you manually test the `--swap-input` flow end-to-end in the local regtest Docker Compose stack — from starting the stack to paying the Lightning invoice and watching the CoinJoin complete.

#### Prerequisites

- Docker and Docker Compose installed
- Working directory: repository root

#### Step 1 — Start the e2e stack

```bash
docker compose --profile e2e up -d
```

This starts: `bitcoin`, `miner`, `directory`, `directory2`, `maker1/2/3`, `wallet-funder`, `lnd-taker`, `lnd-setup`, `electrs`, `nostr-relay`, `electrum-swap-server`, `orderbook-watcher`, `tor`, `jmwalletd`, `jam-playwright`.

Wait ~90 seconds for `lnd-setup` to finish funding the Lightning channel between LND-taker and Electrum's built-in LN node, and for the Electrum swap server to announce its offer on the Nostr relay.

```bash
# Watch until electrum-swap-server shows "(healthy)"
docker compose --profile e2e ps
```

#### Step 2 — Build the taker image from local source

```bash
docker compose build taker
```

#### Step 3 — Run the taker with swap input

The regtest stack has 3 makers with minsizes of 2.5M, 3M, and 3.75M sats. Use `--amount` of at least **4,000,000 sats** and `--counterparties 3`.

The taker wallet mnemonic (`burden notable love elephant orbit couch message galaxy elevator exile drop toilet`) is funded by `wallet-funder` with ~1,400 BTC.

```bash
docker compose --profile taker run --rm \
  -e MNEMONIC="burden notable love elephant orbit couch message galaxy elevator exile drop toilet" \
  -e NETWORK_CONFIG__NETWORK=testnet \
  -e NETWORK_CONFIG__BITCOIN_NETWORK=regtest \
  -e BITCOIN__BACKEND_TYPE=descriptor_wallet \
  -e BITCOIN__RPC_URL=http://jm-bitcoin:18443 \
  -e BITCOIN__RPC_USER=test \
  -e BITCOIN__RPC_PASSWORD=test \
  -e TAKER__MINIMUM_MAKERS=2 \
  -e TAKER__MAX_CJ_FEE_REL=0.01 \
  -e TAKER__MAX_CJ_FEE_ABS=10000 \
  -e SWAP__NOSTR_RELAYS='["ws://jm-nostr-relay:7000"]' \
  taker \
  jm-taker coinjoin \
    --amount 4000000 \
    --destination INTERNAL \
    --counterparties 3 \
    --swap-input \
    --directory jm-directory:5222 \
    --yes \
    --log-level INFO
```

> **Note:** Tor warnings like `[Errno 111] Connect call failed ('127.0.0.1', 9050)` are harmless -- makers are reached via directory routing inside `jm-network`.

#### Step 4 — Watch completion

The taker discovers the Electrum swap server via Nostr kind 30315 offer events on the relay, negotiates the swap via NIP-04 encrypted DMs (kind 25582), pays the Lightning invoice via LND-taker, and monitors for the lockup UTXO on-chain. Once the lockup is detected, it constructs and broadcasts the CoinJoin. The `miner` service mines every 10 seconds so confirmation is automatic.

#### Why these parameters?

| Parameter | Value | Reason |
|-----------|-------|--------|
| `--amount` | >= 4,000,000 sats | Maker minsizes are 2.5M / 3M / 3.75M sats; smaller amounts produce "0 eligible offers" |
| `--counterparties` | 3 | Only 3 makers run in regtest; default of 10 causes "need 10, found 3" error |
| `SWAP__NOSTR_RELAYS` | `["ws://jm-nostr-relay:7000"]` | Points to the regtest Nostr relay for swap server discovery |
| `--directory` | `jm-directory:5222` | Regtest directory server inside `jm-network` |
| `TAKER__MINIMUM_MAKERS` | 2 | Allows the run to succeed even if one maker drops |

#### Swap amount padding

With `--amount 4000000` and 3 makers, the actual swap-needed amount (maker_fees + tx_fee + fake_fee) is typically around 2,000-3,000 sats. Since this is below the swap provider's minimum of 20,000 sats, the amount is automatically padded up.

The extra sats from padding are distributed across maker fees via the fee equalization algorithm, ensuring that **no leftover sats leak to the taker's change output**. This is critical for privacy -- if the taker's change received more than any maker fee in the orderbook, an observer could identify it as the taker.

To test with a larger, more realistic swap amount where padding is not needed, use `--amount 50000000` (0.5 BTC). With higher CoinJoin amounts, maker fees scale up (relative fee offers charge a percentage), and the swap-needed amount may exceed the 20k minimum naturally.

### Troubleshooting

**Wallet Sync Issues:**

```bash
# List wallets
bitcoin-cli listwallets

# Check balance
bitcoin-cli -rpcwallet="jm_xxx_mainnet" getbalance

# Manual rescan
bitcoin-cli -rpcwallet="jm_xxx_mainnet" rescanblockchain 900000

# Check progress
bitcoin-cli -rpcwallet="jm_xxx_mainnet" getwalletinfo
```

| Symptom | Cause | Solution |
|---------|-------|----------|
| First sync times out | Initial descriptor import | Wait and retry |
| Second sync hangs | Concurrent rescan running | Check getwalletinfo |
| Missing transactions | Scan started too late | rescanblockchain earlier |
| Wrong balance | BIP39 passphrase mismatch | Verify passphrase |

**Smart Scan Configuration:**

```toml
[wallet]
scan_lookback_blocks = 12960  # ~3 months
# Or explicit start:
scan_start_height = 870000
```

**RPC Timeout:**

1. Check Core is synced: `bitcoin-cli getblockchaininfo`
2. Increase timeout: `rpcservertimeout=120` in bitcoin.conf
3. First scan may take minutes - retry after completion

---
