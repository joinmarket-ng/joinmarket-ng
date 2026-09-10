# Scripts

Utility scripts for JoinMarket NG development and operations.

## Available Scripts

### Development & Operations

- **build_docs.py** - Reproduce `.github/workflows/properdocs-pages.yml` locally (install docs deps + editable packages, then run `properdocs build --strict -f properdocs.yml`)
- **bump_version.py** - Bump the project version across all components
- **coinjoin_notifier.py** - Monitor and notify about CoinJoin events
- **diagnose_maker.py** - Discover a maker's onion endpoint, compare directory and direct offers, and analyze any fidelity bond proofs
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

### Maker Diagnostics

`diagnose_maker.py` runs an automatic diagnostic through Tor at `127.0.0.1:9050`:

1. Connect to the directory and negotiate nick ownership authentication.
2. Discover and print the maker's onion endpoint and advertised features.
3. Request the orderbook through the directory and display the maker's signed offers.
4. Connect directly to the maker's onion, request the orderbook again, and display those offers separately.
5. Parse and check both signatures of any fidelity bond proofs received on either path.

Offers are displayed even without a fidelity bond. Rejected messages are logged
with their contents and reasons; they do not count as verified offers.
If the direct endpoint responds under a different nick with a valid signature,
the diagnostic warns that the requested nick may be stale and shows the responding
nick. It keeps filtering for the requested identity; `--maker-address` overrides
only the endpoint, not the expected nick.
The network defaults to mainnet and must match the directory. `--network signet`
selects a signet default directory when `--directory` is omitted. Explicit
`--directory onion:port` ports are used as supplied; bare directory onions use 5222.

```bash
# Diagnose through the default signet directory and the discovered maker onion
python scripts/diagnose_maker.py <maker_nick> --network signet

# Select a directory and save a JSON report
python scripts/diagnose_maker.py <maker_nick> --network signet --output diagnosis.json \
  --directory signetvaxgd3ivj4tml4g6ed3samaa2rscre2gyeyohncmwk4fbesiqd.onion:5222

# Supply a known direct endpoint if the directory cannot provide a complete peerlist
python scripts/diagnose_maker.py <maker_nick> --network signet \
  --maker-address <maker_onion:port> --timeout 20 --log-level INFO
```

Loguru logs go to stderr, with DEBUG enabled by default. They include sent
orderbook requests, received directory envelopes and raw direct messages, and
validation failures. Use `--log-level INFO` for progress without message dumps.
The final report goes to stdout; `--output` additionally saves the structured
report, including offer fields and raw bond proofs with their signature analysis.
`--timeout` defaults to 60 seconds per connection/collection phase; peerlist
discovery allows an additional five seconds to finish a chunked response.

The default
`--nick-auth-mode prefer_verified` authenticates when the directory supports it
and permits legacy directories without it. Use `--nick-auth-mode require_verified`
to reject directories without authentication support. A negotiated authentication
failure stops directory requests. `disabled` is available for compatibility
diagnostics. An explicit `--maker-address` still permits an independent direct
check when directory connection or discovery fails.

Exit status is 0 when both paths return verified offers and every received bond
has valid signatures. A missing bond is normal. Exit status 1 means a failed or
incomplete diagnostic, including an unavailable direct endpoint, and 2 means
invalid command arguments. Signature checks do not establish whether a bond UTXO
is unspent or eligible on chain.

This replaces `fidelity_bond_tool.py`. Its `fetch`, `fetch-parse`, and `parse`
subcommands have been removed; supply only the maker nick and connection options.

### Fidelity Bond Cold Storage

These scripts support the cold storage fidelity bond workflow. See [`docs/fidelity-bond-operations.md`](../docs/fidelity-bond-operations.md) for the full guide.

- **sign_bond_psbt.py** - Sign a fidelity bond spending PSBT using a hardware wallet (via HWI, >= 3.1.0 to detect newer device models). Classic Blockstream Jade signing is verified with HWI 3.2.0 and cbor2 5.9.0; cbor2 5.8.0 is blocked because it breaks Jade serial responses. Original Digital BitBox / BitBox01 support is expected but untested; use `--device-type digitalbitbox --device-password` for its required hidden password prompt. Ledger devices require the legacy Bitcoin app (2.0.x and earlier); the current app has been reported to reject bond PSBTs. Trezor/Coldcard/BitBox02/KeepKey cannot sign CLTV scripts.

- **finalize_bond_psbt.py** - Verify and finalize a signed fidelity bond spending PSBT, such as one returned by Specter DIY's QR signing flow or `sign_bond_psbt.py --no-broadcast`. Cryptographically verifies the partial signature (BIP143 SIGHASH_ALL over the CLTV witness script) and builds the final P2WSH witness transaction when Bitcoin Core's `finalizepsbt` cannot finalize the custom witness script. Standard library only, so it also serves as the verification step of the hardware wallet compatibility test in the operations guide.

- **sign_bond_mnemonic.py** - Sign a fidelity bond spending PSBT using a BIP39 mnemonic. Use when hardware wallet signing is not available. Reads and validates the mnemonic interactively (hidden input) and outputs a fully signed raw transaction. Requires the maintained `python-bitcointx` release, native `libsecp256k1`, and `mnemonic`.

- **sign_bond_cert_reference.py** - Sign a fidelity bond certificate using a validated BIP39 mnemonic (for migration from the reference implementation). Derives the private key at `m/84'/0'/0'/2/<timenumber>` and signs the certificate in Electrum recoverable format accepted by `jm-wallet import-certificate`. Use this instead of `wallet-tool.py signmessage`, which has a bug preventing it from signing with fidelity bond paths. Requires the maintained `python-bitcointx` release, native `libsecp256k1`, and `mnemonic`.

- **derive_bond_pubkey.py** - Derive the fidelity bond public key from the reference JoinMarket implementation's xpub (shown by `wallet-tool.py display`). Accepts the account xpub (`fbonds-mpk-` line) or the `/2` branch xpub and a locktime (YYYY-MM), then outputs the public key and the exact `create-bond-address` command to run. Requires the maintained `python-bitcointx` release and native `libsecp256k1`.

## Documentation

For full documentation, see [JoinMarket NG Documentation](https://joinmarket-ng.github.io/joinmarket-ng/).
