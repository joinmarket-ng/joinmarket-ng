# Scripts

Utility scripts for JoinMarket NG development and operations.

## Available Scripts

### Development & Operations

- **build_docs.py** - Reproduce `.github/workflows/properdocs-pages.yml` locally (install docs deps + editable packages, then run `properdocs build --strict -f properdocs.yml`)
- **bump_version.py** - Bump the project version across all components
- **coinjoin_notifier.py** - Monitor and notify about CoinJoin events
- **fidelity_bond_tool.py** - Fetch, parse, and analyze fidelity bond proofs from mainnet makers
- **fund-test-wallets.sh** - Fund regtest wallets for testing
- **generate_changelog.py** - Generate changelog entries from git history
- **config_changelog.py** - Generate or backfill per-release configuration template changes
- **sync_github_release_notes.py** - Preview or apply GitHub release-note updates from `CHANGELOG.md`
- **validate_commit_message.py** - Validate Conventional Commit messages and require `Changelog:` trailers for `feat`/`fix`
- **generate_tor_keys.py** - Generate Tor hidden service keys
- **regtest-miner-jam.sh** - Run Bitcoin Core regtest miner for JAM compatibility testing
- **regtest-miner.sh** - Run Bitcoin Core regtest miner
- **run_parallel_tests.sh** - Execute all test suites (including Docker-based e2e tests) in parallel using Docker Compose project isolation
- **sign-release.sh** - Sign a release manifest (supports local-first and CI-first workflows)
- **update_readme_help.py** - Update generated CLI help in component READMEs (run manually when CLI changes; user guides link to this reference)
- **update-base-images.sh** - Update Docker base image digests
- **update-deps.sh** - Update project dependencies, including the maintained `python-bitcointx` release pin
- **update-flatpak-deps.py** - Update Flatpak sources and pinned JAM Docker dependencies
- **verify-release.sh** - Verify release signatures and optionally reproduce builds
- **build-release.sh** - Build Docker images locally and generate a release manifest for local-first signing

### Fidelity Bond Cold Storage

These scripts support the cold storage fidelity bond workflow. See [`docs/fidelity-bond-operations.md`](../docs/fidelity-bond-operations.md) for the full guide.

- **sign_bond_psbt.py** - Sign a fidelity bond spending PSBT using a hardware wallet (via HWI, >= 3.1.0 to detect newer device models). Classic Blockstream Jade signing is verified with HWI 3.2.0 and cbor2 5.9.0; cbor2 5.8.0 is blocked because it breaks Jade serial responses. Original Digital BitBox / BitBox01 support is expected but untested; use `--device-type digitalbitbox --device-password` for its required hidden password prompt. Ledger devices require the legacy Bitcoin app (2.0.x and earlier); the current app has been reported to reject bond PSBTs. Trezor/Coldcard/BitBox02/KeepKey cannot sign CLTV scripts.

- **finalize_bond_psbt.py** - Verify and finalize a signed fidelity bond spending PSBT, such as one returned by Specter DIY's QR signing flow or `sign_bond_psbt.py --no-broadcast`. Cryptographically verifies the partial signature (BIP143 SIGHASH_ALL over the CLTV witness script) and builds the final P2WSH witness transaction when Bitcoin Core's `finalizepsbt` cannot finalize the custom witness script. Standard library only, so it also serves as the verification step of the hardware wallet compatibility test in the operations guide.

- **sign_bond_mnemonic.py** - Sign a fidelity bond spending PSBT using a BIP39 mnemonic. Use when hardware wallet signing is not available. Reads and validates the mnemonic interactively (hidden input) and outputs a fully signed raw transaction. Requires the maintained `python-bitcointx` release, native `libsecp256k1`, and `mnemonic`.

- **sign_bond_cert_reference.py** - Sign a fidelity bond certificate using a validated BIP39 mnemonic (for migration from the reference implementation). Derives the private key at `m/84'/0'/0'/2/<timenumber>` and signs the certificate in Electrum recoverable format accepted by `jm-wallet import-certificate`. Use this instead of `wallet-tool.py signmessage`, which has a bug preventing it from signing with fidelity bond paths. Requires the maintained `python-bitcointx` release, native `libsecp256k1`, and `mnemonic`.

- **derive_bond_pubkey.py** - Derive the fidelity bond public key from the reference JoinMarket implementation's xpub (shown by `wallet-tool.py display`). Accepts the account xpub (`fbonds-mpk-` line) or the `/2` branch xpub and a locktime (YYYY-MM), then outputs the public key and the exact `create-bond-address` command to run. Requires the maintained `python-bitcointx` release and native `libsecp256k1`.

## Documentation

For full documentation, see [JoinMarket NG Documentation](https://joinmarket-ng.github.io/joinmarket-ng/).
