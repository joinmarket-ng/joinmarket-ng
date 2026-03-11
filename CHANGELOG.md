# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **Hardware wallet signature rejection in `import-certificate`**: The cold storage fidelity bond `import-certificate` command rejected signatures from segwit hardware wallets (via Sparrow). Hardware wallets encode the address type in the signature header byte per the extended Electrum format (35-38 for P2SH-P2WPKH, 39-42 for P2WPKH), but the verification code only handled legacy P2PKH header bytes (27-34). Extended `_verify_recoverable_signature()` to accept all four Electrum ranges (27-42).
- **Docker image reproducibility broken by setuptools version drift**: Fixed reproducibility of Docker images (`directory-server`, `maker`, `taker`, `orderbook-watcher`, `jmwalletd`) which broke when setuptools 82.0.1 was released on PyPI. The root cause: pip's `--constraint` flag only applies to install dependencies, not to PEP 517 build isolation environments. When building our packages from source, pip would download the latest setuptools, which stamps its version into `WHEEL` metadata (`Generator: setuptools (x.y.z)`), producing different layer digests. Fixed using `--build-constraint` on the relevant `pip install` steps. The `verify-release.sh` script now overlays current Dockerfiles onto the release worktree so reproducibility fixes apply retroactively to past releases.
- **armv7 build failure in `jmwalletd` Docker image**: The previous fix applied `--build-constraint` globally (via `ENV PIP_CONSTRAINT`) which conflicted with `httptools==0.7.1`, which pins `requires = ["setuptools==80.9.0"]` exactly in its `pyproject.toml` and has no pre-built wheel for `linux/arm/v7`. This caused a `ResolutionImpossible` error on armv7 when building httptools from source. Fixed by applying `--build-constraint` selectively: only on pip install steps where source-build packages use setuptools as their build backend (our own packages and `stem`), not on steps that install packages with their own exact setuptools pins (i.e., `jmwalletd/requirements.txt` which contains `httptools`).
- **Cold storage fidelity bond documentation rewrite**: Major rewrite of the cold wallet setup guide in `docs/technical/privacy.md`. Key additions: prominent hardware wallet limitation warning with per-device compatibility table (Ledger/Jade can sign CLTV bonds; Trezor, Coldcard, BitBox02, KeepKey cannot); a "test the full flow" step before funding; HWI version requirements (>= 3.1.0 for newer hardware); QR code display section for air-gapped PSBT transfer; dedicated mnemonic/passphrase strategies for reducing exposure risk; migration guide from the reference implementation using the new helper scripts. Consolidated from `docs/README-jmwallet.md` with cross-reference.
- **`create-bond-address` auto-strips Sparrow `wpkh()` wrapper**: Sparrow's "Copy Public Key" wraps the hex key as `wpkh(03abcd...)`. The `create-bond-address` command now automatically strips this wrapper, so users can paste directly without manual editing.
- **`spend-bond` and `sign_bond_psbt.py` warnings updated**: Both the CLI output and the HWI signing script now show accurate per-device guidance: Ledger and Jade can sign CLTV bonds; other devices should use `sign_bond_mnemonic.py` instead.

### Added

