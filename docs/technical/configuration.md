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

## Notes

- BIP39 passphrases are not intended to be stored in config for normal operations.
- Keep secrets out of shell history; prefer config file permissions and environment handling best practices.
