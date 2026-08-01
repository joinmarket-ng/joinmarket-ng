# Configuration

JoinMarket NG loads settings in this order (highest priority first):

1. CLI arguments
2. Environment variables
3. `~/.joinmarket-ng/config.toml`
4. Built-in defaults

## Config File

Main config path:

- `~/.joinmarket-ng/config.toml`

Template/reference:

- Bundled inside the `jmcore` package at `jmcore/src/jmcore/data/config.toml.template`
  (the canonical source, loaded at runtime via `importlib.resources`).
- A reference copy is kept alongside your config at
  `~/.joinmarket-ng/config.toml.template` and refreshed automatically on
  install, update, and component startup.

Fresh installations create a small `config.toml` from the bundled
`config-starter.toml.template`. It contains empty sections and commented examples
for Bitcoin Core connections, maker fees, taker fee limits, and TUI logging.
The full reference remains in the separate `config.toml.template` file. Flatpak
continues to use its bundled Neutrino backend unless explicitly overridden.

Uncomment only the settings you want to override. Omitted settings use the
installed version's built-in defaults, which can change between releases.
An active assignment pins your choice even when it equals a current default.
Copy additional options into the matching section, adding its header only if
that section is absent. The TUI can insert its settings into an empty or minimal
config; template placeholders are not required.

## Config Updates

Your `config.toml` contents are never modified automatically after creation.
Existing installations keep their customized files, including full templates
from older versions. They are not automatically shortened or merged.

During `install.sh --update`, the installer snapshots the previously installed
package's full template before replacing packages and compares it with the
target release's template. This includes changes across skipped releases,
without inspecting or displaying credentials from your config. Interactive
updates offer to show the diff; unattended updates print it in their output.
Each diff hunk identifies its TOML section. Release notes also include these
section-labeled template diffs.

The adjacent reference is refreshed on startup, so it is not used as an upgrade
baseline. If the old package template or comparison helper is unavailable, the
installer reports that comparison is unavailable and refers to the release
notes. Missing settings are normal, not an indication that your file is outdated.

If `config.toml` is missing entirely, creation uses the small starter. Installing
an older release that predates the starter retains its full-template behavior.

`jmcore.settings.config_diff()` remains available as a read-only inventory of
sections and keys absent from a user file. It returns a list of `section:<name>`
and `key:<section>.<key>` strings, not a release-to-release comparison.

## Section Names

Top-level sections in config use these names:

- `[tor]`
- `[bitcoin]`
- `[network_config]`
- `[wallet]`
- `[logging]`
- `[notifications]`
- `[maker]`
- `[taker]`
- `[tumbler]`
- `[directory_server]`
- `[orderbook_watcher]`
- `[tui]`

## Environment Variable Mapping

Nested fields use double underscores:

- `TOR__SOCKS_HOST`
- `BITCOIN__RPC_URL`
- `NETWORK_CONFIG__NETWORK`
- `MAKER__MIN_SIZE`
- `TAKER__COUNTERPARTY_COUNT`
- `MAKER__CHANNEL_RING__ENABLED`
- `MAKER__SWAP_PROVIDER__ENABLED`

Some CLI flags still support legacy env var names (for compatibility), but config/env should prefer the canonical section-based names above.

## Minimal Example

```toml
[bitcoin]
backend_type = "descriptor_wallet"
rpc_url = "http://127.0.0.1:8332"
rpc_user = "rpcuser"
rpc_password = "rpcpassword"

[network_config]
network = "mainnet"

[tor]
socks_host = "127.0.0.1"
socks_port = 9050
```

## Backend Options

- `descriptor_wallet` (recommended)
- `neutrino`

## Logging

`[logging].level` controls verbosity (default: `"INFO"`). The separate
`sensitive` option includes privacy-sensitive diagnostics such as wallet
addresses, mixdepth balances, selected UTXOs, transaction IDs, descriptors, and
detailed errors. It defaults to `false`, including at `DEBUG` and `TRACE` levels.
To include these details while troubleshooting, set:

```toml
[logging]
level = "DEBUG"
sensitive = true
```

Restart the component to apply the change. The equivalent environment variables
are `LOGGING__LEVEL=DEBUG` and `LOGGING__SENSITIVE=true`.

The option is intended for wallet and transaction diagnostics. Routine wallet
initialization reports whether a BIP39 passphrase is set, without printing its
value; mnemonic source messages report where the recovery words were loaded
from, without printing the words. However, this option filters whole log records
and does not redact their contents. Detailed exception tracebacks can include
variable values. Keep these logs private and review them before sharing excerpts.

## Wallet History Reconstruction

`[wallet].reconstruct_history` defaults to `true`. When a wallet with no local
protocol history is imported from seed, JoinMarket NG defers while a Bitcoin
Core rescan is active, then enumerates confirmed wallet transactions on the
next wallet sync and persists guessed `maker`, `taker`, `send`, and `deposit`
rows in `history.csv`.
These rows have `source=onchain`; live protocol rows have `source=protocol` and
are never overwritten.

Set `reconstruct_history = false` to disable the automatic pass. The explicit
`jm-wallet reconstruct-history` command remains available and preserves all
protocol-recorded rows.

The reconstruction uses JoinMarket's equal-output CoinJoin heuristic. The
CoinJoin amount and equal-output count are observable, but maker/taker role and
fees are not unambiguous on-chain. Reconstructed role and fee fields are
therefore estimates, and counterparty nicknames cannot be recovered.
The manual command waits for an active Bitcoin Core rescan before purging or
rebuilding rows. Reconstructed maker guesses are excluded from the legacy
yield-generator earnings report, whose fee fields are authoritative. History
statistics include reconstructed rows and are labeled as estimates when present.

Once a complete pass reaches the backend tip, a wallet-scoped transaction cursor
makes later passes incremental. Capped passes deliberately do not advance that
cursor, so `jm-wallet reconstruct-history --keep-existing` can continue the older
backlog without skipping transactions. Successfully backfilled Neutrino branch
coverage is persisted separately, avoiding repeated rescans while a capped backlog
is processed. This includes hashed coverage markers for explicit fidelity-bond
addresses. Purging reconstructed rows or widening historical address coverage
invalidates the transaction cursor and causes a complete enumeration.

## Neutrino TLS Settings

When using the `neutrino` backend with TLS enabled (default), set:

- `neutrino_tls_cert` -- path to the neutrino-api TLS certificate (PEM)
- `neutrino_auth_token` -- API bearer token string
- `neutrino_auth_token_file` -- path to a file containing the token (alternative to `neutrino_auth_token`)

The `neutrino_url` must use `https://` when TLS is enabled.
See [Neutrino TLS](neutrino-tls.md) and [Installation](../install.md) for the practical migration/setup steps.

## Directory Server Settings

Public directory nodes must set `directory_server.nick_auth_directory_id`, or the equivalent
`DIRECTORY_SERVER__NICK_AUTH_DIRECTORY_ID` environment variable, to their canonical lowercase
Tor v3 endpoint including the port. For example:

```toml
[directory_server]
nick_auth_directory_id = "your56characterhostname.onion:5222"
```

This identity binds signed nick ownership proofs to the directory endpoint selected by the
client. If it is absent, the directory does not advertise or perform nick authentication, leaving
nicks unverified. `test:` identities are only for local and automated test deployments.

`nick_auth_mode` defaults to `prefer_verified`, which authenticates capable clients while
retaining compatibility with legacy clients. Set it to `require_verified` to reject clients that
do not support nick authentication. Setting it to `disabled` intentionally turns off the
protection.

### Heartbeat Settings

The `[directory_server]` section supports heartbeat liveness controls:

- `heartbeat_sweep_interval` (default `60.0`): seconds between sweep cycles
- `heartbeat_idle_threshold` (default `600.0`): idle seconds before probing
- `heartbeat_hard_evict` (default `1500.0`): idle seconds before unconditional eviction
- `heartbeat_pong_wait` (default `30.0`): seconds to wait for PONG reply

These values are tuned to match joinmarket-rs defaults for interoperability.

## Co-Funded Channel Ring

The nested `[maker.channel_ring]` and `[taker.channel_ring]` sections are disabled by
default. Enabling either requires a `p2tr` wallet, a `tr0` offer family, a local
TLS-authenticated LND gRPC endpoint, and a v3 onion `host:port` advertised by that
LND node identity. Remote or shared LND gRPC endpoints are rejected.

The runtime checks final Taproot channel support and the live-validated safe
verified-channel retirement path before advertising or selecting
`cofunded_channel_ring_v1`.
Participant state is stored in atomic mode-0600 JSON files under a mode-0700
directory. The macaroon and TLS certificate contents are never persisted there.

Ring-enabled takers require a full backend that can prove transaction absence in
both the mempool and chain. They select an exact, uniformly capable maker set and
run a strict all-party round. A failed ring round is aborted without ordinary
change outputs, fallback CoinJoin construction, or a smaller ring.

### Live Validation

The isolated Docker test uses three makers, four stock LND v0.21.1 nodes, and
fresh regtest wallets. It intentionally rejects one revision before completing a
four-party ring, paying every directed channel edge, and restarting the taker to
verify persisted reconciliation. Remove the bind-mounted test state before each
run:

```bash
rm -rf tmp/ring-e2e
./scripts/run-ring-e2e.sh ring
```

The script tears down containers and volumes, removes container-owned LND, Tor,
and wallet state, starts a fresh profile, runs the test, and cleans up. Resetting
only the bitcoind volume leaves the host-mounted LND wallets ahead of the new
chain and produces unrelated `Block height out of range` failures. Use
`./scripts/run-ring-e2e.sh lnd-external` for the focused two-node LND lifecycle
suite, or `all` for both suites. `RING_E2E_KEEP=1` leaves a profile running for
inspection and `RING_E2E_NO_RESET=1` deliberately reuses the current profile.
The ring suite rebuilds its application images by default so it cannot test stale
maker code; `RING_E2E_NO_BUILD=1` explicitly reuses existing images.

The parallel runner also provides the ring test with isolated ports and state
cleanup via `./scripts/run_parallel_tests.sh --suite ring-e2e`.

## Maker Swap Provider

`[maker.swap_provider]` is disabled by default. Enabling it requires
`bitcoin.backend_type = "descriptor_wallet"`, `wallet.address_type = "p2tr"`, one
or more `ws://` or `wss://` Nostr relays, an HTTPS LND REST URL, and pinned LND TLS
and macaroon paths. Nostr always uses `[tor]` SOCKS settings. Direct onion serving
also requires a static maker onion host or enabled Tor control.

The nested section controls offered directions, percentage and mining fees, amount
limits, confirmation depth, invoice/swap timeout, polling, offer refresh, request
limits, and the mode-0700 persistence directory. Forward requests that do not register
an invoice expire separately, concurrent nonterminal swaps are globally capped, and only
the configured number of newest terminal records are retained. `use_channel_ring_lnd = true` is
an explicit privacy tradeoff: it reuses the channel-ring LND credentials and is
rejected unless that backend is enabled. The separate REST URL is still required.

The isolated Docker profile exercises Tor-routed Nostr discovery/RPC, authenticated
direct onion RPC, both swap directions, provider restart, cooperative MuSig2 spend,
and script-path settlement. Run it with
`./scripts/run_parallel_tests.sh --suite swap-provider-e2e`, or use the manual
commands in `jmswap/README.md`.

## Notes

- BIP39 passphrases are not intended to be stored in config for normal operations.
- Keep secrets out of shell history; prefer config file permissions and environment handling best practices.