- **`scripts/sign_bond_cert_reference.py` — certificate signing for reference implementation migration**: Self-contained script that signs a joinmarket-ng fidelity bond certificate using a BIP39 mnemonic. Derives the private key at `m/84'/0'/0'/2/<timenumber>` (the reference implementation's fidelity bond path) and signs in Electrum recoverable format, producing a base64 signature directly accepted by `import-certificate`. Only depends on `coincurve`. This is necessary because the reference implementation's `BTC_Timelocked_P2WSH` has a bug where `sign_message()` receives a `(privkey, locktime)` tuple instead of raw bytes, making `wallet-tool.py signmessage` unusable for bond paths.

- **`scripts/derive_bond_pubkey.py` — pubkey extraction for reference implementation migration**: Self-contained script that derives fidelity bond public keys from the reference JoinMarket implementation's xpub (shown by `wallet-tool.py display`). Accepts the account xpub (`fbonds-mpk-` line) or the `/2` branch xpub and a locktime (YYYY-MM), then outputs the compressed public key ready for `create-bond-address`. Only depends on `coincurve`. Includes `--info` mode for timenumber/path lookup without an xpub.

### Changed

- **Ephemeral-identity PoDLE commitment broadcast**: Commitment broadcasts (`!hp2`) are now sent from a fresh random nick on a separate Tor circuit, rather than from the maker's long-lived identity. After verifying a taker's PoDLE proof, the maker opens ephemeral connections to all directory servers using unique SOCKS5 credentials (forcing stream isolation) and a random nick identity, broadcasts the commitment, then tears down the connections. This prevents any party from correlating the `!hp2` broadcast with the maker that participated in the CoinJoin. The same ephemeral approach is used when relaying `!hp2` requests from other makers. Concurrent ephemeral broadcasts are capped at 2 via a semaphore to prevent Sybil DoS attacks.

## [0.19.3] - 2026-03-05

### Added

- **Recovery notification after all directory servers reconnect**: After the critical "All Directories Disconnected" alert, a follow-up "RESOLVED: Directory Servers Reconnected" notification is now sent as soon as at least one directory server reconnects. This uses the same `notify_all_disconnect` toggle (enabled by default) so operators are automatically informed when the issue is resolved.

## [0.19.2] - 2026-03-04

### Fixed

- **`orderbook_watcher` Docker container fails to start**: The `orderbook_watcher` Dockerfile was missing the `jmwallet` installation step. Since `orderbook_watcher/main.py` imports `BitcoinCoreBackend` and `NeutrinoBackend` from `jmwallet`, the container would crash with `ModuleNotFoundError: No module named 'jmwallet'`. Added `jmwallet` requirements and package installation to the Dockerfile and declared `jmwallet` as a dependency in `pyproject.toml`.

## [0.19.1] - 2026-03-04

### Fixed

- **`jam-ng` armv7 Docker build**: Fixed two issues that prevented `linux/arm/v7` builds from succeeding.
  - `node:24-slim` does not publish a `linux/arm/v7` image. The `jam-builder` stage is now pinned to `--platform=linux/amd64` — the output is static browser JS so the build platform is irrelevant.
  - s6-overlay was hardcoded to the `x86_64` tarball with a pinned checksum. The install step now selects the correct arch-specific tarball (`x86_64`, `aarch64`, or `armhf`) and verifies its checksum at build time using `TARGETARCH`/`TARGETVARIANT`.

## [0.19.0] - 2026-03-04

### Fixed

- **Maker PoDLE commitment failure due to unconfirmed UTXOs**: Fixed a bug where the maker bot would advertise liquidity based on unconfirmed UTXOs but fail to complete the coinjoin during `!auth` because unconfirmed UTXOs are excluded from the selection phase. The maker now correctly respects `min_confirmations` (default: 1) for all balance calculations used in offer creation and periodic updates, ensuring it only advertises spendable, confirmed liquidity.
- **Spurious mempool warning after broadcast**: The taker no longer immediately checks the mempool for a just-broadcast transaction (which always fails with "No such mempool transaction"). A 5-second initial delay is now applied before the first mempool lookup in `_update_pending_transaction_now`. The `get_transaction` failure log in both backends is downgraded from WARNING to DEBUG since a missing mempool entry right after broadcast is an expected transient condition.
- **Directory server ignoring `LOGGING__LEVEL` env var**: The standalone `directory_server/docker-compose.yml` and `maker/tests/integration/docker-compose.yml` used flat env var names (e.g., `LOG_LEVEL`, `NETWORK`) that are silently ignored by pydantic-settings. Replaced with the correct nested delimiter form (e.g., `LOGGING__LEVEL`, `NETWORK_CONFIG__NETWORK`) matching `config.toml.template`.
- **Removed `.env.example` files from `directory_server/` and `orderbook_watcher/`**: These files documented incorrect flat env var names. Configuration is now documented directly in `docker-compose.yml` using the config file syntax with `__` delimiter, consistent with pydantic-settings.
- **`TOR__COOKIE_PATH` env var not applied to maker**: `MakerConfig.tor_control` was using `default_factory=TorControlConfig` which constructs a blank config ignoring all env vars, so `cookie_path` was always `None` even when `TOR__COOKIE_PATH` was set. Changed `default_factory` to `create_tor_control_config_from_env` so the Tor control config is always populated from the environment on startup.
- **Maker Tor hidden service setup reliability**: The maker now reliably obtains an ephemeral `.onion` address when Tor is configured.
  - **`jm-tor` Docker healthcheck**: The previous healthcheck (`test -f .../hostname`) was logically equivalent to only checking the `hostname` file due to shell operator precedence — it never actually verified Tor had bootstrapped or that the control auth cookie was valid. The new healthcheck verifies both the `hostname` file exists **and** the `control_auth_cookie` is exactly 32 bytes (the length written by Tor only after full initialization).
  - **Cookie validation in `TorControlClient`**: `_authenticate_cookie()` now explicitly validates the cookie file is exactly 32 bytes before sending the `AUTHENTICATE` command. A 0-byte or partial file (written by Tor during startup) raises `TorAuthenticationError` with a clear message instead of sending an empty hex string that Tor rejects with the cryptic "Got authentication cookie with wrong length (0)" message.
  - **Retry logic in `MakerBot`**: `_setup_tor_hidden_service()` now retries up to 5 times (3s delay) on `TorAuthenticationError`, covering any residual race between the Docker healthcheck passing and the maker process reading the cookie. All errors (auth and non-auth) fall back gracefully to `NOT-SERVING-ONION` with an informative warning rather than crashing.

### Added

- **Signet support in send command**: Sending to signet addresses (`tb1…`) now works correctly. Custom address decoding code replaced with `python-bitcointx` (`CCoinAddress`), which handles all address types and networks without manual script construction.
- **Trustless fidelity bond verification across all blockchain backends**: Replaced mempool.space-dependent bond verification with a unified `BlockchainBackend.verify_bonds()` interface implemented for all backends.
  - **Bitcoin Core backend**: Uses JSON-RPC batching (`_rpc_batch`) to verify all bonds in ~3 HTTP round-trips regardless of the number of bonds. Fetches UTXO existence (`gettxout`), block timestamps (`getblockhash` + `getblockheader`) in batched calls.
  - **Neutrino backend**: Verifies bonds via the `v1/utxo` endpoint with address hints derived from the bond proof (pubkey + locktime), solving the previous inability to verify bonds on Neutrino (which requires an address to scan compact block filters).
  - **Mempool backend**: Falls back to the existing MempoolAPI when no local node backend is configured.
  - **`jmcore`**: Added `derive_bond_address()` and `BondAddressInfo` to `btc_script.py` for P2WSH address derivation from bond proofs, centralizing this logic.
  - **Taker**: `_update_offers_with_bond_values` now delegates to `verify_bonds()` instead of calling MempoolAPI directly.
  - **Orderbook Watcher**: `OrderbookAggregator` uses `verify_bonds()` with fallback to MempoolAPI when no backend is configured. Orderbook watchers in local node setups no longer leak bond queries to mempool.space.
- **`jmwalletd` — JAM-compatible HTTP/WebSocket API daemon**: New monorepo package implementing the JoinMarket wallet RPC API as a FastAPI application, designed as a drop-in replacement for the reference `jmwalletd`. Enables the JAM web UI to work with joinmarket-ng's backend.
  - Full REST API on `/api/v1` matching the reference implementation's endpoints: wallet lifecycle (create, recover, open, lock, unlock), wallet data (display, UTXOs, addresses, seeds), transaction operations (direct send, freeze/unfreeze), CoinJoin control (taker, tumbler, maker start/stop), configuration (get/set), and session management.
  - WebSocket endpoint at `/jmws` (JAM-compatible), `/ws`, and `/api/v1/ws` for real-time CoinJoin state notifications with JWT authentication and heartbeat.
  - JWT authentication with HS256 access tokens (30min) and refresh tokens (4hr), matching the reference auth flow including the custom `x-jm-authorization` header.
  - Self-signed TLS certificate generation for HTTPS/WSS.
  - Backend factory supporting multiple wallet backends (descriptor, bitcoin-core, neutrino, mempool).
  - 161 unit tests with full coverage of auth, models, state, dependencies, routers, wallet operations, and WebSocket.

## [0.18.0] - 2026-03-02

### Breaking Changes

The following CLI options have been **removed** from all commands (`jm-wallet`, `jm-maker`, `jm-taker`):

| Removed option | Replacement |
|---|---|
| `--mnemonic "word1 word2 ..."` | `MNEMONIC="word1 word2 ..." jm-wallet ...` |
| `--password "pw"` | `MNEMONIC_PASSWORD="pw" jm-wallet ...` or `wallet.mnemonic_password` in config |
| `--bip39-passphrase "phrase"` | `BIP39_PASSPHRASE="phrase" jm-wallet ...` or `--prompt-bip39-passphrase` |
| `--rpc-user "user"` | `BITCOIN_RPC_USER="user" jm-wallet ...` or `bitcoin.rpc_user` in config |
| `--rpc-password "pw"` | `BITCOIN_RPC_PASSWORD="pw" jm-wallet ...` or `bitcoin.rpc_password` in config |
| `validate <mnemonic>` (positional) | `MNEMONIC="..." jm-wallet validate` or `jm-wallet validate --mnemonic-file wallet.mnemonic` |

These secrets were leaking into shell history, `/proc/PID/cmdline`, `ps aux`, and audit logs.

For unattended/automated operation, set `MNEMONIC_PASSWORD` (or `wallet.mnemonic_password` in config) so encrypted mnemonic files can be decrypted without a terminal prompt.

### Added

- **Signet infrastructure defaults**: The joinmarket-ng public signet directory node (`signetvaxgd3ivj4tml4g6ed3samaa2rscre2gyeyohncmwk4fbesiqd.onion:5222`) is now the default when signet network is selected. The public orderbook watcher for signet is available at `https://joinmarket-ng-signet.sgn.space/`. Updated `config.toml.template` and `orderbook_watcher/.env.example` with signet examples.

### Security

- **Remove sensitive credentials from CLI arguments** (#130, #132, #133, #136): The removed options appeared in shell history, `/proc/PID/cmdline`, `ps aux`, and audit logs. Secrets are now supplied via environment variables, config file, or interactive prompt. Added `MNEMONIC_PASSWORD` env var support for unattended decryption of encrypted mnemonic files.
- **Fix bech32 checksum bypass in send command (SND-1)**: The hand-rolled bech32 decoder in `_send_transaction` stripped the 6-character checksum without verifying it, meaning a single-character typo in a destination address would silently send funds to a permanently unspendable output. Replaced with the `bech32` library which properly validates checksums per BIP173. Also fixed: unhandled `ValueError` on non-bech32 characters (e.g. uppercase from QR decoders), and `IndexError` on truncated addresses. The same hand-rolled encoder in the neutrino backend was replaced with `bech32.encode()`.

## [0.17.0] - 2026-02-25

### Added

- **`--no-fidelity-bond` flag for maker**: A new CLI flag `--no-fidelity-bond` (config: `no_fidelity_bond = true`) allows running the maker without a fidelity bond proof even when bonds are present in the registry. This is useful for privacy: fidelity bonds are public and linkable to your offers. Mutually exclusive with `--fidelity-bond`, `--fidelity-bond-locktime`, and `--fidelity-bond-index`.

### Fixed

- **SOCKS5h Proxy Incompatibility with httpx-socks**: The `python-socks` library (used by `httpx-socks`) does not recognise the `socks5h://` URL scheme and raises `ValueError`, which was silently caught. This caused `MempoolAPI` and the GitHub update checker to fall back to direct connections without any proxy, failing with DNS resolution errors on `.onion` addresses ("Temporary failure in name resolution"). Added `normalize_proxy_url()` helper in `tor_isolation` that converts `socks5h://` to `socks5://` + `rdns=True`, enabling remote DNS resolution through the Tor SOCKS proxy. Applied to both `MempoolAPI` and `check_for_updates_from_github`.

## [0.16.0] - 2026-02-24

### Added

- **Enhanced Periodic Summary Stats**: The periodic summary notification and CLI `history --stats` now include:
  - **Volume split**: Volume is shown as "successful / total" to distinguish completed CoinJoin volume from total requested volume (including failed attempts).
  - **UTXOs disclosed**: Tracks the number of UTXOs disclosed to takers via `!ioauth`. This counts all UTXOs exposed regardless of whether the CoinJoin completed, since UTXO disclosure is a privacy-relevant event even when transactions fail.
- **Version and Update Check in Summary Notifications**: The periodic summary notification can now include the current version and notify when a newer release is available on GitHub. Opt-in via `check_for_updates = true` in `[notifications]`. The request is routed through Tor when `use_tor` is enabled. Privacy warning: this polls `api.github.com` each summary interval.
- **Tor Stream Isolation**: All outbound Tor connections are now isolated by purpose using SOCKS5 authentication credentials, so that directory, peer, mempool, notification, update-check, and health-check traffic each use separate Tor circuits. This prevents traffic correlation between connection types. Leverages Tor's built-in `IsolateSOCKSAuth` flag -- no Tor configuration changes required. Enabled by default (`stream_isolation = true` in `[tor]`). Six isolation categories: `DIRECTORY`, `PEER`, `MEMPOOL`, `NOTIFICATION`, `UPDATE_CHECK`, `HEALTH_CHECK`. Applied across maker, taker, and orderbook watcher components.

### Fixed

- **Orderbook Watcher DNS Leak**: The orderbook watcher's mempool API proxy used `socks5://` (local DNS resolution) instead of `socks5h://` (DNS resolved by Tor). This leaked DNS queries for `mempool.space` to the local resolver / ISP, even though the HTTP connection itself went through Tor. Now uses `socks5h://` consistently.
- **Wallet Not Reloaded After Bitcoin Core Restart**: When Bitcoin Core restarts while a maker (or taker) is running, the descriptor wallet is unloaded. All subsequent wallet RPC calls (`listunspent`, `listdescriptors`, etc.) fail with error -18 ("Requested wallet does not exist or is not loaded"), causing the wallet to report zero balance and reject CoinJoin requests. The `_rpc_call` method now detects error -18 on wallet-scoped calls, transparently reloads the wallet via `loadwallet`, and retries the failed call once. This makes both periodic rescans and in-flight CoinJoin requests resilient to Bitcoin Core restarts.

### Added

- **Cold Wallet Bond Spending (`spend-bond`)**: New CLI command to generate a PSBT (BIP-174) for spending cold storage fidelity bonds after locktime expires. The PSBT includes the CLTV witness script metadata needed for signing. Implements PSBT serialization from scratch in `jmcore/bitcoin.py`. Usage: `jm-wallet spend-bond <bond-address> <destination> --fee-rate 2.0`, then sign with one of the scripts below.
- **BIP32 Key Origin in Bond PSBTs**: The `spend-bond` command now accepts `--master-fingerprint` and `--derivation-path` to embed `PSBT_IN_BIP32_DERIVATION` (BIP-174 key type 0x06) in the PSBT. This allows HWI to automatically identify the signing key on the hardware wallet.
- **HWI Bond Signing Script**: New standalone `scripts/sign_bond_psbt.py` script for signing bond spending PSBTs via HWI (Hardware Wallet Interface). Supports Trezor, Coldcard, Ledger, and other HW wallets. No seed phrase required. Install with `pip install hwi`.
- **Mnemonic Bond Signing Script**: New standalone `scripts/sign_bond_mnemonic.py` script for signing bond spending PSBTs with a BIP39 mnemonic. Fully self-contained (no project dependencies beyond `coincurve`). Derives the private key from the mnemonic + BIP32 path, verifies it matches the PSBT, and outputs a signed transaction. Mnemonic is read via hidden input and cleared after use.

## [0.15.0] - 2026-02-14

### Fixed

- **Orderbook Watcher: Inflated Fidelity Bond Count**: The "Fidelity Bonds" stat and per-directory bond counts were counting offers-with-bonds instead of unique bonds (by UTXO). Makers with dual offers (relative + absolute) backed by the same bond were counted twice. The frontend now uses the already-deduplicated `fidelitybonds` array for the total count, and the backend deduplicates by UTXO key per directory.

- **Maker Handshake Protocol Incompatibility**: Fixed maker sending DN_HANDSHAKE (type 795, directory server format) instead of HANDSHAKE (type 793, peer client format) when responding to direct peer connections. The reference taker rejected these with "Unexpected dn-handshake from non-dn node", causing CoinJoin failures on direct connections. The maker now correctly responds with HANDSHAKE (793) using client format fields (`proto-ver`, `location-string`, `directory: false`). The orderbook watcher health checker was also updated to handle both response formats. Added regression tests that replicate the reference taker's validation logic.

- **Frozen UTXO Selector Crash** ([#125](../../issues/125)): Fixed `IndexError: list index out of range` when selecting frozen UTXOs in `jm-wallet send --select-utxos`. Frozen and locked fidelity bond UTXOs are now visible but unselectable in the interactive TUI, shown with `[-]` prefix. Toggle (Space/Tab) and "select all" (`a`) skip unselectable UTXOs. The footer displays selectable count accurately. Single-UTXO auto-selection respects frozen/locked status.

- **Frozen UTXO Display Inconsistencies** ([#126](../../issues/126)): Fixed multiple display issues with frozen UTXOs across commands:
  - Total Balance line now shows frozen amounts: `Total Balance: 30,200 sats (68,811 frozen)`.
  - Per-mixdepth balances in simple view show frozen amounts.
  - `[FROZEN]` tag moved after `(label)` in UTXO selector for consistency with `--extended` view.
  - `get_fidelity_bond_balance()` now excludes frozen UTXOs.
  - Taker interactive UTXO selection now shows frozen UTXOs as unselectable (previously they were silently filtered).

### Changed

- **Tor Connection Timeout Increased to 120s**: Increased the default Tor connection timeout from 30s to 120s across all components (maker, taker, directory client). The previous 30s timeout covered the entire SOCKS5 connection lifecycle (TCP + SOCKS negotiation + Tor circuit building + PoW solving), which is too short when PoW-protected hidden services are under DoS load. The reference JoinMarket implementation effectively has no SOCKS-level timeout (Twisted cancels the 60s timeout after TCP handshake, leaving circuit building with no limit). The new 120s default aligns with Tor's internal circuit timeout. Configurable via `connection_timeout` in the `[tor]` config section.

### Added

- **Periodic Summary Notifications**: Makers now receive daily summary notifications with CoinJoin statistics (requests, successes, failures, earnings, volume). Enabled by default with `notify_summary = true` and 24-hour interval. To disable, set `notify_summary = false` in config.toml `[notifications]` section. Configurable interval via `summary_interval_hours` (1-168). Respects existing privacy settings (`include_amounts`). Added `get_history_stats_for_period()` for time-filtered history stats.

- **Background Retry for Notifications**: Failed notifications are now automatically retried in the background with exponential backoff. This is critical for Tor-routed notifications where transient circuit failures are common. Retries never block the main process (fire-and-forget via `asyncio.create_task`). Enabled by default with 3 retry attempts and a 5-second base delay (doubling each attempt). Configurable via `retry_enabled`, `retry_max_attempts` (1-10), and `retry_base_delay` (1-60s) in the `[notifications]` config section. No new dependencies -- uses plain asyncio.

### Fixed

- **Taker History: Zero Mining Fee Recorded**: Fixed a bug where taker transaction history recorded `mining_fee=0` despite the taker paying the full mining fee. The history update after broadcast used `tx_metadata["fee"]` (the estimated fee from transaction construction) instead of `actual_mining_fee` (total inputs minus total outputs from the signed transaction). In sweep mode, these values diverge because the residual from integer rounding goes to miners. This caused the `Net Fee` column in `jm-wallet history` to show only maker fees, understating the taker's total cost.

## [0.14.0] - 2026-02-12

### Fixed

- **Taker Signature Completeness Check**: Fixed a bug in `_phase_collect_signatures` where the taker used `minimum_makers` to decide if enough signatures were collected. Once a transaction is built with specific maker inputs, every maker must provide valid signatures -- `minimum_makers` is only relevant during the filling phase. The old check could allow proceeding with missing signatures if `minimum_makers` was set lower than the actual number of makers in the transaction, producing an invalid (partially signed) transaction. The `add_signatures` method in `CoinJoinTxBuilder` now also raises `ValueError` if any input is missing a signature, as defense-in-depth.

### Added

- **UTXO Freezing** ([#104](../../issues/104)): Added `jm-wallet freeze` command to freeze/unfreeze individual UTXOs, preventing them from being used in automatic coin selection (taker, maker, and sweep operations). This is critical for privacy — preserving specific UTXO sizes, preventing dust attacks, and excluding newly deposited coins from being mixed.
  - **Interactive curses TUI**: Space/Tab to toggle freeze, j/k and arrow keys to navigate, a/n for freeze/unfreeze all, q to exit. Color-coded status indicators (red for frozen, green for spendable, magenta for fidelity bonds). Footer shows frozen count, frozen value, and spendable value. Optional `--mixdepth/-m` filter.
  - **BIP-329 JSONL persistence**: Frozen state is stored in `wallet_metadata.jsonl` using the BIP-329 label format with the `spendable` field on `output` type records. This gives Sparrow wallet interoperability for free — users can sync their coin control state between JoinMarket NG and Sparrow.
  - **Automatic exclusion**: Frozen UTXOs are excluded from `select_utxos()`, `get_all_utxos()`, `select_utxos_with_merge()`, and `get_balance()`. Makers won't advertise frozen funds, and takers won't use them.
  - **Visible in wallet info**: `jm-wallet info` shows frozen amounts per mixdepth in simple view and `[FROZEN]` tags on addresses in extended view.
  - **UTXO selector integration**: The interactive UTXO selector (`--select-utxos`) now shows frozen indicators and prevents selecting frozen UTXOs via "select all".
  - **Comprehensive e2e test suite**: 36 end-to-end tests covering freeze/unfreeze persistence, balance exclusion, UTXO selection exclusion across maker/taker/send paths, BIP-329 persistence and hot-reload, Sparrow interop, read-only filesystem handling, and realistic usage scenarios.

### Changed

- **Directory Disconnect Notification Defaults**: Changed `notify_disconnect` default to `false` (was `true`). Individual directory server disconnect/reconnect notifications are noisy and not actionable. Added new `notify_all_disconnect` setting (default `true`) that fires only when ALL directory servers are disconnected, which is the critical event users need to know about. The `notify_all_directories_disconnected()` method now respects this toggle.

## [0.13.12] - 2026-02-09

### Fixed

- **Pin Python Build Tools for Reproducible Builds**: Pinned `setuptools` and `wheel` versions in all Dockerfiles via `PIP_CONSTRAINT`. When pip builds local packages (jmcore, jmwallet, taker, etc.) via PEP 517 build isolation, it downloads the latest `setuptools` from PyPI. The setuptools version is stamped into each package's `WHEEL` metadata file (`Generator: setuptools (x.y.z)`), and different versions produce different `WHEEL` and `RECORD` file contents. This caused the pip packages layer to have different digests between CI build time (e.g., setuptools 81.0.0) and local verification days later (e.g., setuptools 82.0.0). The `./scripts/update-base-images.sh` script now also updates these pinned versions from PyPI.

- **Maker Infinite Loop on Connection Reset**: Fixed a tight infinite loop in the maker bot that occurred when a directory server connection was reset. A `ConnectionResetError` (errno 104) was not recognized by the string-based error detection in `listen_for_messages()`, causing the loop to `continue` immediately and retry the broken connection with zero delay. This flooded logs and consumed all available RAM over time. The fix adds proper exception type catching in `TCPConnection.receive()` for `OSError`/`ConnectionError`, replaces fragile string matching with explicit exception handling in `listen_for_messages()` with consecutive error tracking, and adds exponential backoff with max error limits in the maker's `_listen_client()` loop.

- **Missing maker-data Docker Volume**: Added the `maker-data` named volume to the root `docker-compose.yml` volumes section. It was referenced by the maker service but not declared, which could cause issues on some Docker versions.

### Changed

- **Docker Resource Limits for Test Environment**: Added deploy resource limits (1 CPU, 512MB memory) to all services in the root `docker-compose.yml` (test environment) to prevent runaway resource consumption from bugs like the infinite loop above. Component-specific docker-compose files (`maker/`, `taker/`, etc.) already had resource limits configured.

## [0.13.11] - 2026-02-08

### Fixed

- **Pin Apt Package Versions for Reproducible Builds**: All apt packages in Dockerfiles are now pinned to exact versions (e.g., `libsodium23=1.0.18-1+deb13u1`). Previously, `apt-get install` without version pins meant that a security update to any package (like libsodium23) between CI build time and local verification would produce a different layer digest, breaking `verify-release.sh --reproduce` within days of release.

- **Auto-Setup BuildKit Builder for OCI Export**: The `verify-release.sh --reproduce` and `sign-release.sh --reproduce` scripts now automatically detect when the current Docker buildx driver doesn't support OCI export format and create a suitable builder (`jmng-verify`) with the `docker-container` driver. Previously, users with plain Docker CE (without Docker Desktop or containerd image store) would get "OCI exporter is not supported for the docker driver" errors.

### Changed

- **update-base-images.sh Now Updates Apt Versions**: The `./scripts/update-base-images.sh` script now also resolves the latest available apt package versions from the base image and updates pinned versions in all Dockerfiles. This ensures that running the script before a release picks up both base image security patches and apt package updates in a single step.

## [0.13.10] - 2026-02-06

### Fixed

- **User Creation Shadow File Reproducibility**: Fixed reproducible builds broken by `useradd` setting the "last password change" field in `/etc/shadow` to the current day (days since Unix epoch). When verifying a release on a different day than CI built it, layer 7 (useradd) would have different digests. Now, if `SOURCE_DATE_EPOCH` is set, we calculate days from that epoch and fix the shadow entry to match.

- **Source File Timestamp Normalization**: Fixed reproducible builds for orderbook-watcher by normalizing source file timestamps to `SOURCE_DATE_EPOCH` in the builder stage. BuildKit's `rewrite-timestamp=true` only modifies the OCI tar output, not layer content hashes. Layer digests are computed before rewriting, so files must have identical timestamps during the build. Without normalization, local files (with old modification times) differ from CI (fresh git clone with recent times).

## [0.13.9] - 2026-02-05

### Fixed

- **Orderbook-Watcher Reproducibility via Builder Stage**: Fixed reproducible builds for orderbook-watcher by copying source and static files through the builder stage with permission normalization. Previously, files were copied directly to the production stage, preserving local filesystem permissions (based on umask), and the post-copy chmod ran as user `jm` which couldn't fix permissions on directories with restrictive modes. Now, files are copied to builder, normalized to 644/755 as root, then copied to production with `--from=builder`.

- **Root .dockerignore**: Added a root-level `.dockerignore` file to exclude development artifacts (`*.egg-info/`, `__pycache__/`, `*.pyc`, etc.) from Docker build context. These files don't exist in CI (fresh git clone) but accumulate locally during development, causing COPY layer mismatches.

## [0.13.8] - 2026-02-05

### Fixed

- **Empty Tor Cookie File Detection**: Cookie path auto-detection now verifies that the cookie file has content (non-zero size) before using it. Previously, an empty cookie file at `/run/tor/control.authcookie` would be selected, causing Tor authentication to fail with "cookie of size zero" errors.

- **Install Script Tor Configuration**: The install script now explicitly sets `CookieAuthFile /run/tor/control.authcookie` in torrc. Previously, only `CookieAuthentication 1` was set, leaving the cookie path to Tor's default which varies by distribution.

- **Install Script Update Mode Torrc Verification**: Running `install.sh --update` now verifies and fixes the Tor configuration if the JoinMarket-NG section is missing, commented out, or incomplete (e.g., missing `CookieAuthFile`).

- **Orderbook-Watcher File Permission Reproducibility**: Added permission normalization step to the orderbook-watcher Dockerfile. Previously, files copied directly to the production stage preserved local filesystem permissions (based on umask), causing builds to differ across systems. The new `RUN find ... chmod` step ensures consistent 644/755 permissions regardless of the build environment.

### Added

- **Skip Signature Verification Option**: Added `--skip-signatures` flag to `verify-release.sh` for testing reproducibility without requiring GPG signatures.

## [0.13.7] - 2026-02-05

### Fixed

- **File Timestamp Reproducibility with rewrite-timestamp**: Added `rewrite-timestamp=true` to Docker build outputs in both CI and verification scripts. This BuildKit feature clamps all file timestamps inside image layers to `SOURCE_DATE_EPOCH`, ensuring files created by `apt-get install`, `pip install`, and other commands have consistent timestamps regardless of when the build runs. Without this, directories like `/etc`, `/var/lib/apt`, etc. have timestamps from build time, causing layer digest mismatches.

- **Verification Script Target Mismatch**: Fixed `verify-release.sh --reproduce` and `sign-release.sh --reproduce` to specify the correct `--target` for each image, matching the CI workflow. Previously, `directory-server` was being built without a target, which defaults to the last stage (`debug`) instead of `production`.

### Note

Releases prior to these changes (including 0.13.5, 0.13.6, and 0.13.7) cannot be fully reproduced locally for the orderbook-watcher image due to file permission differences. Files copied directly to the production stage in orderbook-watcher preserved local filesystem permissions, which vary based on umask settings. CI runners typically use umask 0022 (resulting in 644 files), while developer machines often use umask 0002 (resulting in 664 files). Only releases built with the permission normalization fix will have fully reproducible orderbook-watcher images.

## [0.13.6] - 2026-02-05

### Changed

- **Disabled Build Cache for CI Releases**: Added `no-cache: true` to the CI release workflow. Cached layers from previous builds may contain different package versions, making local reproduction impossible. Fresh builds ensure consistency between CI and local verification.

- **Base Image Digest Pinning**: All Dockerfiles now pin Python base images by manifest list digest for reproducible builds. This ensures the exact same base image is used across builds, regardless of when they run. Use `./scripts/update-base-images.sh` to update digests when new Python images are released.

- **Faster Verification with Git Worktree**: `verify-release.sh --reproduce` and `sign-release.sh --reproduce` now use `git worktree` instead of cloning from GitHub. This is faster and more secure - it uses locally verified code rather than trusting the remote blindly. Users must have the commit locally (run `git fetch origin` if needed).

### Added

- **Base Image Update Script**: New `scripts/update-base-images.sh` script to update Python base image digests in all Dockerfiles. Run periodically to get security updates while maintaining reproducibility. Use `--check` to verify if updates are needed.

## [0.13.5] - 2026-02-05

### Changed

- **Layer-Based Reproducibility Verification**: Replaced manifest digest comparison with layer digest comparison for reproducible build verification. Layer digests are content-addressable hashes of actual image content and are identical regardless of manifest format (Docker vs OCI). This fixes the fundamental issue where CI builds (pushed to registry) produce Docker distribution manifests while local builds produce OCI manifests - even for identical image content, these have different manifest digests. By comparing layer digests instead, verification works reliably across different build environments.

- **Simplified CI Release Workflow**: Removed the slow OCI tar rebuild step from the CI release workflow. Previously, after pushing to the registry, CI would rebuild each platform as an OCI tar to extract digests - this caused timeouts (30+ minutes per image). The new approach extracts layer digests directly from the pushed images using `docker buildx imagetools inspect`, which is fast and reliable.

- **Updated Release Manifest Format**: The release manifest now contains per-platform layer digests in addition to manifest digests. Layer digests are listed under `### <image>-<arch>-layers` sections, enabling local verification to compare the actual image content rather than manifest metadata.

## [0.13.4] - 2026-02-05

### Changed

- **Use OCI Digests for Reproducible Build Verification**: The release manifest now contains OCI tar digests instead of registry manifest digests. CI builds each platform image as an OCI tar (in addition to pushing to registry) and stores those digests in the manifest. This ensures local verification produces the exact same digest as CI, since both use the same output format (`type=oci,dest=...,rewrite-timestamp=true`). Previously, local verification used OCI output while CI stored registry digests, which are fundamentally different even for identical image content.

- **Enabled rewrite-timestamp for Reproducible Builds**: Added `rewrite-timestamp=true` to Docker build outputs in CI and verification scripts. This BuildKit feature clamps all file timestamps inside images to `SOURCE_DATE_EPOCH`, ensuring that file metadata (like directory mtimes created by apt-get, ldconfig) doesn't vary between builds. Combined with disabling attestations, this achieves true reproducible Docker builds.

### Fixed

- **Docker Image Reproducibility (ldconfig cache)**: Added deletion of `/var/cache/ldconfig/aux-cache` after apt-get install in all Dockerfiles. This binary cache file contains non-deterministic data that caused builds to differ even with the same inputs.

## [0.13.3] - 2026-02-05

### Changed

- **Disabled Docker Attestations for Reproducible Builds**: Disabled provenance and SBOM attestations in the CI release workflow (`provenance: false`, `sbom: false`). These attestations include timestamps and environment-specific data that made builds non-reproducible across different build environments. While this removes supply chain metadata from images, it enables true reproducibility verification where anyone can build the same image and get the exact same digest.

## [0.13.2] - 2026-02-04

### Changed

- **Maker `min_size` Default Reduced to Dust Threshold**: Changed the default `min_size` for maker offers from 100,000 sats to 27,300 sats (the dust threshold). The previous 100k default was arbitrary and prevented makers with smaller UTXOs from participating. The dust threshold is the true minimum for any Bitcoin output, making it the natural floor for CoinJoin amounts.

- **Simplified Reproducibility Verification**: The `verify-release.sh --reproduce` and `sign-release.sh --reproduce` scripts no longer require a local Docker registry. Instead, they use OCI tar export (`--output type=oci,dest=...`) to extract the manifest digest directly from the built image. This reduces dependencies (no registry container needed) and is more reliable.

### Fixed

- **Reproducibility Verification Digest Extraction**: Fixed `verify-release.sh --reproduce` and `sign-release.sh --reproduce` to correctly extract platform-specific image digests instead of manifest list digests. When building with `--load`, Docker creates a manifest list that includes attestations, resulting in a different digest than the actual platform image. The scripts now use `jq` to extract the correct digest from `.manifests[]` excluding attestation manifests (platform.os != "unknown"), matching the CI workflow's digest extraction logic.

- **Docker Image Reproducibility**: Fixed Dockerfiles to delete apt/dpkg log files (`/var/log/dpkg.log`, `/var/log/apt/*`) after package installation. These logs contain timestamps that made builds non-reproducible across different build times. This affects all four images: maker, taker, directory-server, and orderbook-watcher.

## [0.13.1] - 2026-02-04

### Fixed

- **Release Verification Script Now Fails on Reproduce Errors**: Fixed `verify-release.sh --reproduce` to properly fail (exit 1) when locally built Docker images have different digests than the release manifest. Previously, digest mismatches were only logged as warnings and the script would exit successfully.

- **Single-Architecture Reproducibility Verification**: Fixed `verify-release.sh --reproduce` and `sign-release.sh` to build only for the current machine's architecture (e.g., amd64 on x86_64, arm64 on Apple Silicon/RPi4). Previously attempted to build all 3 platforms which was slow and unnecessary. Verification now also cross-checks the built image against both the manifest and the published registry image to ensure the release wasn't tampered with.

### Changed

- **Per-Platform Digests in Release Manifest**: The release manifest now stores individual digests for each platform (`maker-amd64`, `maker-arm64`, `maker-arm-v7`) in addition to the manifest list digest (`maker-manifest`). This enables faster verification by building only the current architecture while keeping provenance/SBOM attestations enabled for supply chain security.

- **All Signers Must Reproduce Builds**: The `sign-release.sh` script now enables `--reproduce` by default for all signers. Multiple signatures only add value if each signer independently verifies reproducibility. Use `--no-reproduce` to skip verification (not recommended).

## [0.13.0] - 2026-02-04

### Added

- **NUMS Point Generation Algorithm** ([#101](../../issues/101)): Added explicit documentation and implementation of the NUMS (Nothing Up My Sleeve) point generation algorithm for PoDLE commitments. The `generate_nums_point()` function now transparently generates deterministic NUMS points using SHA256 hashing of secp256k1's generator G. NUMS points are cached for efficiency and validated against test vectors from the original JoinMarket implementation. Support for NUMS indices expanded from 10 to the full range of 256 (0-255), providing generous headroom for multiple commitment reuses per UTXO.

- **Tor-Level DoS Defense for Hidden Services**: Makers can now configure Tor-level DoS protection for their hidden services via the `hidden_service_dos` config option. This includes:
  - **Proof-of-Work Defense** (`PoWDefensesEnabled`): Computational puzzle that clients must solve to connect. Makes flooding attacks expensive. Enabled by default with suggested effort starting at 0 (no puzzle required for normal operation) and auto-scaling under attack.
    - For ephemeral HS (ADD_ONION): Requires **Tor 0.4.9.2+** (not yet in stable releases)
    - For persistent HS (torrc): Requires Tor 0.4.8+ with `--enable-gpl` build
  - **Max Streams per Circuit** (`max_streams`): Limit concurrent streams per rendezvous circuit.
  - Automatic capability detection for Tor version and PoW module availability.
  - **Note**: Introduction point rate limiting (`HiddenServiceEnableIntroDoSDefense`) is NOT supported for ephemeral hidden services due to Tor control protocol limitations. Users who need this protection should configure persistent hidden services in torrc. See INSTALL.md for configuration examples.
  - Reference: https://community.torproject.org/onion-services/advanced/dos/

- **Connection-Based Rate Limiting for Direct Connections**: Added `DirectConnectionRateLimiter` that tracks by connection address (peer_str) instead of nick. This prevents nick rotation attacks where attackers use a random nick per request to bypass the existing nick-based rate limiting. Direct connections now have stricter limits: 30s orderbook interval (vs 10s), 10 violations to ban (vs 100), and general message rate limiting (5 msg/s with 20 burst).

### Fixed

- **Taker History Update Failure in Sweep Mode**: Fixed a bug where taker history entries were not being updated after a successful sweep CoinJoin. The issue occurred because a change address was always generated (even when not needed), but not always used in the transaction. This caused history matching to fail because the recorded change address didn't match reality. The fix prevents generating a change address when it's not needed: the taker now calculates whether change will exceed the dust threshold before generating an address. If no change output will be created (sweep mode or dust), no address is generated, and an empty string is stored in history. This ensures history accurately reflects which addresses were actually revealed in transactions.

- **Fidelity Bond Address Detection During Sync**: Fixed a bug where fidelity bond addresses were incorrectly flagged as "out of range" during wallet sync, triggering an unnecessary extended range search (~40 seconds delay). The root cause was that `_find_address_path()` only searched branches 0 and 1 (external/internal), but fidelity bond addresses use branch 2. The fix checks the fidelity bond registry before falling back to expensive derivation scanning, allowing bond addresses to be identified immediately.

- **Early Fund Validation for CoinJoin** ([#102](../../issues/102), [#106](../../issues/106)): Added early fund validation for `jm-taker coinjoin` to check if sufficient funds are available before connecting to directory servers. This avoids unnecessary waiting time when the wallet has insufficient funds. The `Taker` class now exposes `sync_wallet()` and `connect()` methods separately, allowing the CLI to validate funds after wallet sync but before directory connection. Additionally, when using `--select-utxos`, funds are now validated immediately after UTXO selection (fixing the bug where coinjoins would start with insufficient funds and only fail later with "Failed to generate PoDLE commitment").

### Changed

- **Improved CoinJoin Confirmation Display** ([#110](../../issues/110)): Redesigned the `jm-taker coinjoin` confirmation screen for better readability:
  - Title changed from "EXPECTED CJ TX" (all caps) to "Expected COINJOIN Transaction" (mixed case)
  - Information displayed in column form with consistent label widths
  - Reordered fields to match workflow: Source Mixdepth → Destination → CoinJoin Amount → Makers → Fees
  - Added "Miner Fee Rate" display (sat/vB)
  - Maker list now shows right-aligned fee and bond values for easier comparison
  - Removed redundant "Counterparties" field (count now shown inline as "Makers (N):")

## [0.11.6] - 2026-02-03

### Fixed

- **CoinJoin Confirmation Total Fee Display** ([#109](../../issues/109)): Fixed a bug where the "Total Fees (makers+network)" in the CoinJoin confirmation prompt only showed maker fees, not the actual total. The display now correctly shows the sum of maker fees and mining fees.

- **Address Reuse After Counterparty Disappears (Maker & Taker)**: Fixed a critical privacy bug affecting both makers and takers where addresses revealed during the CoinJoin protocol could be reused if the counterparty disappeared before the transaction completed.
  - **Maker fix**: Addresses revealed during `!ioauth` are now recorded to history before sending the response, ensuring they are blacklisted even if the taker disappears before sending `!tx`.
  - **Taker fix**: Addresses included in the `!tx` message (destination and change addresses) are now recorded to history before sending to makers, ensuring they are blacklisted even if makers don't respond with signatures or the broadcast fails.
  - Previously, both roles only recorded addresses to history after successful transaction signing/broadcast. Now, addresses are recorded **before** being revealed, with the history entry updated later with txid and fee information.
  - The `create_taker_history_entry()` function now requires a `change_address` parameter to ensure taker change addresses are also tracked and blacklisted.
  - Addresses are persisted before being revealed to prevent reuse even in failure scenarios.

## [0.11.5] - 2026-01-24

### Fixed

- **Maker Advertising Fidelity Bond Funds as Spendable**: Fixed a bug where makers would include fidelity bond (FB) UTXOs in their advertised max size, leading to failed CoinJoins when takers requested amounts that could only be satisfied by including the FB funds. The fix adds `get_balance_for_offers()` method that excludes all FB UTXOs, and updates the maker offer creation and mixdepth selection to use this balance. UTXO selection methods (`select_utxos`, `select_utxos_with_merge`, `get_all_utxos`) now exclude FB UTXOs by default via the `include_fidelity_bonds` parameter. The `jm-wallet info` command now shows FB balance separately.

- **External Fidelity Bonds Not Recognized During Sync**: Fixed a bug where external fidelity bonds (cold storage bonds with `index=-1`) were not being properly recognized during wallet sync. These UTXOs were incorrectly treated as regular spendable funds instead of fidelity bonds, causing them to be included in offer balances and potentially leading to failed CoinJoins. The fix adds additional checks in `sync_with_descriptor_wallet()` to recognize fidelity bond addresses from the registry even when they don't match through the primary lookup path.

## [0.11.4] - 2026-01-23

### Fixed

- **Address Reuse Bug for Used-but-Empty Addresses**: Fixed a critical privacy bug where addresses that had been used (received and spent funds) would incorrectly show as "new" instead of "used-empty". This could lead to address reuse, a serious privacy concern for CoinJoin wallets. The root cause was that `listsinceblock` and `listtransactions` RPCs don't reliably return transaction details for addresses in descriptor wallets, especially after wallet import. The fix uses `listaddressgroupings` RPC as the primary source for detecting used addresses, which reliably returns all addresses that have been involved in any transaction (as inputs or outputs). This is combined with `listsinceblock` as a secondary source for completeness.

## [0.11.3] - 2026-01-22

### Fixed

- **Descriptor Wallet Sync Hanging with Deep History**: Fixed a critical bug where wallets with address history at indices beyond the current descriptor range would cause sync to hang or fail to find those addresses. This affected users migrating from other wallet software or with extensive transaction history. The fix includes:
  - Added `_find_address_path_extended()` to search beyond the current descriptor range when addresses are not found
  - Addresses from transaction history beyond the current range now trigger an extended search (up to 5000 indices beyond)
  - Once found, the descriptor range is automatically upgraded to accommodate the high-index addresses
  - Added detailed progress logging for address cache population (shows ETA for large caches)
  - Added logging to track addresses found beyond the current range

- **Extended Address Search for Non-Wallet Addresses**: Fixed a performance issue where the extended address range search would unnecessarily search for counterparty addresses from CoinJoin transactions. The `get_addresses_with_history()` method now excludes "send" category addresses (addresses we sent to, not our own) which don't belong to this wallet. This prevents slow extended searches after CoinJoin transactions and ensures makers restart quickly between transactions.

## [0.11.2] - 2026-01-21

### Changed

- **Dependency Lock Files with Hashes**: Updated all dependency lock files (`requirements.txt` and `requirements-dev.txt`) to include SHA256 hashes for enhanced security. This ensures package integrity verification during installation. The `scripts/update-deps.sh` script now uses `pip-compile --generate-hashes` flag. The `coincurve` dependency is pinned to a specific commit hash for reproducibility and hash verification.

### Added

- **Nick State Files for External Tracking**: All components (maker, taker, directory server, orderbook watcher) now write their nick to a state file at startup (`~/.joinmarket-ng/state/<component>.nick`). This allows operators to easily identify running bots' nicks for external monitoring and tracking. The files are automatically cleaned up on shutdown.

- **Nick Included in Startup Notifications**: Startup notifications now include the component's nick in the notification body, making it easier for operators to identify which bot sent the notification without needing to check logs.

- **Self-CoinJoin Protection**: When running both maker and taker from the same wallet/data directory, the components now automatically detect and protect against self-CoinJoins:
  - Taker reads the maker nick state file and automatically excludes it from peer selection
  - Maker reads the taker nick state file and rejects fill requests from its own taker nick
  - This protection is automatic and requires no configuration

### Fixed

- **Spent Addresses Shown as 'new' After Wallet Import**: Fixed a bug where addresses that had received and spent funds (now empty) would incorrectly show as 'new' instead of 'used-empty' after importing a wallet from mnemonic. The issue was that `sync_with_descriptor_wallet()` only added addresses to `addresses_with_history` if they were already in the address cache. If the cache wasn't populated far enough, spent addresses returned by `get_addresses_with_history()` would be silently ignored. The fix uses `_find_address_path()` which will derive and find addresses even if not in the initial cache.

- **DirectoryServer Shutdown Hang in Python 3.12+**: Fixed a hang during test fixture teardown when using Python 3.12+. The `DirectoryServer.stop()` method now properly tracks and cancels client handler tasks before calling `wait_closed()`, which in Python 3.12+ waits for all handler tasks to complete. Added timeout safeguards to both `stop()` and test fixtures to prevent indefinite hangs.

- **CoinJoin Confirmation Prompt Input Handling**: Fixed an issue where user confirmation ("y") would be incorrectly declined during the final broadcast confirmation. The stdin buffer is now properly flushed before reading user input to avoid stale data when running in asyncio context.

- **Encrypted Mnemonic Decryption Error Handling**: Fixed an unhandled `UnicodeDecodeError` that could occur when loading encrypted mnemonic files from config. If the decrypted content is not valid UTF-8 (e.g., file corrupted or encrypted with a different tool), the error is now caught and a clear error message is displayed instead of a raw codec error.

- **Default Wallet Uses Config Password**: Fixed an issue where `wallet.mnemonic_password` from config was not used when loading the default wallet at `~/.joinmarket-ng/wallets/default.mnemonic`. Previously, setting `mnemonic_password` in config only worked if `mnemonic_file` was also explicitly set. Now the config password is used for the default wallet path as well, eliminating the need to set `mnemonic_file` when using the default location. Also consolidated mnemonic resolution logic from jmwallet into the shared `resolve_mnemonic` function in jmcore.

- **Directory Server Uses Random Nick**: Fixed the directory server to use a random JM-format nick (e.g., `J5FA1Gj7Ln4vSGne`) instead of a hardcoded `directory-{network}` nick. This matches the reference implementation behavior where directory servers use the same nick format as any other peer.

- **Descriptor Wallet Gap Limit Bug**: Fixed a critical bug where wallets with more than 1000 addresses would show 0 balance in `jm-wallet info` despite having funds. The issue was threefold:
  1. `_find_address_path()` only scanned up to index 100, so addresses beyond that were marked "unknown"
  2. `DEFAULT_SCAN_RANGE` (1000) was used as a max index rather than a true gap limit
  3. No mechanism existed to upgrade descriptor ranges when wallets grew beyond the initial range

  The fix includes:
  - `_find_address_path()` now scans up to the full descriptor range (retrieved from Bitcoin Core)
  - Pre-populate address cache during sync for O(1) lookups
  - Automatic detection and upgrade of descriptor ranges when highest used index approaches the limit
  - Added `get_descriptor_ranges()`, `get_max_descriptor_range()`, and `upgrade_descriptor_ranges()` methods to DescriptorWalletBackend
  - Added `check_and_upgrade_descriptor_range()` method to WalletService that automatically expands ranges as needed

- **recover-bonds Now Waits for Wallet Rescan**: Fixed a bug where `jm-wallet recover-bonds` would attempt to query UTXOs before the wallet rescan completed, causing "Wallet is currently rescanning" errors or missing bond discovery. The command now properly waits for each batch of descriptor imports to finish rescanning before querying for UTXOs. Added `wait_for_rescan_complete()` method to the descriptor wallet backend.

- **list-bonds Now Updates Registry with Discovered Bonds**: Fixed a bug where `jm-wallet list-bonds --locktime` would find bonds on the blockchain but not save them to `fidelity_bonds.json`. Now when bonds are discovered via `--locktime` scanning, they are automatically added to the registry with full UTXO information (txid, vout, value, confirmations). Existing registry entries also get their UTXO info updated.

### Changed

- **Improved CoinJoin Transaction Summaries**:
  - Changed "Fee:" to "Total Fees (makers+network):" in confirmation prompts to clearly show it represents the sum of maker fees and mining fees
  - Added CSV entry logging when users decline to broadcast, allowing manual transaction tracking and later broadcast via the transaction hex

- **Improved Fidelity Bond Recovery Documentation**: Enhanced maker/README.md with detailed fidelity bond recovery workflow including BIP39 passphrase handling. Added note in DOCS.md clarifying that BIP39 passphrases are intentionally not read from config.toml for security reasons.

## [0.11.0] - 2026-01-20

### Added

- **Fidelity Bond Tool ASCII Signature Format Support**: The `fidelity_bond_tool.py` script now correctly verifies certificate signatures in both binary and ASCII message formats. Previously, it only tried the binary format (raw pubkey bytes in the message), which failed for cold storage bonds where the certificate was signed using Sparrow Wallet's message signing feature. The ASCII format (hex pubkey string in the message) is now also tried, matching the behavior of the reference implementation and our `verify_fidelity_bond_proof` function. The tool now also reports which format was used for successful verification.

- **Enhanced Fidelity Bond Modal in Orderbook Watcher**: The bond details modal now shows comprehensive verification information similar to `fidelity_bond_tool.py`:
  - **Verification summary banner** at the top with color-coded status (green=valid, yellow=expired cert, blue=pending)
  - **Certificate details section** showing UTXO pubkey (cold wallet), certificate pubkey (hot wallet), and certificate type (self-signed vs delegated)
  - **Certificate expiry validation** fetches current block height from Mempool API and shows remaining validity or expiration status
  - **Improved locktime display** shows human-readable unlock date with time remaining
  - Helps diagnose why a bond may show value 0 (e.g., expired certificate)

- **Improved Offer Type Configuration Documentation and Logging**: Enhanced maker configuration to make the `offer_type` setting more intuitive:
  - Updated `config.toml.template` with clearer documentation explaining that `offer_type` must be explicitly set to use absolute fees (simply setting `cj_fee_absolute` alone is not sufficient)
  - Added startup logging that clearly shows the configured offer type and fee (e.g., "Offer config: type=sw0reloffer, relative fee=0.001 (0.1000%)")
  - Added detailed startup logging when using `--dual-offers` showing both offer configurations
  - Added summary log after offer creation showing all offers to be announced with their sizes and fees
  - Addresses issue [#86](../../issues/86) where users expected commenting out `cj_fee_relative` would switch to absolute fees

- **Real-Time Autocomplete for Mnemonic Input**: The `jm-wallet import` interactive mnemonic input now features real-time autocomplete suggestions as you type. When there are 10 or fewer matching BIP39 words, they are displayed inline in gray. When only one match remains (after typing 3+ characters), the word auto-completes automatically. Tab completion is also available for partial matches. The feature gracefully falls back to readline-based completion on terminals that don't support raw input mode. Additionally, you can now paste all words at once (or a subset), with validation and clear error messages for invalid words.

- **Component Name in Notification Titles**: Notifications now include the component name in the title, making it easier to identify which component sent a notification when running multiple JoinMarket components (Maker, Taker, Directory, Orderbook). For example, instead of "JoinMarket NG: Fill Request Received", notifications now show "JoinMarket NG (Maker): Fill Request Received". This is especially useful when running multiple components simultaneously and receiving notifications through a single channel.

- **Fix Scientific Notation in Maker Fee Offers**: Fixed an issue where small relative fee values (like `0.00001`) were being sent in scientific notation (e.g., `1e-05`) instead of decimal notation. This happened when the fee was configured as a float in TOML config or environment variables, and Python's default float-to-string conversion produced scientific notation. The JoinMarket protocol expects decimal notation, which could cause compatibility issues with reference implementations. Added field validators to normalize all `cj_fee_relative` values to proper decimal strings.

- **Improved Wallet Info Display**: Redesigned the `jm-wallet info` output to be clearer and less misleading:
  - **Standard view**: Balance and deposit addresses are now shown on separate lines with clear headers, instead of on the same line which could be misinterpreted as showing the balance at that specific address.
  - **Extended view**: Added a legend explaining address status labels (new, deposit, cj-out, non-cj-change, used-empty, flagged) so users can understand why addresses were skipped or marked as do-not-reuse.

- **Unconfirmed Transaction Display in Wallet Info**: The `jm-wallet info --extended` command now shows "(unconfirmed)" status for addresses with unconfirmed UTXOs. This detects pending transactions directly from the Bitcoin backend (via `listunspent` with `minconf=0`), providing visibility into unconfirmed funds even for direct sends that aren't tracked in CoinJoin history.

- **Spent Address Shows "used-empty" Instead of "new"**: Fixed a bug in `jm-wallet info --extended` where an address that previously had funds but was spent (outside of CoinJoin) would incorrectly show as "new" with 0 balance instead of "used-empty". The address display range calculation now correctly considers general blockchain activity (`addresses_with_history`) in addition to CoinJoin history.

- **Pending Transaction Timeout**: Maker now automatically marks pending CoinJoin transactions as failed after 60 minutes (configurable via `pending_tx_timeout_min` setting). This prevents the transaction history from being cluttered with entries that the taker never broadcast. Previously, these entries would remain in "pending" state indefinitely, causing repeated (and noisy) transaction lookup attempts in the logs.

- **Fix CoinJoin Address Labels Not Showing After Failed Retries**: Fixed a bug where addresses used in successful CoinJoin transactions would incorrectly show as "flagged" instead of "cj-out" (for CoinJoin outputs) or proper labels if the same address appeared in later failed transactions. This happened when a taker would retry using the same maker address multiple times, resulting in one successful entry and multiple failed entries in history. The fix ensures that successful transaction types take precedence - once an address is used in a confirmed CoinJoin, it keeps its "cj-out" or "change" label regardless of subsequent failed attempts.

- **Fix Address Reuse in Concurrent CoinJoin Sessions**: Fixed a critical privacy bug where the maker could reuse the same CoinJoin output and change addresses across multiple concurrent sessions. This occurred because addresses were only marked as "used" in history after the CoinJoin completed (when `!tx` was received), so a second `!fill` request arriving before the first completed would get the same addresses. The fix adds in-memory address reservation: when a maker sends `!ioauth` with addresses, those addresses are immediately reserved and will not be reused for subsequent sessions, even if the CoinJoin fails.

- **Mempool Min Fee Check for Wallet Send**: The `jm-wallet send` command now checks the fee rate against the node's mempool minimum fee (like the taker already does). If a manual `--fee-rate` is below the node's `minrelaytxfee`, a warning is logged and the mempool minimum is used instead, preventing "min relay fee not met" broadcast failures.

- **Minimum Relay Fee Documentation**: Added new section to DOCS.md explaining Bitcoin node fee rate configuration, including how to enable sub-satoshi fee rates via `minrelaytxfee` in `bitcoin.conf`.

- **Log Level CLI Flag Across All Components**: Added `--log-level` / `-l` flag to all CLI commands that were missing it:
  - `jm-maker start` and `jm-maker generate-address` commands
  - `jm-directory-server` CLI (status, health subcommands)
  - `jm-orderbook-watcher` main entry point
  - The flag accepts TRACE, DEBUG, INFO, WARNING, and ERROR levels (TRACE was not documented before)
  - Updated `config.toml.template` and settings documentation to include TRACE as a valid log level
  - Environment variable for log level is `LOGGING__LEVEL` (not `LOGGING__LOG_LEVEL` - the latter never worked)

- **Wallet Name in Startup Logs**: Both maker and taker now log the Bitcoin Core descriptor wallet name (e.g., `jm_xxxxxxxx_mainnet`) during startup when using the descriptor wallet backend. This makes it easier to identify which wallet is being used, especially when running multiple instances.

- **Accurate Fee Rate in Final Transaction Summary**: The taker's final transaction summary now displays the actual mining fee and fee rate calculated from the signed transaction. Previously, only the estimated fee was shown, which didn't account for residual/dust amounts absorbed into the fee when change would be below dust threshold. This is especially important for sweep transactions where the actual fee can be significantly higher than the estimate. The summary now shows actual vsize alongside byte size.

- **Automatic Password Prompt for Encrypted Mnemonics**: All CLI commands that load mnemonic files now automatically detect encrypted files (Fernet (AES)) and prompt for a password interactively. Previously, users had to explicitly pass `--password` on the command line, which led to confusing errors when trying to use encrypted mnemonic files. This works across `jm-taker`, `jm-maker`, and `jm-wallet` commands.

- **Password Confirmation Retry Loop**: The `jm-wallet import` and `jm-wallet generate` commands now retry password confirmation up to 3 times when passwords don't match, instead of immediately exiting. This improves the user experience by allowing correction of typos without having to restart the command.

- **BIP39 Passphrase Prompt for Maker/Taker**: Added `--prompt-bip39-passphrase` option to `jm-maker start` and `jm-taker coinjoin` commands. This allows users to enter their BIP39 passphrase interactively at startup rather than passing it via environment variable or command line argument.

- **Wallet Scan Start Height Setting**: New `scan_start_height` configuration option in `[wallet]` section allows specifying an explicit block height for initial wallet scanning. This is useful when you know when your wallet was first used, enabling faster initial sync for newer wallets.

- **Fee Rate Configuration Option**: Added `fee_rate` option to `[taker]` config section for manual fee rate specification in sat/vB. This takes precedence over `fee_block_target` when set, useful for users who prefer explicit fee rates over estimation.

- **Troubleshooting Documentation**: Added new "Troubleshooting" section to DOCS.md with:
  - Common `bitcoin-cli` debugging commands for wallet sync issues
  - Smart scan configuration tips for faster initial sync
  - RPC timeout troubleshooting guide

- **Reproducible Docker Builds**: All Docker images now support reproducible builds using `SOURCE_DATE_EPOCH`. This allows anyone to verify that released images were built from the published source code.
  - Dockerfiles updated to use `SOURCE_DATE_EPOCH` build arg for consistent timestamps
  - CI/CD workflows pass git commit timestamp to builds
  - Release workflow generates manifest files with image digests
  - New verification script: `scripts/verify-release.sh` to verify GPG signatures and image digests
  - New signing script: `scripts/sign-release.sh` for trusted parties to attest releases
  - GPG signature infrastructure in `signatures/` directory
  - Documentation added to DOCS.md and README.md

- **Directory Server Auto-Reconnection**: Makers now automatically attempt to reconnect to disconnected directory servers. This improves maker uptime and resilience against temporary network issues or directory server restarts. Previously, if a maker lost connection to a directory server during startup or due to network issues, it would remain disconnected indefinitely.
  - New config options: `directory_reconnect_interval` (default: 300s/5min) and `directory_reconnect_max_retries` (default: 0 = unlimited)
  - On successful reconnection, offers are automatically re-announced to the reconnected directory
  - Notifications are sent for both disconnections and successful reconnections

- **External Wallet Fidelity Bonds (Cold Storage Support)**: Added support for fidelity bonds with external wallet (hardware wallet/cold storage) private keys. The bond UTXO private key never needs to touch an internet-connected device. Instead, users create a certificate chain where the cold wallet signs a certificate authorizing a hot wallet keypair to sign nick proofs on its behalf.
  - New CLI commands:
    - `jm-wallet create-bond-address <pubkey>`: Create bond address from public key (no mnemonic needed)
    - `jm-wallet generate-hot-keypair`: Generate hot wallet keypair for certificate
    - `jm-wallet prepare-certificate-message`: Create message for hardware wallet signing
    - `jm-wallet import-certificate`: Import signed certificate into bond registry
  - Certificate chain: `UTXO keypair (cold) -> signs -> certificate (hot) -> signs -> nick proofs`
  - Security benefits: Bond funds remain completely safe in cold storage; certificate has configurable expiry (~2 years default); if hot wallet is compromised, only certificate is at risk
  - Compatible with hardware wallets via Sparrow Wallet message signing

- **Multi-Offer Support (Dual Offers)**: Makers can now advertise both relative and absolute fee offers simultaneously with different offer IDs. This allows makers to serve different types of takers (those preferring percentage-based fees vs fixed fees) from a single instance.
  - New `--dual-offers` CLI flag for `jm-maker start` creates both offer types automatically
  - Each offer type gets a unique offer ID (0 for relative, 1 for absolute)
  - !fill requests are routed to the correct offer based on the offer ID
  - Fidelity bond value is shared across all offers
  - Extensible architecture: `offer_configs` list in `MakerConfig` allows N offers (internal API, not yet exposed via CLI for simplicity)
  - Usage: `jm-maker start --dual-offers --cj-fee-relative 0.001 --cj-fee-absolute 500`

- **Wallet Import Command**: New `jm-wallet import` command to recover existing wallets from BIP39 mnemonic phrases. Features interactive word-by-word input with Tab completion (where readline is available), automatic word auto-completion when only one BIP39 word matches the prefix, suggestions display when multiple words match, mnemonic checksum validation after entry, and optional encryption of the saved wallet file. Supports 12, 15, 18, 21, and 24-word mnemonics.

### Fixed

- **Sweep Transaction Mining Fee Accuracy**: Fixed a bug where sweep transactions (taker with `amount=0`) would pay significantly higher mining fees than displayed at the start of the CoinJoin. The issue was caused by two problems:
  1. The `tx_fee_factor` randomization was applied when calculating the tx fee budget for sweep amount calculation, causing the budget to be up to 4x (with default `tx_fee_factor=3.0`) the base fee rate.
  2. At transaction build time, a new fee estimate with different randomization was used, creating a mismatch.

  With this fix:
  - Sweep fee budgets are calculated without randomization to ensure deterministic amounts
  - The same fee budget is used at both order selection and build time
  - The mining fee amount stays constant; only the effective fee rate may vary based on actual transaction size
  - Improved logging shows the tx fee budget, actual vsize, and effective fee rate

- **Log Level from Config/Env Ignored**: Fixed a bug where `LOGGING__LEVEL` environment variable and `[logging] level` config setting were ignored by CLI commands. The `--log-level` CLI argument worked correctly, but the env/config values were never applied because logging was configured before settings were loaded. Now the priority is: CLI argument > env/config > default ("INFO").

- **Maker cj_fee_absolute config setting ignored**: Fixed bug where setting `cj_fee_absolute` in `config.toml` had no effect because the maker always defaulted to relative fee offers. Added new `offer_type` setting to the `[maker]` config section that allows specifying which fee type to use: `sw0reloffer` (relative, default) or `sw0absoffer` (absolute). Previously, the only way to use absolute fees was via the `--cj-fee-absolute` CLI flag.

- **Install script missing python3-dev dependency**: Added `python3-dev` to the install script's dependency checks. This package is required for building Python C extensions (like the cryptography library used for wallet encryption). Previously, installations would fail when trying to install jmcore if this package was missing, and the script would exit before creating the activation script.

- **Tor cookie path auto-detection order**: Reordered the auto-detection paths for Tor cookie authentication to prioritize `/run/tor/control.authcookie` (common on Debian/Ubuntu with systemd) over `/var/lib/tor/control_auth_cookie`. Previously, the less common path was checked first, causing auto-detection to fail on most modern Linux systems.

- **Taker --fee-rate validation error with default fee_block_target**: Fixed bug where specifying `--fee-rate` on the CLI would fail with "Cannot specify both fee_rate and fee_block_target" error even when fee_block_target was not explicitly set. The issue was that `build_taker_config()` unconditionally fell back to `wallet.default_fee_block_target` (default: 3) even when `fee_rate` was provided. Now `fee_block_target` is only set when `fee_rate` is not provided.

- **Channel consistency check allows messages from different directory servers**: Fixed false positive channel consistency violations when taker messages arrived via different directory servers. The JoinMarket protocol broadcasts messages to ALL directory servers, so receiving `!auth` from `dir:serverA` after `!fill` from `dir:serverB` is expected behavior. The check now only validates that "direct" and "directory" channel types are not mixed, not the specific server identity.

- **Direct message parse failures now logged with content**: When the maker fails to parse a direct message, the log now includes a preview of the message content (truncated to 100 chars) to aid debugging. Previously only logged "Failed to parse direct message" with no indication of what was received.

- **Rate limiting for direct message parse failure warnings**: Parse failure warnings are now rate-limited (1 per 10 seconds per peer) to prevent log spam when receiving repeated malformed messages from the same peer.

- **Chunked PEERLIST responses**: Directory server now sends PEERLIST responses in chunks of 20 peers instead of a single massive message. This fixes timeout issues when receiving large peerlists over slow Tor connections. Previously, mainnet directories with hundreds of peers would frequently timeout because the entire peerlist had to be transmitted in one message. The client now accumulates peers from multiple PEERLIST messages, using a 5-second inter-chunk timeout to detect when all chunks have been received.

- **CoinJoin output destination address path**: Changed INTERNAL destination addresses to use internal chain (/1) instead of external chain (/0). This matches the reference implementation where all JoinMarket-generated addresses (CJ outputs and change) use the internal branch, while external (/0) is reserved for user-facing deposit addresses.

- **Fee rate randomization (tx_fee_factor)**: Changed from a simple multiplier (default 3.0x) to randomization like the reference implementation. Fees are now randomized between `base_fee` and `base_fee * (1 + tx_fee_factor)` for privacy. Default changed from 3.0 to 0.2 (20% randomization range). Set to 0 to disable randomization.

- **Fee rate resolution with mempool minimum**: Fee estimation now checks against mempool minimum fee and uses the higher value. Manual fee rates below mempool minimum trigger a warning and use mempool minimum instead. This prevents transactions from being rejected due to insufficient fee.

- **Interactive UTXO selection (--select-utxos) logging**: Improved logging for `--select-utxos` in sweep mode to better indicate whether UTXOs were manually selected or all UTXOs were used. This helps debug cases where the interactive selector might not appear.

### Improved

- **BIP39 Passphrase Documentation**: Expanded DOCS.md to clarify that `jm-wallet import` only stores the mnemonic without the BIP39 passphrase. The passphrase is provided when using the wallet (via `--bip39-passphrase`, `--prompt-bip39-passphrase`, or `BIP39_PASSPHRASE` env var).

- **Config Template Clarity**: Improved `config.toml.template` comments to:
  - Distinguish "coinjoin fees" (paid to makers) from "network/miner fees"
  - Document `fee_rate` option precedence over `fee_block_target`
  - Explain smart scan and background rescan behavior for wallet import

- **Orderbook watcher feature detection**: Fixed race condition where offers from new makers were stored with empty features before the peerlist response arrived. Now when peerlist response arrives with features, all cached offers for those makers are retroactively updated with the correct features.

- **Peer location updates now include features**: Fixed directory server to include peer features (neutrino_compat, peerlist_features) in peer location update messages sent after private message routing. Previously, when a client learned about a new peer through a PEERLIST update (not via explicit GETPEERLIST request), the features were missing. This caused orderbook watchers to miss feature information for makers discovered through private message routing.

- **Faster feature discovery for new makers**: Improved orderbook watcher feature discovery timing:
  - Added immediate feature discovery (30 seconds after startup) instead of waiting 10 minutes for first health check
  - Reduced initial health check delay from 10 minutes to 2 minutes
  - Added automatic feature discovery for makers without features after each peerlist refresh (every 5 minutes)
  - Direct health checks now populate features in directory client caches, ensuring offers are tagged with correct features

- **Feature merging across directories**: Fixed issue where maker features (neutrino_compat, peerlist_features) were being overwritten instead of merged when receiving updates from multiple directory sources. When a PEERLIST came from a reference directory (no features), it would overwrite features previously learned from an NG directory. Now features are properly merged: once we learn a feature for a nick, we keep it. This ensures the orderbook watcher and taker correctly detect maker capabilities regardless of which directory responds first.

- **Multiple offers per maker with same bond**: Fixed bond deduplication in orderbook watcher incorrectly dropping offers when a maker advertises multiple offer IDs (e.g., oid=0 and oid=1) backed by the same fidelity bond. Previously, only one offer was kept per bond UTXO. Now the deduplication key includes both the bond UTXO and offer ID, preserving all distinct offers from the same maker while still deduplicating when different nicks share the same bond (maker restart scenario).

- **Maker direct connection handshake support**: Makers now respond to handshake requests on direct connections (via their hidden service). This enables health checkers and feature discovery tools to connect directly to makers and discover their features (neutrino_compat, peerlist_features) without relying on directory server peerlists. Previously, direct connections only handled CoinJoin protocol messages (fill, auth, tx, push), causing health checks to time out and feature discovery to fail for NG makers.

- **Direct connection orderbook requests**: Makers now properly handle `!orderbook` requests received via direct connection (PUBMSG type 687). Previously, orderbook requests sent over direct connections were ignored with "Failed to parse direct message" warnings, because the maker only handled PRIVMSG (type 685) on direct connections. This was causing repeated warnings like `'{"type": 687, "line": "J5xxx!PUBLIC!orderbook"}'`. Now these requests are processed with the same rate limiting as directory-relayed requests.

- **Improved rate limiting and ban logging**: Added DEBUG/TRACE level logging throughout the rate limiter to help diagnose peer behavior:
  - TRACE: Logs each allowed request
  - DEBUG: Logs each rate-limited request with violation count, backoff level, and wait time
  - DEBUG: Logs when banned peer requests are rejected (with remaining ban time)
  - DEBUG: Logs when ban expires and peer state is reset
  - WARNING: Ban events now include the final backoff level for context

- **Improved PoDLE verification logging**: Added DEBUG/TRACE level logging for PoDLE proof verification to help diagnose authentication issues:
  - TRACE: Logs verification inputs (P, P2, sig, e, commitment - truncated)
  - DEBUG: Logs full PoDLE details on success (taker, utxo, commitment)
  - DEBUG: Logs detailed failure reasons including commitment/utxo info
  - DEBUG: Logs UTXO validation details (value, confirmations)
  - DEBUG: Logs specific rejection reasons (too young, too small)

- **Peer feature logging in handshake**: Makers now log advertised peer features (version, network, features) at DEBUG level when receiving handshake requests on direct connections. This helps diagnose feature negotiation and compatibility issues. Supports both reference implementation format (dict: `{"peerlist_features": true}`) and NG format (comma-string: `"neutrino_compat,peerlist_features"`).

- **Improved direct message parse failure logging**: Parse failures now log the full message content at DEBUG level (in addition to the rate-limited WARNING with truncated preview). This helps diagnose protocol issues without flooding logs.

## [0.10.0] - 2026-01-15

### Security

- **Sensitive data protection**: Refactored configuration models to use Pydantic's `SecretStr` type for sensitive fields (mnemonics, passphrases, passwords, destination addresses, notification URLs). This prevents accidental exposure of sensitive data in logs, error messages, and tracebacks. All sensitive values are automatically masked as `**********` in string representations and logging output, while remaining accessible via `.get_secret_value()` when needed.

### Fixed

- **Config file section headers**: Fixed config.toml.template to have all section headers (like `[bitcoin]`, `[tor]`, `[maker]`, etc.) uncommented by default. Previously, users would uncomment individual settings but forget to uncomment the section header, causing the settings to be silently ignored by the TOML parser. This led to confusion where config file settings appeared to be ignored even though they were correctly uncommented.
- **Config file error handling**: Improved error handling for malformed config.toml files. The application now exits immediately with exit code 1 and displays a clear error message when the config file has invalid TOML syntax (e.g., missing closing brackets, invalid characters). Previously, parsing errors were silently logged as warnings, and the application would continue with default values, making it difficult to diagnose configuration issues.
- **jm-directory-ctl config compliance**: Fixed `jm-directory-ctl status` and `jm-directory-ctl health` commands to respect `directory_server.health_check_host` and `directory_server.health_check_port` settings from config.toml. Previously, these commands always used hardcoded defaults (127.0.0.1:8080) and ignored the config file.
- **jm-wallet generate-bond-address config compliance**: Fixed `jm-wallet generate-bond-address` to respect `network_config.network` and `data_dir` settings from config.toml when CLI arguments are not provided. Previously, it always defaulted to mainnet and used hardcoded data directory logic.
- **jm-taker clear-ignored-makers config compliance**: Fixed `jm-taker clear-ignored-makers` to respect `data_dir` setting from config.toml when the `--data-dir` argument is not provided.
- **Orderbook watcher feature detection**: Fixed orderbook watcher to correctly identify JoinMarket NG makers' features (neutrino_compat, peerlist_features). Two issues resolved: (1) When new makers join after orderbook watcher startup, their features weren't being discovered until the next periodic peerlist refresh (5 minutes) or health check (15 minutes). Now the orderbook watcher immediately requests peerlist when discovering new peers to fetch their features. (2) Health checker now properly advertises peerlist_features support in its handshake to extract maker features, and merges these features with offers even when peerlist has already provided some features (health check provides authoritative confirmation via direct connection).
- **Taker pending transaction update on exit**: Fixed issue where taker CoinJoin transactions remained marked as `[PENDING]` in history after successful broadcast. The taker now immediately checks transaction status (mempool for full nodes, block confirmation for Neutrino) right after recording the history entry, before the CLI exits. Additionally, `jm-wallet info` now automatically updates the status of any pending transactions found in history, acting as a safeguard for transactions that confirm after the taker process has exited.
- **Spent address tracking in descriptor wallet**: Fixed issue where addresses that had been used but fully spent (zero balance) were not being tracked in `addresses_with_history`. The descriptor wallet backend now uses `listtransactions` RPC to fetch all addresses with any transaction history, ensuring the wallet correctly tracks which addresses have been used even if they no longer have UTXOs. This prevents address reuse and ensures `jm-wallet info` shows the correct next address.
- **Signature Ordering Mismatch**: Fixed critical bug where maker signatures were matched to the wrong transaction inputs, causing `OP_EQUALVERIFY` failures during broadcast. Root cause: signatures from the reference maker are sent in **transaction input order** (sorted by position in the serialized tx), not in the order UTXOs were originally provided in the `!ioauth` response. The taker now correctly matches signatures to transaction inputs by finding maker UTXOs in the actual transaction input order, rather than assuming they match the `!ioauth` order.
- **Slow Signature Processing**: Fixed 60-second delay between receiving signatures and processing them. Two issues: (1) For `!sig` responses (which expect multiple messages per maker), the loop condition `accumulate_responses and responses` kept waiting for the full timeout even after all signatures were received. Now uses `expected_counts` parameter to know when all signatures are collected. (2) Directory clients were polled sequentially, each waiting up to 5 seconds. Now polls all directories concurrently with `asyncio.gather()` using shorter 1-second chunks to allow more frequent checking of the direct message queue.
- **Sweep Mode CJ Amount Preservation**: Fixed critical bug where reference makers would reject sweep transactions with "wrong change". Root causes: (1) In sweep mode, the taker was recalculating `cj_amount` in `_phase_build_tx` when actual maker inputs differed from the initial estimate. Since makers calculate their expected change based on the original `cj_amount` from the `!fill` message, this recalculation caused a mismatch. (2) The initial tx_fee estimate used only 2 inputs per maker, which was insufficient when makers provided 6+ UTXOs, causing negative residual. The fix: (a) Preserve the original `cj_amount` sent in `!fill` - any tx_fee difference becomes additional miner fee (residual), (b) Use conservative tx_fee estimate (2 inputs/maker + 5 buffer) to minimize negative residual cases, (c) Fail gracefully with clear error when a maker provides many UTXOs causing negative residual (rare edge case).
- **Smart Message Routing**: Fixed `CryptError` with reference makers caused by duplicate `!fill` messages resetting session keys. Taker now intelligently routes messages via a single directory instead of broadcasting to all connected directories.
- **Session Channel Consistency**: Fixed critical protocol error where taker would mix communication channels (directory relay for `!fill`, direct connection for `!auth`) within a single CoinJoin session. This caused reference makers to reject messages as they appeared to be from different sessions. Taker now establishes ONE communication channel per maker before sending `!fill` and uses ONLY that channel for all subsequent messages (`!auth`, `!tx`, `!push`) in that session. Channel selection: tries direct connection first (5s timeout), falls back to directory relay if unavailable.
- **Directory Signature Verification**: Fixed `hostid` used for signing directory-relayed messages. Now correctly uses the fixed `"onion-network"` hostid (matching the reference implementation in `jmdaemon/onionmc.py`) instead of the directory's hostname. Previously, messages relayed through directories were signed with the wrong hostid, causing "nick signature verification failed" errors on reference makers.
- **Direct Peer Connection Message Signing**: Fixed message signing for direct peer-to-peer Tor connections. Messages sent via direct onion connections now include the required signature (pubkey + sig) that reference makers expect. Previously, direct connection messages were sent without signatures, causing reference makers to reject them with "Sig not properly appended to privmsg". The fix adds `nick_identity` parameter to `OnionPeer` and uses `ONION_HOSTID` ("onion-network") as the hostid for signing, matching the reference implementation's expectations.
- **Notification Configuration**: Fixed notification system to respect config file settings. Previously, notifications only read from environment variables (`NOTIFY_URLS`, etc.), completely ignoring the `[notifications]` section in `config.toml`. Now the notification system uses the unified settings system (config file + env vars + CLI args), with proper precedence: CLI args > environment variables > config file > defaults. All components (taker, maker, orderbook watcher, directory server) have been updated to pass settings to `get_notifier()`.
- **Fidelity Bond Verification**: Fixed a bug where fidelity bonds were parsed but not verified against the blockchain, causing their value to be 0. This prevented bond-weighted maker selection from working correctly, falling back to random selection. Taker now verifies bond UTXOs and calculates their value before maker selection.
- **Maker Selection Strategy**: Fixed maker selection to use deterministic mixed bonded/bondless strategy. The bondless allowance determines the proportion of maker slots using fair rounding: with 3 makers and 12.5% allowance, round(3 × 0.875) = 3 bonded slots. Bonded slots are filled by bond-weighted selection (prioritizing high-bond makers), while bondless slots are filled randomly from ALL remaining offers (both bonded and bondless makers, with equal probability). "Bondless" means bond-agnostic, not anti-bond. This ensures bonded makers are consistently rewarded while still supporting new/bondless makers. If insufficient bonded makers exist, remaining slots are filled from all available offers (optionally requiring zero-fee via `bondless_require_zero_fee` flag).
- **Orderbook Timeout**: Increased orderbook request timeout from 10s to 120s based on empirical testing. The previous timeout was missing ~75-80% of available offers. New timeout captures ~95% of offers (95th percentile response time is ~101s over Tor).
- **Peer-to-Peer Handshake Format**: Fixed message format for direct peer connections to use `{"type": 793, "line": "<json>"}` format, matching reference implementation (was using `{"type": 793, "data": {...}}`).
- **Maker Replacement Selection**: Fixed maker replacement to exclude makers already in the current session. Previously, a maker that already responded could be incorrectly re-selected as a replacement, causing commitment rejection errors.
- **Taker peerlist handling**: Fixed taker peerlist handling that was previously ignored. This way we start colelcting peer features and onion addresses earlier.
- **Minimum makers default**: Changed `minimum_makers` default from 2 to 1 (taker + 1 maker = 2 participants).
- **UTXO selection timing**: Moved UTXO selection (including interactive selector) before orderbook fetch to avoid wasting user time if they cancel.
- **Log verbosity**: Changed fee filtering logs from DEBUG to TRACE to reduce noise.
- **Ignored makers persistence**: Ignored makers list now persists across taker sessions in `~/.joinmarket-ng/ignored_makers.txt`. New CLI command `jm-taker clear-ignored-makers` to clear the list.
- **Blacklisted commitment handling**: Fixed taker to not permanently ignore makers who reject due to a blacklisted commitment. When a maker rejects a commitment as blacklisted, the taker now retries with a different commitment (different NUMS index or UTXO) instead of permanently ignoring that maker. The maker might accept a different commitment, so they should remain available for future attempts.
- **Self-broadcast fallback on already-spent inputs**: Fixed taker broadcast fallback to recognize when a maker has already successfully broadcast the CoinJoin transaction. When self-broadcast fails with "bad-txns-inputs-missingorspent" (UTXOs already spent) or similar errors, the taker now verifies if the CoinJoin transaction exists on-chain before reporting failure. This handles multi-node setups where the maker's broadcast propagates before the taker's verification can confirm it.
- **Wallet history status display**: Fixed `jm-wallet history` to show `[PENDING]` for unconfirmed transactions instead of incorrectly showing `[FAILED]`. Pending transactions (waiting for first confirmation) are now clearly distinguished from actually failed transactions.
- **Wallet info address display**: Fixed `jm-wallet info` to show the next address after the last used one (highest used index + 1) instead of the next unused address. This prevents showing index 0 when higher indexes have been used, making it clear which addresses have been utilized. The display now ignores gaps in the address sequence and always shows the address immediately following the highest used index, considering all usage sources (blockchain history, current UTXOs, and CoinJoin history).

### Added

- **Centralized Version Management**: Introduced a single source of truth for project versioning in `jmcore/src/jmcore/version.py`. All components now import their `__version__` from this central location, ensuring consistency across the project. The version is also accessible via `jmcore.VERSION`, `jmcore.get_version()`, and `jmcore.get_version_info()`.
- **Directory Server Version in MOTD**: Directory servers now advertise the JoinMarket NG version in their MOTD (Message of the Day), similar to the reference implementation. The format is: `JOINMARKET VERSION: X.Y.Z`. This helps clients identify the server software version.
- **Version Bump Script**: New `scripts/bump_version.py` automates the release process by updating all version files, preparing the changelog (adding version header and date, preserving Unreleased section, adding diff link), updating `install.sh`, creating a git commit with a standard message (`release: X.Y.Z`), and tagging. Usage: `python scripts/bump_version.py 0.10.0 --push`
- **Orderbook watcher directory metadata display**: The orderbook watcher web UI now displays directory server metadata including MOTD (message of the day), protocol version (e.g., v5 or v5-6), and supported features (e.g., neutrino_compat, peerlist_features). This information appears in the "Offers per Directory Node" section, helping users understand the capabilities and configuration of each directory server.
- **Interactive UTXO Selection for Taker**: Added `--select-utxos` / `-s` flag to `jm-taker coinjoin` command, enabling interactive UTXO selection before CoinJoin execution. Uses the same fzf-like TUI as `jm-wallet send`, allowing users to manually choose which UTXOs to include in the CoinJoin transaction. Works with both sweep mode and normal CoinJoin mode.
- **Orderbook Response Measurement Tool**: New `scripts/measure_orderbook_delays.py` tool to measure response time distribution when requesting orderbooks from directory servers over Tor. Helps validate timeout settings empirically.
- **Direct Peer Connections**: Taker can now establish direct Tor connections to makers, bypassing directory servers for private message exchange.
  - Improves privacy by preventing directories from observing who is communicating with whom
  - Attempts to establish direct connections before sending `!fill` (5s timeout, no added latency if unavailable)
  - Once a channel is chosen (direct or directory), ALL messages to that maker use the same channel
  - Automatic fallback to directory relay if direct connection fails
  - Connection attempts use exponential backoff to avoid overwhelming peers
  - Enabled by default (`prefer_direct_connections=True` in `MultiDirectoryClient`)
  - New `OnionPeer` class in `jmcore.network` handles direct peer connection lifecycle

- **Maker Replacement on Non-Response**: Taker now automatically replaces non-responsive makers during CoinJoin.
  - New config option: `max_maker_replacement_attempts` (default: 3, range: 0-10)
  - If makers fail to respond during fill or auth phases, taker selects replacements from orderbook
  - Failed makers are added to an ignored list to prevent re-selection
  - Replacement makers go through the full handshake (fill + auth phases)
  - Setting to 0 disables replacement (original behavior: fail immediately)
  - Improves CoinJoin success rate when some makers are unresponsive or drop out

- **Simplified Installation**: New one-line installation with automatic updates.
  - Install: `curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash`
  - Update: `curl -sSL ... | bash -s -- --update`
  - Installs from tagged releases via pip (no git clone required)
  - Creates shell integration at `~/.joinmarket-ng/activate.sh`
  - Unified install/update mode with automatic detection of existing installations

- **Configuration File Support**: Added TOML configuration file (`~/.joinmarket-ng/config.toml`) for persistent settings.
  - Configuration priority: CLI args > environment variables > config file > defaults
  - Auto-generated template with all settings commented out on first run
  - Users only uncomment settings they want to change, facilitating software updates
  - New `config-init` command for maker and taker to initialize the config file
  - Unified settings model in `jmcore.settings` using pydantic-settings

- **Interactive UTXO Selection TUI**: New `--select-utxos` / `-s` flag for `jm-wallet send` command.
  - fzf-like curses interface for manually selecting UTXOs
  - Navigate with arrow keys or j/k, toggle selection with Tab/Space
  - Shows mixdepth, amount (sats and BTC), confirmations, and outpoint
  - Visual indicators for timelocked fidelity bond UTXOs
  - Real-time display of selected total vs target amount
  - Keyboard shortcuts: a (select all), n (deselect all), g/G (top/bottom)

### Changed

- **Renamed `full_node` backend to `scantxoutset`** for clarity. The backend type has been renamed to better reflect what it does (uses Bitcoin Core's `scantxoutset` RPC to scan the UTXO set). This is an alternative backend that should not be recommended for general usage - `descriptor_wallet` is the preferred default for full nodes. Updated all documentation to reflect this change and removed examples about the `scantxoutset` backend from tutorials.
- **Environment Variable Naming Standardization**: Standardized environment variable naming to use double underscore (`__`) for nested settings, following pydantic-settings convention.
  - Old format: `TOR_SOCKS_HOST`, `NOTIFY_URLS`
  - New format: `TOR__SOCKS_HOST`, `NOTIFICATIONS__URLS`
  - Consolidated `TorSettings` and `TorControlSettings` into a single `TorSettings` model
  - Tor control settings now use `TOR__CONTROL_ENABLED`, `TOR__CONTROL_HOST`, `TOR__CONTROL_PORT`, `TOR__COOKIE_PATH`
  - Updated all Docker Compose files to use the new format
  - Config template no longer shows separate `[tor_control]` section (now part of `[tor]`)
- **Installation path**: Virtual environment now lives at `~/.joinmarket-ng/venv/` (was `jmvenv/` in repo)
- **Documentation**: Updated all READMEs to use config file approach instead of .env files
- **Directory connections now parallel**: Taker and orderbook watcher connect to all directory servers concurrently instead of sequentially.
  - Significantly reduces startup time when connecting to multiple directories (especially over Tor).
  - Directory orderbook fetching is also parallelized.
- **Removed peerlist-based offer filtering**: Directory's orderbook is now trusted as authoritative.
  - If a maker has an offer in the directory, they are considered online.
  - Peerlist responses may be delayed or unavailable over Tor, so offers are no longer filtered based on peerlist presence.
  - This prevents incorrectly rejecting valid offers from active makers.
- **Enhanced CoinJoin routing visibility**: Taker now logs detailed message routing information during CoinJoin.
  - Shows which directory servers are used to relay messages to makers.
  - Displays maker onion addresses in the transaction confirmation prompt.
  - Debug logs show routing details for !fill, !auth, !tx, and !push messages.
  - Indicates whether messages are sent via direct connection or directory relay.

## Fixed

- **Wallet Info Shows Next Unused Address**: The `jm-wallet info` command now displays the first unused address (next index after highest used) instead of always showing index 0. This allows users to quickly grab an address for depositing without manual derivation path lookups.
- **Address reuse after internal send**: Fixed address reuse bug where `get_next_address_index` would return an already-used address index after funds were spent.
  - Now properly considers `addresses_with_history` (addresses that ever had UTXOs, including spent ones).
  - Always returns the next index after the highest used, never reusing lower indices even if they appear empty.
  - Prevents privacy leaks from address reuse after internal sends or CoinJoins.
- **Signature base64 padding error**: Fixed "Incorrect padding" errors when decoding maker signatures.
  - Base64 strings without proper padding are now handled correctly.
- **PoDLE commitment blacklist retry**: Taker now automatically retries with a new NUMS index when a maker rejects due to blacklisted commitment.
  - Previously, a blacklisted commitment would cause the entire CoinJoin to fail.
  - Now retries up to `taker_utxo_retries` times (default 3) with different commitment indices.

## [0.9.0] - 2026-01-12

### Added

- **Descriptor Wallet Backend now exposed via CLI**: Users can now select `--backend descriptor_wallet` for fast UTXO tracking.
  - Available in all CLIs: `jm-wallet`, `jm-maker`, `jm-taker`
  - Uses Bitcoin Core's `importdescriptors` for one-time wallet setup
  - Fast syncs via `listunspent` (~1s vs ~90s for scantxoutset)
  - Automatic descriptor import and wallet setup on first use
  - **New default backend** for maker, taker, and wallet commands (changed from `full_node`)
  - Docker compose examples updated to use `descriptor_wallet` by default
- **Orderbook Watcher: Maker direct reachability tracking**.
  - Each offer now includes `directly_reachable` field (true/false/null) showing if maker is reachable via direct Tor connection.
  - Health checker extracts maker features from handshake, useful when directory servers don't provide peerlist features.
  - Reachability info available in orderbook.json API response for monitoring and debugging.
  - Note: Unreachable makers are NOT removed from orderbook - directory may still have valid connection.
- **Operator Notifications**: Push notification system via Apprise for CoinJoin events.
  - Supports 100+ notification services (Gotify, Telegram, Discord, Pushover, email, etc.)
  - Privacy-aware: configurable amount/txid/nick inclusion
  - Per-event toggles for fine-grained control
  - Fire-and-forget: notifications never block protocol operations
  - Components integrated: Maker, Taker, Directory Server, Orderbook Watcher
  - Docker images now include `apprise` by default for notification support
- **DescriptorWalletBackend**: New Bitcoin Core backend using descriptor wallets for efficient UTXO tracking.
  - Uses `importdescriptors` RPC for one-time wallet setup
  - Uses `listunspent` RPC for fast UTXO queries (O(wallet) vs O(UTXO set))
  - Persistent tracking: Bitcoin Core maintains UTXO state automatically
  - Real-time mempool awareness: sees unconfirmed transactions immediately
  - Deterministic wallet naming based on mnemonic fingerprint
- `setup_descriptor_wallet()` method in WalletService for one-time descriptor import
- `sync_with_descriptor_wallet()` method for fast wallet sync via listunspent
- Helper functions `generate_wallet_name()` and `get_mnemonic_fingerprint()` for deterministic wallet naming
- Early backend connection validation in taker CLI before wallet sync.
- Estimated transaction fee logging before user confirmation prompt (assumes 1 input per maker + 20% buffer).
- Final transaction summary before broadcast with exact input/output counts, maker fees, and mining fees.
- Support for broadcast confirmation callback to allow user to review transaction before broadcasting.
- `has_mempool_access()` method to BlockchainBackend for detecting mempool visibility.
- `BroadcastPolicy.MULTIPLE_PEERS` - new broadcast policy that sends to N random makers (default 3).
- `broadcast_peer_count` configuration parameter to control number of peers for MULTIPLE_PEERS policy.
- Unified broadcast behavior between full node and Neutrino clients.
- Comprehensive backend comparison documentation in jmwallet README with performance characteristics and use cases.
- **Smart Scan for Descriptor Wallet**: Fast startup for descriptor wallet import on mainnet.
  - Initial import only scans ~1 year of blockchain history (52,560 blocks)
  - Reduces first-time wallet sync from 20+ minutes to seconds on mainnet
  - Background full rescan runs automatically to ensure no old transactions are missed
  - Configurable via `smart_scan`, `background_full_rescan`, `scan_lookback_blocks` in WalletConfig

### Changed

- **Default backend changed from `scantxoutset` to `descriptor_wallet`** for all components (maker, taker, wallet CLI).
  - Scantxoutset (formerly `full_node`) still available via `--backend scantxoutset`
  - Provides significant performance improvement for ongoing operations (~1s vs ~90s per sync)
  - Docker compose examples updated to use descriptor_wallet by default
- Fee rate handling improvements:
  - Changed default fee rate from 10 sat/vB to 1 sat/vB fallback.
  - Added support for sub-1 sat/vB fee rates (float instead of int).
  - Added `--block-target` option for fee estimation (1-1008 blocks).
  - Added `--fee-rate` option for manual fee rate (mutually exclusive with `--block-target`).
  - Default behavior: 3-block fee estimation when connected to full node.
  - Neutrino backend: falls back to 1 sat/vB (cannot estimate fees).
  - Error when `--block-target` is used with neutrino backend.
- Backend `estimate_fee()` now returns `float` for precision with sub-sat rates.
- Added `can_estimate_fee()` method to backends for capability detection.
- Increased default counterparty count from 3 to 10 makers.
- Reduced logging verbosity: parsed offers, fidelity bond creation, and Neutrino operations now logged at DEBUG level.
- Improved sweep coinjoin logging: initial "Starting CoinJoin" message now shows "ALL (sweep)" instead of "0 sats".
- **Default broadcast policy changed from RANDOM_PEER to MULTIPLE_PEERS** (sends to 3 random makers).
- **Unified broadcast behavior**: All policies (SELF, RANDOM_PEER, MULTIPLE_PEERS, NOT_SELF) work
  the same way for both full node and Neutrino backends. The only difference is Neutrino skips
  mempool verification when falling back to self-broadcast.
- RANDOM_PEER and MULTIPLE_PEERS now allow self-fallback if all makers fail (both full node and Neutrino).
- Neutrino pending transaction timeout reduced from 48h to 10h before warning.
- Neutrino pending transaction monitoring uses block-based UTXO verification (cannot access mempool).
- Neutrino backend UTXO detection improved with incremental rescans and retries for better robustness.

### Fixed

- **Taker failing when Maker uses multiple UTXOs**: Fixed handling of multiple `!sig` messages from makers with multiple inputs.
- **Orderbook Watcher peerlist timeout with JoinMarket NG directories**: Fixed incorrect timeout handling when directory announces `peerlist_features` during handshake.
  - Directories announcing `peerlist_features` now use a longer timeout (120s vs 30s) for peerlist requests over Tor.
  - Timeout on directories with `peerlist_features` no longer permanently disables peerlist requests (the peerlist may simply be large and slow to transmit).
  - Improved log messages to distinguish between "likely reference implementation" timeouts and "large peerlist or slow network" timeouts.
- **Orderbook Watcher bond deduplication logging noise**: Fixed false "stale offer replacement" logs when the same offer from the same maker was seen from multiple directories.
  - Same (nick, oid) pairs are now silently deduplicated instead of logging as "stale replacement".
  - Only logs when an actual different maker reuses the same bond UTXO (e.g., after nick restart).
- **Orderbook Watcher aggressive offer pruning**: Fixed overly aggressive cleanup that was removing valid offers.
  - **Removed age-based staleness cleanup entirely** - makers can run for months, offer age is not a valid signal.
  - Maker health check no longer removes offers from makers that are unreachable via direct connection (directory may still have valid connection).
  - Peerlist-based cleanup now skips if any directory refresh fails (avoids false positives).
  - Philosophy changed to **"show offers when in doubt"** rather than aggressive pruning.
  - Only removes offers when explicitly signaled by directory (`;D` disconnect marker or nick absent from ALL directories' peerlists).
- **Orderbook Watcher showing only few offers despite receiving many from directories**.
  - Directory servers send realtime PEERLIST updates (one per peer) when peers connect/disconnect.
  - DirectoryClient was incorrectly treating these partial updates as complete peerlist replacements.
  - Now accumulates active peers from partial responses instead of replacing the entire list.
  - Only removes offers for nicks explicitly marked as disconnected (`;D` suffix).
  - Periodic peerlist refresh now collects active nicks from ALL directories before cleanup.
  - This fixes orderbooks being pruned down to just the most recently seen makers.
- Critical maker transaction fee calculation bug causing "Change output value too low" errors.
  - Maker `txfee` from offers is the total transaction fee contribution (in satoshis), not per-input/output.
  - Previously incorrectly multiplied `offer.txfee` by `(num_inputs + num_outputs + 1)`, causing maker change calculations to fail.
  - Now correctly uses `offer.txfee` directly as per JoinMarket protocol specification.
- Concurrent read bug in TCPConnection causing "readuntil() called while another coroutine is already waiting" errors.
  - Added receive lock to serialize concurrent `receive()` calls on the same connection.
  - This fixes race conditions when `listen_continuously()` and `get_peerlist_with_features()` run concurrently.
- Wallet address alignment in `jm-wallet info --extended` output.
  - Fixed misalignment when address indices transition from single to double digits (e.g., 9 to 10).
  - Derivation paths now use fixed-width padding (24 characters) for consistent column alignment.

## [0.8.0] - 2026-01-08

### Added

- Support for multiple directory servers with message deduplication.
- Maker health checking via direct onion connection.
- BIP39 passphrase support for wallets (CLI and component integration).
- BIP84 zpub support for native SegWit wallets.
- Auto-discovery for fidelity bonds and timenumber utilities.
- Configuration for separate Tor hidden service targets (split onion serving host).
- Tests for BIP39 passphrase and multi-directory functionality.

### Fixed

- Flaky E2E tests regarding taker commitment clearing and neutrino blacklist resetting.
- Detection of peer count after CoinJoin confirmation in Maker bot.

## [0.7.0] - 2026-01-03

### Added

- Generic per-peer rate limiting across all components.
- Specific rate limiting for orderbook requests to prevent spam.
- Fidelity bond proof compatibility and analysis tool.
- Exponential backoff and banning for orderbook rate limiter.
- Docker multi-architecture builds (ARM support).
- Periodic directory connection status logging.
- `INSTALL.md` with detailed installation instructions.
- Support for `MNEMONIC_FILE` environment variable.
- SimpleX community link to README.

### Changed

- Unified data directory to `~/.joinmarket-ng`.
- Improved Dockerfile efficiency with multi-stage builds.
- Moved to `prek` action for CI.
- Renamed project title to JoinMarket NG in documentation and orderbook watcher.

### Fixed

- Linking of standalone fidelity bonds to offers in Orderbook Watcher.
- Maker orderbook rate limit logging.
- Docker layer caching for ARM builds.

## [0.6.0] - 2025-12-28

### Added

- Persistence for PoDLE commitment blacklist.
- Tracking of CoinJoin transaction confirmations in wallet history.
- Stale offer filtering.
- UTXO max PoDLE retries for makers.
- Advanced UTXO selection strategies for takers and makers.
- Configurable dust threshold for CoinJoin transactions.
- Periodic wallet rescan.
- CoinJoin notifier script.

### Changed

- Redesigned dependency management.
- Moved `CommitmentBlacklist` to `jmcore`.
- Moved to integer satoshi amounts for Bitcoin values to avoid float issues.

### Fixed

- Maker change calculation bug causing negative change.
- Directory server message routing concurrency.
- Fee estimation and Bitcoin units display format.
- Maker sending fidelity bonds via PRIVMSG.

## [0.5.0] - 2025-12-21

### Added

- Protocol v5 extension feature for Neutrino support.
- Feature negotiation via handshake (`neutrino_compat`).
- Push broadcast policy for taker.
- Auto-miner for regtest in Docker Compose.
- Mnemonic generation, encryption, and fidelity bond generation.
- JSON-line message parsing limits to prevent DoS.
- Support for Tor ephemeral hidden services and Cookie Auth.

### Changed

- Migrated from `cryptography` to `coincurve` for ECDSA operations.
- Adopted feature flags instead of strict protocol version bumps.
- Consolidated documentation into `DOCS.md`.

### Fixed

- Taker fee limit checks.
- Fidelity bond proof verification and generation.
- Reference implementation compatibility.

## [0.4.0] - 2025-12-14

### Added

- Complete Maker Bot implementation with fidelity bonds and signing.
- Taker implementation with input signing.
- Neutrino backend integration.
- `AGENTS.md` for AI agents documentation.
- Comprehensive E2E tests with Docker Compose.

### Changed

- CI workflow to always run all tests.
- Updated READMEs for components.

### Fixed

- Blockchain height consistency in E2E tests.
- GitHub Actions workflow to start Bitcoin Regtest node properly.

## [0.3.0] - 2025-12-07

### Added

- Health check and monitoring features to Directory Server.
- Fidelity bond offer counts to directory stats.
- Docker health check for directory server.
- Debug Docker image with `pdbpp` and `memray`.

### Changed

- Increased max message size to 2MB.
- Increased max peers limit to 10000.
- Set log level to INFO in docker-compose files.

### Fixed

- Orderbook Watcher clean shutdown on SIGTERM/SIGINT.
- Directory Server file-based logging removal.
- Handling of failed peer mappings on send failures.

## [0.2.0] - 2025-11-20

### Added

- Orderbook Watcher component.
- Healthcheck to Orderbook Watcher service.
- Directory node connection status tracking.
- Auto-remove stale offers from inactive counterparties.
- Tor hidden service support for mempool.space integration.

### Fixed

- "Unexpected response type: 687" error.
- Fidelity bond handling for new offers.
- Orderbook request logic improvements.
- Connection handling and UI status indicators.

## [0.1.0] - 2025-11-16

### Added

- Initial project structure.
- Directory Server implementation with Peer Types and Monitoring.
- Basic README and Docker setup.
- Pre-built image support for directory server compose.
- Tor configuration instructions.

[Unreleased]: ../../compare/0.19.3...HEAD
[0.19.3]: ../../compare/0.19.2...0.19.3
[0.19.2]: ../../compare/0.19.1...0.19.2
[0.19.1]: ../../compare/0.19.0...0.19.1
[0.19.0]: ../../compare/0.18.0...0.19.0
[0.18.0]: ../../compare/0.17.0...0.18.0
[0.17.0]: ../../compare/0.16.0...0.17.0
[0.16.0]: ../../compare/0.15.0...0.16.0
[0.15.0]: ../../compare/0.14.0...0.15.0
[0.14.0]: ../../compare/0.13.12...0.14.0
[0.13.12]: ../../compare/0.13.11...0.13.12
[0.13.11]: ../../compare/0.13.10...0.13.11
[0.13.10]: ../../compare/0.13.9...0.13.10
[0.13.9]: ../../compare/0.13.8...0.13.9
[0.13.8]: ../../compare/0.13.7...0.13.8
[0.13.7]: ../../compare/0.13.6...0.13.7
[0.13.6]: ../../compare/0.13.5...0.13.6
[0.13.5]: ../../compare/0.13.4...0.13.5
[0.13.4]: ../../compare/0.13.3...0.13.4
[0.13.3]: ../../compare/0.13.2...0.13.3
[0.13.2]: ../../compare/0.13.1...0.13.2
[0.13.1]: ../../compare/0.13.0...0.13.1
[0.13.0]: ../../compare/0.11.6...0.13.0
[0.11.6]: ../../compare/0.11.5...0.11.6
[0.11.5]: ../../compare/0.11.4...0.11.5
[0.11.4]: ../../compare/0.11.3...0.11.4
[0.11.3]: ../../compare/0.11.2...0.11.3
[0.11.2]: ../../compare/0.11.1...0.11.2
[0.11.0]: ../../compare/0.10.0...0.11.0
[0.10.0]: ../../compare/0.9.0...0.10.0
[0.9.0]: ../../compare/0.8.0...0.9.0
[0.8.0]: ../../compare/0.7.0...0.8.0
[0.7.0]: ../../compare/0.6.0...0.7.0
[0.6.0]: ../../compare/0.5.0...0.6.0
[0.5.0]: ../../compare/0.4.0...0.5.0
[0.4.0]: ../../compare/0.3.0...0.4.0
[0.3.0]: ../../compare/0.2.0...0.3.0
[0.2.0]: ../../compare/0.1.0...0.2.0
[0.1.0]: ../../releases/tag/0.1.0
