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

**Verify a release:**

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

**Sign a release:**

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
