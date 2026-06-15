# JoinMarket Maker Bot

Earn fees by providing liquidity for CoinJoin transactions. Makers passively earn bitcoin while enhancing network privacy.

> Coming from the reference [joinmarket-clientserver](https://github.com/JoinMarket-Org/joinmarket-clientserver) (now archived)?
> Read [Migration from JoinMarket Reference](#migration-from-joinmarket-reference) first.

## Installation

Install JoinMarket-NG with the maker component:

```bash
curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash -s -- --maker
```

See [Installation](install.md) for backend setup, Tor configuration, and manual install.

## Prerequisites

- Tor is required for production maker operation.
- For Tor SOCKS/control defaults, see [Tor Notes](install.md#tor-notes).
- **No minimum balance is required to run a maker.** The 100k sats
  per mixdepth figure is a common orientation for new users, not a
  protocol requirement; makers with smaller balances will simply match
  fewer taker requests.

## Quick Start

### 1) Create or import a wallet

```bash
jm-wallet generate
# or import an existing mnemonic
jm-wallet import
```

Both commands write to `~/.joinmarket-ng/wallets/default.mnemonic` by
default; subsequent `jm-maker` and `jm-wallet` commands pick it up
automatically (use `--mnemonic-file` only to override).

Store the mnemonic offline. See [Wallet guide](README-jmwallet.md).

### 2) Configure backend

Set `~/.joinmarket-ng/config.toml` and choose one backend:

- `descriptor_wallet` (recommended, own Bitcoin Core)
- `neutrino` (lightweight alternative)

Backend configuration examples are in [Installation](install.md#configure-backend).

### 3) Start maker

```bash
jm-maker start
```

The bot syncs wallet state, builds offers, and waits for takers.

### 4) Optional: tune fees

```bash
# Relative fee (0.2%)
jm-maker start --cj-fee-relative 0.002 --min-size 200000
```

Use exactly one fee model: `--cj-fee-relative` or `--cj-fee-absolute`.

### 5) Optional: dual offers (relative + absolute)

Pass `--dual-offers` (or set `dual_offers = true` under `[maker]` in
`config.toml`) to advertise one relative and one absolute offer
simultaneously.  Their size ranges are split automatically at the fee
intersection `x = cj_fee_absolute / cj_fee_relative`:

- absolute offer covers `[min_size, x]` -- guarantees a flat minimum
  profit on small CoinJoins where percentage fees would be negligible.
- relative offer covers `[x, max_balance]` -- stays competitive on large
  mixes where absolute fees would be wastefully cheap.

Example: `cj_fee_absolute = 1000` sats, `cj_fee_relative = 0.001` -> the
abs offer is announced for CJs up to 1 000 000 sats, the rel offer for
larger CJs.  This produces a piecewise fee curve roughly equivalent to a
linear `min flat fee + proportional component` model without breaking
the protocol.  If the intersection falls below the configured min_size
or above the wallet balance, the dominated offer is suppressed
automatically.

## Fidelity Bonds

Makers automatically discover bonds from the local registry at startup.

- User workflow (generate/list/recover bonds): [Wallet guide](README-jmwallet.md)
- Protocol details and cold-wallet certificate flow: [Technical Privacy Notes](technical/privacy.md#fidelity-bonds)

## Migration from JoinMarket Reference

If you ran a maker on the legacy
[joinmarket-clientserver](https://github.com/JoinMarket-Org/joinmarket-clientserver)
(now archived), most operational concepts carry over: same wire protocol,
same fee models, same fidelity bonds. The main differences:

- **Wallets are mnemonic-based.** No BerkeleyDB, no `wallet.jmdat`. Import
  your existing 12-word seed into a JoinMarket-NG mnemonic file. The same
  addresses and bonds derive from the same seed.
- **No `joinmarket.cfg`.** Configuration lives in
  `~/.joinmarket-ng/config.toml` (TOML, sectioned). See
  [Configuration](technical/configuration.md) and
  [`config.toml.template`](https://github.com/joinmarket-ng/joinmarket-ng/blob/main/jmcore/src/jmcore/data/config.toml.template).
- **No IRC.** Transport is Tor onion services to directory nodes. Tor is
  required in production.
- **Backends:** instead of a Bitcoin Core wallet with manual import, choose
  `descriptor_wallet` (recommended, watch-only on your own Core) or
  `neutrino` (light client). See [Installation](install.md#configure-backend).
- **Yield generators:** the legacy `yg-privacyenhanced.py` script is
  replaced by `jm-maker start` with the same fee flags
  (`--cj-fee-relative`, `--cj-fee-absolute`, `--min-size`).

Typical migration flow:

```bash
# 1. Import your existing mnemonic into the default wallet location
jm-wallet import

# 2. Recover existing fidelity bonds (scans a wider locktime window)
jm-wallet recover-bonds

# 3. Verify balances and addresses match what you expect
jm-wallet info

# 4. Start the maker
jm-maker start
```

See [Wallet guide](README-jmwallet.md) for import options and BIP39
passphrase handling, and [Fidelity Bonds](technical/privacy.md#fidelity-bonds)
for the cold-wallet certificate flow used to register bonds whose key is
not in the hot wallet.

## Docker Deployment

This component ships with a production-oriented `docker-compose.yml`.

- Setup and Tor requirements: see local compose comments and [Tor Notes](install.md#tor-notes)
- Backend tradeoffs and compatibility notes: [Technical Wallet Notes](technical/wallet.md#backend-systems)

Typical run:

```bash
docker-compose up -d
docker-compose logs -f maker
```

## Running as a Service

Makers are long-running and benefit from supervised, auto-restarting
processes. The two common options:

### systemd (Linux)

Create `/etc/systemd/system/jm-maker.service` (replace `youruser` and paths):

```ini
[Unit]
Description=JoinMarket-NG Maker
# Make sure bitcoind and Tor are at least started before us. The unit
# still retries forever (see Restart= below) so a slow bitcoind boot
# (IBD, mempool rebuild, ...) does not put the service into failed state.
After=network-online.target bitcoind.service tor.service
Wants=network-online.target bitcoind.service tor.service

[Service]
Type=simple
User=youruser
ExecStart=/home/youruser/.joinmarket-ng/venv/bin/jm-maker start \
    --mnemonic-file /home/youruser/.joinmarket-ng/wallets/default.mnemonic
Restart=on-failure
RestartSec=30
# Retry forever: bitcoind RPC may be unavailable for several minutes
# after boot. Without this systemd would give up after a few attempts.
StartLimitIntervalSec=0
# Optional hardening
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now jm-maker
journalctl -u jm-maker -f         # live logs
```

For boot auto-start to work the mnemonic password must already live
in `~/.joinmarket-ng/config.toml` (see the next paragraph). If you do
not want the password on disk, leave the unit disabled (drop the
`enable` above) and start it interactively when you log in. This is
the same trade-off the Raspiblitz integration exposes.

If your mnemonic file is encrypted, the bot needs the password at startup
and cannot prompt under systemd. Set `mnemonic_password` (the encryption
password) and/or `bip39_passphrase` (BIP39 25th word) in the `[wallet]`
section of `~/.joinmarket-ng/config.toml`. Make sure that file is
`chmod 600` and owned by the service user. This is the same approach used
by the Raspiblitz integration.

On Raspiblitz, the bonus script manages the systemd unit for you; see the
[TUI guide](README-tui.md).

#### FHS layout: separate config and data directories

By default the config file lives at `<data-dir>/config.toml`. To follow the
Linux Filesystem Hierarchy Standard (config under `/etc`, state under
`/var/lib`) pass `--config-file` to decouple the two paths. This pairs well
with systemd's `ConfigurationDirectory` and `StateDirectory` directives:

```ini
[Service]
Type=simple
DynamicUser=yes
ConfigurationDirectory=joinmarket
StateDirectory=joinmarket
ExecStart=/opt/joinmarket-ng/venv/bin/jm-maker start \
    --config-file /etc/joinmarket/config.toml \
    --data-dir /var/lib/joinmarket \
    --mnemonic-file /var/lib/joinmarket/wallets/default.mnemonic
```

`--config-file` accepts a path anywhere on disk (the equivalent
`JOINMARKET_CONFIG_FILE` environment variable also works, and is honored by
the components that do not take a `--data-dir` flag, such as the directory
server and orderbook watcher). On first run the config template is created at
that path; the data directory is used only for wallet state, history, and
runtime files.

### Docker

The bundled `docker-compose.yml` uses `restart: unless-stopped` and is the
recommended path on machines where Tor and the backend are also containers.
See the [Docker Deployment](#docker-deployment) section above.

## Logs

By default `jm-maker` logs to stderr in human-readable format. Common
patterns:

```bash
# Tee to a file while keeping live output
jm-maker start --mnemonic-file ... 2>&1 | tee -a ~/.joinmarket-ng/jm-maker.log

# Verbose troubleshooting (very chatty)
jm-maker start --log-level DEBUG ...

# Quieter for unattended operation
jm-maker start --log-level WARNING ...
```

When running under systemd, logs go to the journal automatically; use
`journalctl -u jm-maker` (add `-f` to follow, `--since "1 hour ago"` to
filter). Persistent journals survive reboots if
`/var/log/journal` exists.

The default `INFO` level only logs state changes (offers created/updated,
balance changes, peer events). Routine periodic wallet rescans and healthy
directory connection status are emitted at `DEBUG` to keep long-running
maker logs readable. Disconnections, failed transactions, and rate-limit
events are always logged at `WARNING` or `ERROR`.

## Configuration Notes

Configuration merges as: `config.toml` < environment variables < CLI flags.

## Multiple Local Instances

If you want to run more than one maker on the same machine, give each maker
its own data directory. The simplest pattern is to pass `--data-dir` (or set
`JOINMARKET_DATA_DIR`) on every `jm-maker` and `jm-wallet` command so each
instance gets its own `config.toml`, wallet files, logs, and local runtime
state.

```bash
mkdir -p ~/jm-maker-a ~/jm-maker-b

jm-maker config-init --data-dir ~/jm-maker-a
jm-maker config-init --data-dir ~/jm-maker-b

jm-wallet generate --data-dir ~/jm-maker-a
jm-wallet generate --data-dir ~/jm-maker-b

jm-maker start \
  --data-dir ~/jm-maker-a \
  --mnemonic-file ~/jm-maker-a/wallets/default.mnemonic

jm-maker start \
  --data-dir ~/jm-maker-b \
  --mnemonic-file ~/jm-maker-b/wallets/default.mnemonic
```

For takers, separate installations are usually unnecessary. One installation
can manage multiple wallet mnemonic files, and you can switch between them
with `--mnemonic-file`. Use separate `--data-dir` values for takers only when
you specifically want isolated config and runtime state.

For full option lists and exact defaults, use the auto-generated command help below (`jm-maker start --help`).

## Security and Operations

- Maker transaction signing includes strict verification before signature release.
- Directory communication goes over Tor; production should avoid clearnet fallback behavior.
- Keep mnemonic files encrypted and backed up; never share mnemonic or wallet files.

Common checks:

```bash
jm-wallet info --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic
jm-maker start --help
```

## Command Reference

<!-- AUTO-GENERATED HELP START: jm-maker -->

<details>
<summary><code>jm-maker --help</code></summary>

```

 Usage: jm-maker [OPTIONS] COMMAND [ARGS]...

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --install-completion          Install completion for the current shell.      │
│ --show-completion             Show completion for the current shell, to copy │
│                               it or customize the installation.              │
│ --help                        Show this message and exit.                    │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ start             Start the maker bot.                                       │
│ generate-address  Generate a new receive address.                            │
│ config-init       Initialize the config file with default settings.          │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-maker start --help</code></summary>

```

 Usage: jm-maker start [OPTIONS]

 Start the maker bot.

 Configuration is loaded from ~/.joinmarket-ng/config.toml (or
 $JOINMARKET_DATA_DIR/config.toml),
 environment variables, and CLI arguments. CLI arguments have the highest
 priority.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --mnemonic-file         -f      PATH                  Path to mnemonic file  │
│ --prompt-bip39-passph…                                Prompt for BIP39       │
│                                                       passphrase             │
│                                                       interactively          │
│ --data-dir              -d      PATH                  Data directory for     │
│                                                       JoinMarket files.      │
│                                                       Defaults to            │
│                                                       ~/.joinmarket-ng       │
│                                                       [env var:              │
│                                                       JOINMARKET_DATA_DIR]   │
│ --network                       [mainnet|testnet|sig  Protocol network       │
│                                 net|regtest]          (mainnet, testnet,     │
│                                                       signet, regtest)       │
│ --bitcoin-network               [mainnet|testnet|sig  Bitcoin network for    │
│                                 net|regtest]          address generation     │
│                                                       (defaults to           │
│                                                       --network)             │
│ --backend-type                  TEXT                  Backend type:          │
│                                                       descriptor_wallet |    │
│                                                       neutrino               │
│ --rpc-url                       TEXT                  Bitcoin full node RPC  │
│                                                       URL                    │
│                                                       [env var:              │
│                                                       BITCOIN_RPC_URL]       │
│ --neutrino-url                  TEXT                  Neutrino REST API URL  │
│                                                       [env var:              │
│                                                       NEUTRINO_URL]          │
│ --min-size                      INTEGER               Minimum CoinJoin size  │
│                                                       in sats                │
│ --cj-fee-relative               TEXT                  Relative coinjoin fee  │
│                                                       (e.g., 0.001 = 0.1%)   │
│                                                       [env var:              │
│                                                       CJ_FEE_RELATIVE]       │
│ --cj-fee-absolute               INTEGER               Absolute coinjoin fee  │
│                                                       in sats. Mutually      │
│                                                       exclusive with         │
│                                                       --cj-fee-relative.     │
│                                                       [env var:              │
│                                                       CJ_FEE_ABSOLUTE]       │
│ --tx-fee-contribution           INTEGER               Tx fee contribution in │
│                                                       sats                   │
│ --directory             -D      TEXT                  Directory servers      │
│                                                       (comma-separated       │
│                                                       host:port)             │
│                                                       [env var:              │
│                                                       DIRECTORY_SERVERS]     │
│ --tor-socks-host                TEXT                  Tor SOCKS proxy host   │
│                                                       (overrides             │
│                                                       TOR__SOCKS_HOST)       │
│ --tor-socks-port                INTEGER               Tor SOCKS proxy port   │
│                                                       (overrides             │
│                                                       TOR__SOCKS_PORT)       │
│ --tor-control-host              TEXT                  Tor control port host  │
│                                                       (overrides             │
│                                                       TOR__CONTROL_HOST)     │
│ --tor-control-port              INTEGER               Tor control port       │
│                                                       (overrides             │
│                                                       TOR__CONTROL_PORT)     │
│ --tor-cookie-path               PATH                  Path to Tor cookie     │
│                                                       auth file (overrides   │
│                                                       TOR__COOKIE_PATH)      │
│ --disable-tor-control                                 Disable Tor control    │
│                                                       port integration       │
│ --onion-serving-host            TEXT                  Bind address for       │
│                                                       incoming connections   │
│                                                       (overrides             │
│                                                       MAKER__ONION_SERVING_… │
│ --onion-serving-port            INTEGER               Port for incoming      │
│                                                       .onion connections     │
│                                                       (overrides             │
│                                                       MAKER__ONION_SERVING_… │
│ --tor-target-host               TEXT                  Target hostname for    │
│                                                       Tor hidden service     │
│                                                       (overrides             │
│                                                       TOR__TARGET_HOST)      │
│ --fidelity-bond-lockt…  -L      INTEGER               Fidelity bond          │
│                                                       locktimes to scan for  │
│ --fidelity-bond-index   -I      INTEGER               Fidelity bond          │
│                                                       derivation index       │
│                                                       [env var:              │
│                                                       FIDELITY_BOND_INDEX]   │
│ --fidelity-bond         -B      TEXT                  Specific fidelity bond │
│                                                       to use (format:        │
│                                                       txid:vout)             │
│ --no-fidelity-bond                                    Disable fidelity bond  │
│                                                       usage. Skips registry  │
│                                                       lookup and bond proof  │
│                                                       generation even when   │
│                                                       bonds exist in the     │
│                                                       registry.              │
│ --merge-algorithm       -M      TEXT                  UTXO selection         │
│                                                       strategy: default,     │
│                                                       gradual, greedy,       │
│                                                       random                 │
│                                                       [env var:              │
│                                                       MERGE_ALGORITHM]       │
│ --dual-offers                                         Create both relative   │
│                                                       and absolute fee       │
│                                                       offers simultaneously. │
│                                                       Each offer gets a      │
│                                                       unique ID (0 for       │
│                                                       relative, 1 for        │
│                                                       absolute). Use with    │
│                                                       --cj-fee-relative and  │
│                                                       --cj-fee-absolute to   │
│                                                       set fees for each.     │
│ --log-level             -l      TEXT                  Log level              │
│ --help                                                Show this message and  │
│                                                       exit.                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-maker generate-address --help</code></summary>

```

 Usage: jm-maker generate-address [OPTIONS]

 Generate a new receive address.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --mnemonic-file         -f      PATH                  Path to mnemonic file  │
│ --prompt-bip39-passph…                                Prompt for BIP39       │
│                                                       passphrase             │
│                                                       interactively          │
│ --network                       [mainnet|testnet|sig  Protocol network       │
│                                 net|regtest]                                 │
│ --bitcoin-network               [mainnet|testnet|sig  Bitcoin network for    │
│                                 net|regtest]          address generation     │
│                                                       (defaults to           │
│                                                       --network)             │
│ --backend-type                  TEXT                  Backend type           │
│ --data-dir                      PATH                  Data directory         │
│                                                       (default:              │
│                                                       ~/.joinmarket-ng or    │
│                                                       $JOINMARKET_DATA_DIR)  │
│                                                       [env var:              │
│                                                       JOINMARKET_DATA_DIR]   │
│ --log-level             -l      TEXT                  Log level              │
│ --help                                                Show this message and  │
│                                                       exit.                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-maker config-init --help</code></summary>

```

 Usage: jm-maker config-init [OPTIONS]

 Initialize the config file with default settings.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --data-dir  -d      PATH  Data directory for JoinMarket files                │
│                           [env var: JOINMARKET_DATA_DIR]                     │
│ --help                    Show this message and exit.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>


<!-- AUTO-GENERATED HELP END: jm-maker -->
