# Development

## Local Setup

From repository root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip

for d in jmcore jmwallet maker taker directory_server orderbook_watcher jmwalletd; do
  python -m pip install -e "./${d}[dev]"
done
```

## Tests

Fast unit test run:

```bash
pytest jmcore directory_server orderbook_watcher maker taker jmwallet
```

Full orchestrated suite (unit + Docker-backed phases):

```bash
./scripts/run_all_tests.sh
```

When selecting Docker-marked tests manually, use `--fail-on-skip`.

## Lint / Format / Type Check

Preferred:

```bash
prek run --all-files
```

Fallback:

```bash
pre-commit run --all-files
```

## Documentation

Build docs locally from repository root:

```bash
python scripts/build_docs.py
```

What this does:

- installs docs dependencies from `requirements-docs.txt`
- installs editable project packages used by API doc generation
- runs `properdocs build -q -f properdocs.yml` and writes output to `site/`

If you want to run the steps manually:

```bash
python -m pip install -r requirements-docs.txt
python -m pip install -e jmcore -e jmwallet -e taker -e maker -e directory_server -e orderbook_watcher
python -m properdocs build -q -f properdocs.yml
```

For local preview:

```bash
python -m properdocs serve -q -f properdocs.yml
```

## Reference Compatibility Tests

Some e2e tests require a local clone of the reference implementation at repository root:

```bash
git clone --depth 1 https://github.com/JoinMarket-Org/joinmarket-clientserver.git
```

Then run marker-specific tests as needed (`-m reference`, `-m reference_maker`).

## Releases and Signatures

Reproducible release verification and signing workflows:

- build locally: `scripts/build-release.sh`
- sign: `scripts/sign-release.sh`
- verify: `scripts/verify-release.sh`

See [Signatures](../README-signatures.md) for repository signature layout.

### Local-First Workflow (Recommended for Release Managers)

Build, sign, and then let CI verify independently. No waiting for CI.

```bash
# 1. Bump version (creates commit + tag locally)
python scripts/bump_version.py patch --no-push

# 2. Build images locally and generate manifest
./scripts/build-release.sh

# 3. Sign the locally-built manifest
./scripts/sign-release.sh --manifest release-manifest-<version>.txt --key <fingerprint>

# 4. Push to trigger CI
git push && git push --tags
```

CI will build the same images independently and verify its layer digests
match your signed local manifest. The release is confirmed reproducible
when CI passes.

Note: strict layer-digest matching is currently skipped for `jam-ng` because
the CRA/webpack frontend build is non-deterministic across environments.

### CI-First Workflow (For Additional Signers)

Wait for CI to complete, then reproduce and sign.

```bash
# After CI release is complete:
./scripts/sign-release.sh <version> --key <fingerprint>
```

This downloads the manifest, rebuilds locally, and signs if digests match.
The same `jam-ng` skip rule applies for strict layer matching.

## Verify a Release

```bash
./scripts/verify-release.sh <version>

# with local reproduction check
./scripts/verify-release.sh <version> --reproduce
```

Reproduction uses Dockerfiles from the release commit to ensure strict
historical accuracy.

## Sign a Release

```bash
# Sign a CI-built release (downloads manifest, reproduces, signs)
./scripts/sign-release.sh <version> --key <gpg-key-id>

# Sign a locally-built manifest (from build-release.sh)
./scripts/sign-release.sh <version> --manifest release-manifest-<version>.txt --key <gpg-key-id>
```

For the local-first workflow, the manifest must come from the same release
commit as the local tag created by `bump_version.py`. `sign-release.sh` refuses
to sign a local manifest if its embedded `commit:` does not match the release
tag (or `HEAD` when no local tag exists).
