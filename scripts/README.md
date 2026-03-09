# Scripts

Utility scripts for JoinMarket NG development and operations.

## Available Scripts

### Development & Operations

- **build_docs.py** - Generate API documentation from source code
- **bump_version.py** - Bump the project version across all components
- **coinjoin_notifier.py** - Monitor and notify about CoinJoin events
- **fidelity_bond_tool.py** - Fetch, parse, and analyze fidelity bond proofs from mainnet makers
- **fund-test-wallets.sh** - Fund regtest wallets for testing
- **generate_changelog.py** - Generate changelog entries from git history
- **generate_tor_keys.py** - Generate Tor hidden service keys
- **regtest-miner-jam.sh** - Run Bitcoin Core regtest miner for JAM compatibility testing
- **regtest-miner.sh** - Run Bitcoin Core regtest miner
- **run_all_tests.sh** - Execute complete test suite including Docker-based e2e tests
- **sign-release.sh** - Sign a release tarball
- **update_readme_help.py** - Update README files with CLI command help sections (run manually when CLI changes)
- **update-base-images.sh** - Update Docker base image digests
- **update-deps.sh** - Update project dependencies
- **verify-release.sh** - Verify a signed release tarball

### Fidelity Bond Cold Storage

These scripts support the cold storage fidelity bond workflow. See [`docs/technical/privacy.md`](../docs/technical/privacy.md) for the full guide.

- **sign_bond_psbt.py** - Sign a fidelity bond spending PSBT using a hardware wallet (via HWI). Supports Ledger and Jade; Trezor/Coldcard/BitBox02/KeepKey cannot sign CLTV scripts.

- **sign_bond_mnemonic.py** - Sign a fidelity bond spending PSBT using a BIP39 mnemonic. Use when hardware wallet signing is not available. Reads the mnemonic interactively (hidden input) and outputs a fully signed raw transaction.

- **sign_bond_cert_reference.py** - Sign a fidelity bond certificate using a BIP39 mnemonic (for migration from the reference implementation). Derives the private key at `m/84'/0'/0'/2/<timenumber>` and signs the certificate in Electrum recoverable format accepted by `jm-wallet import-certificate`. Use this instead of `wallet-tool.py signmessage`, which has a bug preventing it from signing with fidelity bond paths.

- **derive_bond_pubkey.py** - Derive the fidelity bond public key from the reference JoinMarket implementation's xpub (shown by `wallet-tool.py display`). Accepts the account xpub (`fbonds-mpk-` line) or the `/2` branch xpub and a locktime (YYYY-MM), then outputs the public key and the exact `create-bond-address` command to run. Only requires `coincurve`.

## Documentation

For full documentation, see [JoinMarket NG Documentation](https://joinmarket-ng.github.io/joinmarket-ng/).
